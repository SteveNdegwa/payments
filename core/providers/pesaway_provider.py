import base64
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal, InvalidOperation

import requests
from django.core.exceptions import ValidationError
from requests import RequestException

from core.providers.base_provider import (
    BaseProvider,
    ProviderBalance,
    ProviderResult,
    ProviderResultStatus,
)
from core.services.registry import register_provider
from utils.common import check_required_fields

logger = logging.getLogger(__name__)


@register_provider("core.providers.pesaway_provider.PesaWayProvider")
class PesaWayProvider(BaseProvider):
    REGION = "ke"
    ACCEPTED_CODE = "200.001"
    callback_verification_supported = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._access_token = ""
        self._access_token_expires_at = 0.0

    @property
    def base_url(self) -> str:
        base_url = self.config.get("base_url", "")
        if not base_url:
            raise ValidationError("PesaWay config missing required field: base_url")
        return base_url.rstrip("/")

    @property
    def merchant_code(self) -> str:
        merchant_code = self.config.get("merchant_code", "")
        if not merchant_code:
            raise ValidationError("PesaWay config missing required field: merchant_code")
        return str(merchant_code)

    def _validate_config(self, *, callback: bool = False) -> None:
        required = {"callback_signature_key"} if callback else {"consumer_key", "consumer_secret"}
        missing = sorted(field for field in required if not self.config.get(field))
        if missing:
            raise ValidationError(f"PesaWay config missing: {', '.join(missing)}")

    @staticmethod
    def _decimal_or_none(value) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    @property
    def token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at - 30:
            return self._access_token

        self._validate_config()
        response = requests.post(
            f"{self.base_url}/api/v1/token/",
            json={
                "consumer_key": self.config["consumer_key"],
                "consumer_secret": self.config["consumer_secret"],
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/json", "X-Region": self.REGION},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        token = data.get("token")
        if not token:
            raise ValueError("PesaWay token response did not include an access token")
        self._access_token = token
        self._access_token_expires_at = float(data.get("expires_at") or 0)
        return token

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Region": self.REGION,
        }

    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        try:
            if currency.upper() != "KES":
                return self._unsupported_currency(currency)
            check_required_fields(
                payload,
                {"phone_number", "channel", "callback_url", "external_reference"},
            )
            if payload["channel"] not in {"MPESA", "Airtel"}:
                return ProviderResult(
                    status=ProviderResultStatus.FAILED,
                    failure_code="unsupported_channel",
                    failure_reason="PesaWay Kenya collections support MPESA and Airtel.",
                )

            request_payload = {
                "ExternalReference": payload["external_reference"],
                "Amount": str(amount),
                "PhoneNumber": payload["phone_number"],
                "MerchantCode": self.merchant_code,
                "Channel": payload["channel"],
                "Currency": currency.upper(),
                "Reason": payload.get("reason", "Payment"),
                "ResultsUrl": payload["callback_url"],
            }
            response = requests.post(
                f"{self.base_url}/api/v1/mobile-money/receive-payment/",
                json=request_payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            if str(data.get("code")) == self.ACCEPTED_CODE:
                return ProviderResult(
                    status=ProviderResultStatus.REQUIRES_ACTION,
                    provider_transaction_id=data.get("TransactionID", ""),
                    provider_reference=data.get("OriginatorReference", ""),
                    raw_response=data,
                    next_action={
                        "type": "mobile_money_prompt",
                        "message": data.get(
                            "ResponseDescription",
                            "Approve the mobile-money prompt to complete payment.",
                        ),
                    },
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=str(data.get("code") or "provider_rejected"),
                failure_reason=data.get("ResponseDescription", "PesaWay rejected the collection"),
                raw_response=data,
            )
        except RequestException as exc:
            logger.exception("PesaWay HTTP error during collection")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.exception("PesaWay collection failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
            )

    def query_status(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        try:
            payment_method_code = payload.get("payment_method_code")
            resource = "bank" if payment_method_code == "BANK_TRANSFER" else "mobile-money"
            response = requests.get(
                f"{self.base_url}/api/v1/{resource}/transaction-query/",
                json={"TransactionReference": provider_transaction_id},
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            success = str(data.get("ResultCode")) == "0"
            return ProviderResult(
                status=(ProviderResultStatus.SUCCESS if success else ProviderResultStatus.FAILED),
                provider_transaction_id=data.get("Reference", provider_transaction_id),
                provider_reference=data.get("Receipt", ""),
                failure_code="" if success else str(data.get("ResultCode", "unknown")),
                failure_reason="" if success else data.get("ResultDesc", "Transaction failed"),
                raw_response=data,
            )
        except RequestException as exc:
            logger.exception("PesaWay HTTP error during status query")
            return ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                provider_transaction_id=provider_transaction_id,
                failure_code="http_error",
                failure_reason=str(exc),
            )

    def transfer(
        self,
        *,
        recipient_type: str,
        payment_method_code: str,
        amount: Decimal,
        currency: str,
        payload: dict,
    ) -> ProviderResult:
        try:
            if currency.upper() != "KES":
                return self._unsupported_currency(currency)
            check_required_fields(payload, {"external_reference", "callback_url"})
            request_payload = {
                "ExternalReference": payload["external_reference"],
                "Amount": str(amount),
                "MerchantCode": self.merchant_code,
                "Currency": currency.upper(),
                "Reason": payload.get("reason", "Outbound transfer"),
                "ResultsUrl": payload["callback_url"],
            }

            if payment_method_code == "BANK_TRANSFER":
                check_required_fields(payload, {"account_number", "bank_name"})
                request_payload.update(
                    AccountNumber=payload["account_number"],
                    Channel="Bank",
                    BankName=payload["bank_name"],
                )
                resource = "bank"
            elif payment_method_code != "MOBILE_MONEY":
                return self._unsupported_operation(
                    f"{recipient_type} {payment_method_code} transfer"
                )
            elif recipient_type == "B2C":
                check_required_fields(payload, {"phone_number", "channel"})
                if payload["channel"] not in {"MPESA", "Airtel"}:
                    return self._unsupported_transfer_channel(payload["channel"])
                request_payload.update(
                    PhoneNumber=payload["phone_number"],
                    Channel=payload["channel"],
                )
                resource = "mobile-money"
            elif recipient_type == "B2B":
                check_required_fields(payload, {"account_number", "channel"})
                if payload["channel"] not in {"MPESA Paybill", "MPESA Till"}:
                    return self._unsupported_transfer_channel(payload["channel"])
                request_payload.update(
                    AccountNumber=payload["account_number"],
                    Channel=payload["channel"],
                )
                resource = "mobile-money"
            else:
                return self._unsupported_operation(
                    f"{recipient_type} {payment_method_code} transfer"
                )

            response = requests.post(
                f"{self.base_url}/api/v1/{resource}/send-payment/",
                json=request_payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            if str(data.get("code")) == self.ACCEPTED_CODE:
                return ProviderResult(
                    status=ProviderResultStatus.PENDING,
                    provider_transaction_id=data.get("TransactionID", ""),
                    provider_reference=data.get("OriginatorReference", ""),
                    raw_response=data,
                )
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=str(data.get("code") or "provider_rejected"),
                failure_reason=data.get("ResponseDescription", "PesaWay rejected the transfer"),
                raw_response=data,
            )
        except RequestException as exc:
            logger.exception("PesaWay HTTP error during outbound transfer")
            return ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                failure_code="http_error",
                failure_reason=str(exc),
            )
        except Exception as exc:
            logger.exception("PesaWay outbound transfer failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
            )

    @staticmethod
    def _unsupported_transfer_channel(channel: str) -> ProviderResult:
        return ProviderResult(
            status=ProviderResultStatus.FAILED,
            failure_code="unsupported_channel",
            failure_reason=f"Unsupported PesaWay Kenya transfer channel: {channel}",
        )

    @staticmethod
    def _unsupported_currency(currency: str) -> ProviderResult:
        return ProviderResult(
            status=ProviderResultStatus.FAILED,
            failure_code="unsupported_currency",
            failure_reason=f"PesaWay Kenya only supports KES, not {currency}.",
        )

    def query_balance(self) -> ProviderBalance:
        response = requests.post(
            f"{self.base_url}/api/v1/account-balance/",
            json={"MerchantCode": self.merchant_code},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return ProviderBalance(
            amount=Decimal(str(data["available"])),
            currency=str(data.get("currency") or "KES").upper(),
        )

    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        try:
            self._validate_config(callback=True)
        except ValidationError:
            return False

        signature = next(
            (str(value) for key, value in headers.items() if key.lower() == "signature"),
            "",
        )
        if not signature:
            return False

        raw_body = payload.get("_raw_body")
        if raw_body is None:
            callback_data = {
                key: value
                for key, value in payload.items()
                if key not in {"_raw_body", "_source_ip"}
            }
            raw_body = json.dumps(callback_data)
        digest = hmac.new(
            self.config["callback_signature_key"].encode(),
            raw_body.encode(),
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(signature, expected)

    def parse_callback(self, *, payload: dict) -> ProviderResult:
        callback_data = {
            key: value for key, value in payload.items() if key not in {"_raw_body", "_source_ip"}
        }
        result_code = callback_data.get("ResultCode", callback_data.get("ResultType"))
        if result_code is None:
            return ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                failure_code="unknown_callback_format",
                raw_response=callback_data,
            )

        success = str(result_code) == "0"
        balance = self._decimal_or_none(callback_data.get("AccountAvailableFunds"))
        amount_processed = self._decimal_or_none(callback_data.get("TransactionAmount"))

        return ProviderResult(
            status=ProviderResultStatus.SUCCESS if success else ProviderResultStatus.FAILED,
            provider_transaction_id=callback_data.get("TransactionID", ""),
            provider_reference=callback_data.get("TransactionReceipt", ""),
            failure_code="" if success else str(result_code),
            failure_reason="" if success else callback_data.get("ResultDesc", "Transaction failed"),
            raw_response=callback_data,
            amount_processed=amount_processed if success else None,
            account_balance=balance,
            account_balance_currency="KES" if balance is not None else "",
        )
