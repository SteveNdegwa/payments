import base64
import ipaddress
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from django.core.exceptions import ValidationError
from requests import RequestException
from requests.auth import HTTPBasicAuth

from core.providers.base_provider import (
    BaseProvider,
    ProviderResult,
    ProviderResultStatus,
)
from core.services.registry import register_provider
from utils.common import check_required_fields

logger = logging.getLogger(__name__)


@register_provider("core.providers.mpesa_daraja_provider.MpesaDarajaProvider")
class MpesaDarajaProvider(BaseProvider):
    callback_verification_supported = True

    def _validate_config(self) -> None:
        required_fields = {
            "consumer_key",
            "consumer_secret",
        }
        self._raise_for_missing_config(required_fields)

    def _validate_stk_push_config(self) -> None:
        required_fields = {
            "consumer_key",
            "consumer_secret",
            "business_shortcode",
            "business_passkey",
        }
        self._raise_for_missing_config(required_fields)

    def _validate_b2b_config(self) -> None:
        required_fields = {
            "consumer_key",
            "consumer_secret",
            "business_shortcode",
            "initiator_name",
            "security_credential",
        }
        self._raise_for_missing_config(required_fields)

    def _raise_for_missing_config(self, required_fields: set[str]) -> None:
        missing_fields = [field for field in required_fields if not self.config.get(field)]
        if missing_fields:
            raise ValidationError(
                f"Daraja config missing required fields: {', '.join(missing_fields)}"
            )

    @property
    def base_url(self) -> str:
        base_url = self.config.get("base_url", "")
        if not base_url:
            raise ValidationError("Daraja config missing required field: base_url")
        return base_url.rstrip("/")

    @property
    def token(self) -> str:
        self._validate_config()
        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"
        consumer_key = self.config.get("consumer_key")
        consumer_secret = self.config.get("consumer_secret")
        response = requests.get(url, auth=HTTPBasicAuth(consumer_key, consumer_secret))
        response.raise_for_status()
        return response.json()["access_token"]

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        return self._stk_push(amount=amount, payload=payload)

    def transfer(
        self,
        *,
        recipient_type: str,
        payment_method_code: str,
        amount: Decimal,
        currency: str,
        payload: dict,
    ) -> ProviderResult:
        if recipient_type != "B2B" or payment_method_code != "MOBILE_MONEY":
            return self._unsupported_operation(f"{recipient_type} {payment_method_code} transfer")
        if currency.upper() != "KES":
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="unsupported_currency",
                failure_reason="Daraja Kenya B2B transfers require KES.",
            )
        return self._paybill_to_paybill(amount=amount, payload=payload)

    def _stk_push(self, *, amount: Decimal, payload: dict) -> ProviderResult:
        try:
            self._validate_stk_push_config()
            check_required_fields(
                payload=payload,
                required_fields={
                    "phone_number",
                    "account_reference",
                    "callback_url",
                },
            )

            shortcode = self.config["business_shortcode"]
            passkey = self.config["business_passkey"]

            timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
            password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()

            phone_number = payload["phone_number"]
            callback_url = payload["callback_url"]
            account_reference = payload["account_reference"]
            transaction_desc = payload.get("transaction_desc", "Payment")

            request_payload = {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(amount),
                "PartyA": phone_number,
                "PartyB": shortcode,
                "PhoneNumber": phone_number,
                "CallBackURL": callback_url,
                "AccountReference": account_reference,
                "TransactionDesc": transaction_desc,
            }

            url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"

            response = requests.post(
                url,
                json=request_payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            response_data = response.json()

            return ProviderResult(
                status=ProviderResultStatus.REQUIRES_ACTION,
                provider_transaction_id=response_data.get("CheckoutRequestID"),
                raw_response=response_data,
                next_action={
                    "type": "mobile_money_stk_push",
                    "message": response_data.get(
                        "CustomerMessage",
                        "Enter your M-Pesa PIN on your phone to complete payment.",
                    ),
                },
            )

        except RequestException as exc:
            logger.exception("MpesaDarajaProvider HTTP error during charge")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", dict)(),
            )

        except Exception as exc:
            logger.exception("MpesaDarajaProvider charge exception")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def _paybill_to_paybill(self, *, amount: Decimal, payload: dict) -> ProviderResult:
        try:
            self._validate_b2b_config()
            check_required_fields(
                payload=payload,
                required_fields={
                    "destination_shortcode",
                    "account_reference",
                    "callback_url",
                },
            )

            shortcode = self.config["business_shortcode"]
            initiator = self.config["initiator_name"]
            security_credential = self.config["security_credential"]

            callback_url = payload["callback_url"]
            request_payload = {
                "Initiator": initiator,
                "SecurityCredential": security_credential,
                "CommandID": "BusinessPayBill",
                "SenderIdentifierType": "4",
                "RecieverIdentifierType": "4",
                "Amount": int(amount),
                "PartyA": payload.get("source_shortcode", shortcode),
                "PartyB": payload["destination_shortcode"],
                "AccountReference": payload["account_reference"],
                "Remarks": payload.get("remarks", "Business payment"),
                "QueueTimeOutURL": payload.get("queue_timeout_url", callback_url),
                "ResultURL": callback_url,
            }

            url = f"{self.base_url}/mpesa/b2b/v1/paymentrequest"

            response = requests.post(
                url,
                json=request_payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            response_data = response.json()
            response_code = response_data.get("ResponseCode")

            if response_code == "0":
                return ProviderResult(
                    status=ProviderResultStatus.PENDING,
                    provider_transaction_id=response_data.get("ConversationID", ""),
                    provider_reference=response_data.get("OriginatorConversationID", ""),
                    raw_response=response_data,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=response_code or "provider_rejected",
                failure_reason=response_data.get("ResponseDescription", "B2B request failed"),
                raw_response=response_data,
            )

        except RequestException as exc:
            logger.exception("MpesaDarajaProvider HTTP error during b2b paybill")

            return ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", dict)(),
            )

        except Exception as exc:
            logger.exception("MpesaDarajaProvider b2b paybill exception")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def refund(
        self,
        *,
        provider_transaction_id: str,
        amount: Decimal,
        payload: dict,
    ) -> ProviderResult:
        try:
            shortcode = self.config["business_shortcode"]
            initiator = self.config["initiator_name"]
            security_credential = self.config["security_credential"]

            request_payload = {
                "Initiator": initiator,
                "SecurityCredential": security_credential,
                "CommandID": "TransactionReversal",
                "TransactionID": provider_transaction_id,
                "Amount": int(amount),
                "ReceiverParty": shortcode,
                "ReceiverIdentifierType": "4",
                "ResultURL": payload.get("callback_url", ""),
                "QueueTimeOutURL": payload.get("callback_url", ""),
                "Remarks": payload.get("remarks", "Refund"),
                "Occasion": payload.get("occasion", ""),
            }

            url = f"{self.base_url}/mpesa/reversal/v1/request"

            response = requests.post(
                url,
                json=request_payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            response_data = response.json()

            if response_data.get("ResponseCode") == "0":
                return ProviderResult(
                    status=ProviderResultStatus.PENDING,
                    provider_transaction_id=provider_transaction_id,
                    provider_reference=response_data.get("ConversationID", ""),
                    raw_response=response_data,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code=response_data.get("ResponseCode"),
                failure_reason=response_data.get("ResponseDescription"),
                raw_response=response_data,
            )

        except RequestException as exc:
            logger.exception("MpesaDarajaProvider HTTP error during refund")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def query_status(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        try:
            shortcode = self.config["business_shortcode"]

            initiator = self.config.get("initiator_name")
            security_credential = self.config.get("security_credential")

            payload = {
                "Initiator": initiator,
                "SecurityCredential": security_credential,
                "CommandID": "TransactionStatusQuery",
                "TransactionID": provider_transaction_id,
                "PartyA": shortcode,
                "IdentifierType": "4",
                "ResultURL": payload.get("callback_url", ""),
                "QueueTimeOutURL": payload.get("callback_url", ""),
                "Remarks": "Status Query",
                "Occasion": "",
            }

            url = f"{self.base_url}/mpesa/transactionstatus/v1/query"

            response = requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            response_data = response.json()

            response_code = response_data.get("ResponseCode")

            if response_code == "0":
                return ProviderResult(
                    status=ProviderResultStatus.PENDING,
                    provider_transaction_id=provider_transaction_id,
                    provider_reference=response_data.get("ConversationID", ""),
                    raw_response=response_data,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code=response_data.get("ResponseCode", "unknown"),
                failure_reason=response_data.get("ResponseDescription", "Status query failed"),
                raw_response=response_data,
            )

        except RequestException as exc:
            logger.exception("MpesaDarajaProvider HTTP error during query_status")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", dict)(),
            )

        except Exception as exc:
            logger.exception("MpesaDarajaProvider query_status exception")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        source_ip = payload.get("_source_ip")
        allowed_networks = self.config.get("callback_allowed_ips", [])
        if not source_ip or not allowed_networks:
            return False
        try:
            address = ipaddress.ip_address(source_ip)
            return any(
                address in ipaddress.ip_network(network, strict=False)
                for network in allowed_networks
            )
        except ValueError:
            return False

    @staticmethod
    def _result_parameter(result: dict, key: str) -> str:
        parameters = result.get("ResultParameters", {}).get("ResultParameter", [])
        if isinstance(parameters, dict):
            parameters = [parameters]
        for parameter in parameters:
            if parameter.get("Key") == key:
                return str(parameter.get("Value", ""))
        return ""

    def parse_callback(self, *, payload: dict) -> ProviderResult:
        payload = {
            key: value for key, value in payload.items() if key not in {"_raw_body", "_source_ip"}
        }
        # STK PUSH CALLBACK
        if "Body" in payload and "stkCallback" in payload["Body"]:
            stk = payload["Body"]["stkCallback"]

            result_code = stk.get("ResultCode")
            checkout_id = stk.get("CheckoutRequestID")
            merchant_id = stk.get("MerchantRequestID")

            if result_code == 0:
                return ProviderResult(
                    status=ProviderResultStatus.SUCCESS,
                    provider_transaction_id=checkout_id,
                    provider_reference=merchant_id,
                    raw_response=payload,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_reference=checkout_id,
                failure_code=str(result_code),
                failure_reason=stk.get("ResultDesc"),
                raw_response=payload,
            )

        # TRANSACTION STATUS / REVERSAL RESULT
        if "Result" in payload:
            result = payload["Result"]

            result_code = result.get("ResultCode")
            conversation_id = result.get("ConversationID")
            originator_conversation_id = result.get("OriginatorConversationID", "")
            transaction_receipt = self._result_parameter(result, "TransactionReceipt")

            if result_code == 0:
                return ProviderResult(
                    status=ProviderResultStatus.SUCCESS,
                    provider_transaction_id=transaction_receipt,
                    provider_reference=conversation_id or originator_conversation_id,
                    raw_response=payload,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_reference=conversation_id or originator_conversation_id,
                failure_code=str(result_code),
                failure_reason=result.get("ResultDesc"),
                raw_response=payload,
            )

        # QUEUE TIMEOUT
        if "ResultCode" in payload and payload.get("ResultType") == 1:
            return ProviderResult(
                status=ProviderResultStatus.PENDING,
                raw_response=payload,
            )

        return ProviderResult(
            status=ProviderResultStatus.UNKNOWN,
            failure_code="unknown_callback_format",
            raw_response=payload,
        )
