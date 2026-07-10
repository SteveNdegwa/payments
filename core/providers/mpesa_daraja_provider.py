import base64
import logging
from datetime import datetime
from decimal import Decimal

import requests
from django.core.exceptions import ValidationError
from requests import RequestException
from requests.auth import HTTPBasicAuth

from core.providers.base_provider import BaseProvider, ProviderResult, ProviderResultStatus
from core.services.registry import register_provider
from utils.common import check_required_fields

logger = logging.getLogger(__name__)


@register_provider("core.providers.mpesa_daraja_provider.MpesaDarajaProvider")
class MpesaDarajaProvider(BaseProvider):
    @staticmethod
    def _unsupported_operation(operation: str) -> ProviderResult:
        return ProviderResult(
            status=ProviderResultStatus.FAILED,
            failure_code="unsupported_operation",
            failure_reason=f"M-Pesa Daraja does not support {operation}.",
        )

    def _validate_credentials(self) -> None:
        required_fields = {
            "consumer_key",
            "consumer_secret",
            "business_shortcode",
            "business_passkey",
            "base_url",
            # "initiator_name",
            # "security_credential",
        }
        missing_fields = [field for field in required_fields if not self.credentials.get(field)]
        if missing_fields:
            raise ValidationError(
                f"Daraja config missing required fields: {', '.join(missing_fields)}"
            )

    @property
    def token(self) -> str:
        base_url = self.credentials.get("base_url")
        url = f"{base_url}/oauth/v1/generate?grant_type=client_credentials"
        consumer_key = self.credentials.get("consumer_key")
        consumer_secret = self.credentials.get("consumer_secret")
        response = requests.get(url, auth=HTTPBasicAuth(consumer_key, consumer_secret))
        response.raise_for_status()
        return response.json()["access_token"]

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        try:
            self._validate_credentials()
            check_required_fields(
                payload=payload,
                required_fields={
                    "phone_number",
                    "account_reference",
                    "callback_url",
                },
            )

            shortcode = self.credentials["business_shortcode"]
            passkey = self.credentials["business_passkey"]
            base_url = self.credentials["base_url"]

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
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

            url = f"{base_url.rstrip('/')}/mpesa/stkpush/v1/processrequest"

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
                    "message": "Enter your M-Pesa PIN on your phone to complete payment.",
                },
            )

        except RequestException as exc:
            logger.exception(f"MpesaDarajaProvider HTTP error during charge: {exc}")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", lambda: {})(),
            )

        except Exception as exc:
            logger.exception(f"MpesaDarajaProvider charge exception: {exc}")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def authorize(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        return self._unsupported_operation("authorization")

    def capture(
        self, *, provider_transaction_id: str, amount: Decimal, payload: dict
    ) -> ProviderResult:
        return self._unsupported_operation("capture")

    def void(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        return self._unsupported_operation("void")

    def refund(
        self,
        *,
        provider_transaction_id: str,
        amount: Decimal,
        payload: dict,
    ) -> ProviderResult:
        try:
            shortcode = self.credentials["business_shortcode"]
            base_url = self.credentials["base_url"]
            initiator = self.credentials["initiator_name"]
            security_credential = self.credentials["security_credential"]

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

            url = f"{base_url.rstrip('/')}/mpesa/reversal/v1/request"

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
            logger.exception(f"MpesaDarajaProvider HTTP error during refund: {exc}")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def query_status(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        try:
            shortcode = self.credentials["business_shortcode"]
            base_url = self.credentials["base_url"]

            initiator = self.credentials.get("initiator_name")
            security_credential = self.credentials.get("security_credential")

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

            url = f"{base_url.rstrip('/')}/mpesa/transactionstatus/v1/query"

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
            logger.exception(f"MpesaDarajaProvider HTTP error during query_status: {exc}")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", lambda: {})(),
            )

        except Exception as exc:
            logger.exception(f"MpesaDarajaProvider query_status exception: {exc}")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_transaction_id=provider_transaction_id,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )

    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        return True

    def parse_callback(self, *, payload: dict) -> ProviderResult:
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

            if result_code == 0:
                return ProviderResult(
                    status=ProviderResultStatus.SUCCESS,
                    provider_reference=conversation_id,
                    raw_response=payload,
                )

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                provider_reference=conversation_id,
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
