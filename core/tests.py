from decimal import Decimal
from unittest.mock import Mock, PropertyMock, patch

from django.test import SimpleTestCase

from core.providers.base_provider import ProviderResultStatus
from core.providers.mpesa_daraja_provider import MpesaDarajaProvider


class MpesaDarajaProviderTests(SimpleTestCase):
    def setUp(self):
        self.credentials = {
            "consumer_key": "consumer-key",
            "consumer_secret": "consumer-secret",
            "business_shortcode": "123456",
            "base_url": "https://sandbox.safaricom.co.ke",
            "initiator_name": "testapi",
            "security_credential": "encrypted-credential",
        }

    @patch.object(MpesaDarajaProvider, "headers", new_callable=PropertyMock)
    @patch("core.providers.mpesa_daraja_provider.requests.post")
    def test_charge_supports_b2b_paybill_flow(self, mock_post, mock_headers):
        mock_headers.return_value = {
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        }
        response = Mock()
        response.json.return_value = {
            "ResponseCode": "0",
            "ResponseDescription": "Accept the service request successfully.",
            "ConversationID": "AG_20260718_123",
            "OriginatorConversationID": "originator-123",
        }
        mock_post.return_value = response

        provider = MpesaDarajaProvider(credentials=self.credentials)
        result = provider.charge(
            amount=Decimal("1000"),
            currency="KES",
            payload={
                "daraja_flow": "b2b_paybill",
                "destination_shortcode": "654321",
                "account_reference": "INV-123",
                "remarks": "Supplier payment",
                "callback_url": "https://example.com/callback",
            },
        )

        self.assertEqual(result.status, ProviderResultStatus.PENDING)
        self.assertEqual(result.provider_transaction_id, "AG_20260718_123")
        self.assertEqual(result.provider_reference, "originator-123")

        mock_post.assert_called_once()
        url = mock_post.call_args.kwargs["url"] if "url" in mock_post.call_args.kwargs else None
        if url is None:
            url = mock_post.call_args.args[0]
        self.assertEqual(url, "https://sandbox.safaricom.co.ke/mpesa/b2b/v1/paymentrequest")

        request_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(request_payload["CommandID"], "BusinessPayBill")
        self.assertEqual(request_payload["SenderIdentifierType"], "4")
        self.assertEqual(request_payload["RecieverIdentifierType"], "4")
        self.assertEqual(request_payload["PartyA"], "123456")
        self.assertEqual(request_payload["PartyB"], "654321")
        self.assertEqual(request_payload["ResultURL"], "https://example.com/callback")
        self.assertEqual(request_payload["QueueTimeOutURL"], "https://example.com/callback")

    def test_charge_rejects_unknown_daraja_flow(self):
        provider = MpesaDarajaProvider(credentials=self.credentials)

        result = provider.charge(
            amount=Decimal("1000"),
            currency="KES",
            payload={"daraja_flow": "unknown"},
        )

        self.assertEqual(result.status, ProviderResultStatus.FAILED)
        self.assertEqual(result.failure_code, "unsupported_daraja_flow")

    def test_parse_b2b_result_callback_extracts_receipt(self):
        provider = MpesaDarajaProvider(credentials=self.credentials)

        result = provider.parse_callback(
            payload={
                "Result": {
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "OriginatorConversationID": "originator-123",
                    "ConversationID": "AG_20260718_123",
                    "ResultParameters": {
                        "ResultParameter": [
                            {"Key": "TransactionReceipt", "Value": "TGI2J9ABCD"},
                            {"Key": "TransactionAmount", "Value": 1000},
                        ]
                    },
                }
            }
        )

        self.assertEqual(result.status, ProviderResultStatus.SUCCESS)
        self.assertEqual(result.provider_transaction_id, "TGI2J9ABCD")
        self.assertEqual(result.provider_reference, "AG_20260718_123")
