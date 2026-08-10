import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import InboundPayment, System


class InboundPaymentStatusTests(TestCase):
    api_key = "inbound-status-key"

    @classmethod
    def setUpTestData(cls):
        cls.system = System.objects.create(
            name="Status Client",
            slug="status-client",
            hashed_api_key=cls.api_key,
            webhook_url="https://example.com/webhooks",
        )
        cls.inbound_payment = InboundPayment.objects.create(
            system=cls.system,
            source_system=cls.system,
            amount=Decimal("125.000000"),
            currency="KES",
            idempotency_key="status-query",
            external_reference="ORDER-125",
            status=InboundPayment.Status.PROCESSING,
        )
        cls.url = reverse(
            "api:core:inbound-payment-status",
            kwargs={"inbound_payment_id": cls.inbound_payment.id},
        )

    def test_returns_inbound_payment_status(self):
        response = self.client.get(self.url, headers={"X-Api-Key": self.api_key})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["message"], "Inbound payment retrieved")
        self.assertEqual(payload["error"], "")
        self.assertEqual(
            payload["data"]["inbound_payment_id"], str(self.inbound_payment.id)
        )
        self.assertEqual(payload["data"]["status"], InboundPayment.Status.PROCESSING)
        self.assertNotIn("latest_transaction", payload["data"])

    def test_rejects_missing_api_key(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {
                "success": False,
                "message": "Unauthorized",
                "data": {},
                "error": "Missing API key",
            },
        )

    def test_hides_payment_from_unrelated_system(self):
        other_api_key = "other-status-key"
        System.objects.create(
            name="Other Client",
            slug="other-client",
            hashed_api_key=other_api_key,
            webhook_url="https://other.example.com/webhooks",
        )

        response = self.client.get(self.url, headers={"X-Api-Key": other_api_key})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_rejects_non_get_methods(self):
        response = self.client.post(
            self.url,
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-Api-Key": self.api_key},
        )

        self.assertEqual(response.status_code, 405)
