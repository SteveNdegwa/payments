import logging
from decimal import Decimal

import requests
from django.core.exceptions import ValidationError
from requests import RequestException
from requests.auth import HTTPBasicAuth
from datetime import datetime
import base64

from core.providers.base_provider import BaseProvider, ProviderResult, ProviderResultStatus
from utils.common import check_required_fields

logger = logging.getLogger(__name__)


class MpesaDarajaProvider(BaseProvider):
    def _validate_config(self) -> None:
        required_fields = {
            "consumer_key",
            "consumer_secret",
            "business_shortcode",
            "business_passkey",
            "base_url"
        }
        missing_fields = [
            field for field in required_fields if self.config.get(field)
        ]
        if missing_fields:
            raise ValidationError(
                f"Daraja config missing required fields: {", ".join(missing_fields)}"
            )

    @property
    def token(self) -> str:
        base_url = self.config.get("base_url")
        url = f"{base_url}/oauth/v1/generate?grant_type=client_credentials"
        consumer_key = self.config.get("consumer_key")
        consumer_secret = self.config.get("consumer_secret")
        response = requests.get(url, auth=HTTPBasicAuth(consumer_key, consumer_secret))
        response.raise_for_status()
        return response.json()["access_token"]

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def charge(self, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        try:
            check_required_fields(
                payload=payload,
                required_fields={
                    "phone_number",
                    "account_reference",
                    "callback_url",
                },
            )

            shortcode = self.config.get("business_shortcode")
            passkey = self.config.get("business_passkey")
            base_url = self.config.get("base_url")

            if not all([shortcode, passkey, base_url]):
                raise ValueError("Missing required Daraja configuration.")

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            password = base64.b64encode(
                f"{shortcode}{passkey}{timestamp}".encode()
            ).decode()

            phone_number = payload["phone_number"]
            callback_url = payload["callback_url"]
            account_reference = payload["account_reference"]
            transaction_desc = payload.get("transaction_desc", "Payment")

            request_payload = {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(amount),  # Daraja expects integer
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
                status=ProviderResultStatus.PENDING,
                provider_reference=response_data.get("CheckoutRequestID"),
                raw_response=response_data,
            )

        except RequestException as exc:
            logger.exception("MpesaDarajaProvider HTTP error during charge")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="http_error",
                failure_reason=str(exc),
                raw_response=getattr(exc.response, "json", lambda: {})(),
            )

        except Exception as exc:
            logger.exception("MpesaDarajaProvider charge exception")

            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="provider_error",
                failure_reason=str(exc),
                raw_response={},
            )


    def stk_push(self, phone_number, amount, callback_url, account_reference="Ref001", transaction_desc="Payment"):
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        shortcode = self.config.get("business_shortcode")
        passkey = self.config.get("business_passkey")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount,
            "PartyA": phone_number,
            "PartyB": shortcode,
            "PhoneNumber": phone_number,
            "CallBackURL": callback_url,
            "AccountReference": account_reference,
            "TransactionDesc": transaction_desc
        }
        url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"
        response = requests.post(url, json=payload, headers=self.headers)
        return response.json(), payload

    def register_c2b_urls(self, validation_url, confirmation_url, response_type="Completed"):
        payload = {
            "ShortCode": self.shortcode,
            "ResponseType": response_type,
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url
        }
        url = f"{self.base_url}/mpesa/c2b/v1/registerurl"
        response = requests.post(url, json=payload, headers=self.get_headers())
        return response.json()

    def transaction_status(self, transaction_id, party_a, identifier_type="4", remarks="Check", occasion=""):
        payload = {
            "Initiator": "testapi",
            "SecurityCredential": "SECURITY_CREDENTIAL",
            "CommandID": "TransactionStatusQuery",
            "TransactionID": transaction_id,
            "PartyA": party_a,
            "IdentifierType": identifier_type,
            "ResultURL": "https://yourdomain.com/path/to/result",
            "QueueTimeOutURL": "https://yourdomain.com/path/to/timeout",
            "Remarks": remarks,
            "Occasion": occasion
        }

        url = f"{self.base_url}/mpesa/transactionstatus/v1/query"
        response = requests.post(url, json=payload, headers=self.get_headers())
        return response.json()

    def reversal(self, transaction_id, amount, receiver_party, remarks="Reversal", occasion=""):
        payload = {
            "Initiator": "testapi",
            "SecurityCredential": "SECURITY_CREDENTIAL",
            "CommandID": "TransactionReversal",
            "TransactionID": transaction_id,
            "Amount": amount,
            "ReceiverParty": receiver_party,
            "ReceiverIdentifierType": "11",
            "ResultURL": "https://spinmobile.co/path/to/result",
            "QueueTimeOutURL": "https://spinmobile.co/path/to/timeout",
            "Remarks": remarks,
            "Occasion": occasion
        }

        url = f"{self.base_url}/mpesa/reversal/v1/request"
        response = requests.post(url, json=payload, headers=self.get_headers())
        return response.json()
