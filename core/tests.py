import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.models import (
    InboundPayment,
    InboundPaymentEvent,
    InboundPaymentEventProviderConfig,
    LedgerAccount,
    OutboundTransferEvent,
    OutboundTransferEventProviderConfig,
    PaymentMethodType,
    Provider,
    ProviderAccount,
    ProviderConfigDefinition,
    System,
)
from core.services.exceptions import InboundPaymentError, OutboundTransferError
from core.services.inbound_payment_service import InboundPaymentService
from core.services.outbound_transfer_service import OutboundTransferService


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
        self.assertEqual(payload["data"]["inbound_payment_id"], str(self.inbound_payment.id))
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


class OrganizationProviderRoutingTests(TestCase):
    api_key = "provider-routing-key"

    @classmethod
    def setUpTestData(cls):
        cls.system = System.objects.create(
            name="IAMS",
            slug="idms",
            hashed_api_key=cls.api_key,
            webhook_url="https://example.com/webhooks",
        )
        cls.mobile_money = PaymentMethodType.objects.create(
            code=PaymentMethodType.Code.MOBILE_MONEY,
            name="Mobile Money",
        )
        cls.default_provider = Provider.objects.create(
            name="Default Provider",
            slug="default-provider",
            class_name="DefaultProvider",
        )
        cls.default_provider.payment_method_types.add(cls.mobile_money)
        cls.override_provider = Provider.objects.create(
            name="Organization Provider",
            slug="organization-provider",
            class_name="OrganizationProvider",
        )
        cls.override_provider.payment_method_types.add(cls.mobile_money)
        ProviderConfigDefinition.objects.create(
            provider=cls.override_provider,
            name="API key",
            key="api_key",
            value_type=ProviderConfigDefinition.ValueType.TEXT,
            is_secret=True,
        )
        ProviderConfigDefinition.objects.create(
            provider=cls.override_provider,
            name="Retries",
            key="retries",
            value_type=ProviderConfigDefinition.ValueType.INTEGER,
        )
        cls.default_account = ProviderAccount.objects.create(
            provider=cls.default_provider,
            name="Default account",
        )
        cls.event = InboundPaymentEvent.objects.create(
            system=cls.system,
            name="Onboarding payment",
            slug="onboarding-payment",
            fixed_amount=Decimal("100.00"),
        )
        cls.global_config = InboundPaymentEventProviderConfig.objects.create(
            inbound_payment_event=cls.event,
            provider=cls.default_provider,
            provider_account=cls.default_account,
        )

    def test_provider_discovery_and_override_creation(self):
        list_response = self.client.get(
            reverse("api:core:provider-list"),
            {"payment_method_type": "mobile_money"},
            headers={"X-Api-Key": self.api_key},
        )
        self.assertEqual(list_response.status_code, 200)
        slugs = {item["slug"] for item in list_response.json()["data"]["providers"]}
        self.assertEqual(slugs, {"default-provider", "organization-provider"})

        detail_response = self.client.get(
            reverse(
                "api:core:provider-detail",
                kwargs={"provider_slug": self.override_provider.slug},
            ),
            headers={"X-Api-Key": self.api_key},
        )
        definitions = detail_response.json()["data"]["provider"]["config_definitions"]
        self.assertEqual({item["key"] for item in definitions}, {"api_key", "retries"})

        create_response = self.client.post(
            reverse("api:core:organization-override-create"),
            data=json.dumps(
                {
                    "event_slug": self.event.slug,
                    "event_type": "payment",
                    "organization_remote_code": "agency-1",
                    "provider_slug": self.override_provider.slug,
                    "configs": {"api_key": "secret", "retries": 3},
                }
            ),
            content_type="application/json",
            headers={"X-Api-Key": self.api_key},
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        override = InboundPaymentEventProviderConfig.objects.get(
            inbound_payment_event=self.event,
            organization_remote_code="agency-1",
        )
        self.assertEqual(override.provider, self.override_provider)
        self.assertEqual(override.provider_account.organization_remote_code, "agency-1")
        self.assertEqual(
            override.provider_account.get_config(),
            {"api_key": "secret", "retries": 3},
        )

        config_list_response = self.client.get(
            reverse("api:core:organization-provider-config-list"),
            {"organization_remote_code": "agency-1"},
            headers={"X-Api-Key": self.api_key},
        )
        config_id = config_list_response.json()["data"]["provider_configs"][0]["id"]
        config_detail_response = self.client.get(
            reverse(
                "api:core:organization-provider-config-detail",
                kwargs={"config_id": config_id},
            ),
            {"organization_remote_code": "agency-1"},
            headers={"X-Api-Key": self.api_key},
        )
        self.assertEqual(config_detail_response.status_code, 200)
        provider_config = config_detail_response.json()["data"]["provider_config"]
        self.assertEqual(
            provider_config["provider"]["slug"],
            self.override_provider.slug,
        )
        self.assertIsNone(provider_config["configs"]["api_key"]["value"])
        self.assertTrue(provider_config["configs"]["api_key"]["is_configured"])

    def test_provider_configuration_endpoints_require_api_key(self):
        response = self.client.get(reverse("api:core:provider-list"))

        self.assertEqual(response.status_code, 401)

    @patch("core.tasks.task_collect_inbound_payment.apply_async")
    def test_inactive_override_blocks_global_fallback(self, apply_async):
        account = ProviderAccount.objects.create(
            provider=self.override_provider,
            name="Inactive organization account",
            organization_remote_code="blocked-org",
        )
        InboundPaymentEventProviderConfig.objects.create(
            inbound_payment_event=self.event,
            organization_remote_code="blocked-org",
            provider=self.override_provider,
            provider_account=account,
            is_active=False,
        )

        with self.assertRaisesMessage(InboundPaymentError, "is inactive"):
            InboundPaymentService.initialize_inbound_payment(
                source_system=self.system,
                system_slug=self.system.slug,
                inbound_payment_event_slug=self.event.slug,
                organization_remote_code="blocked-org",
                idempotency_key="blocked-payment",
            )

        apply_async.assert_not_called()

    @patch("core.tasks.task_execute_outbound_transfer.apply_async")
    def test_inactive_transfer_override_blocks_global_fallback(self, apply_async):
        debit_account = LedgerAccount.objects.create(
            code="TRANSFER-DEBIT",
            name="Transfer debit",
            account_type=LedgerAccount.AccountType.ASSET,
            currency="KES",
            system=self.system,
        )
        credit_account = LedgerAccount.objects.create(
            code="TRANSFER-CREDIT",
            name="Transfer credit",
            account_type=LedgerAccount.AccountType.LIABILITY,
            currency="KES",
            system=self.system,
        )
        transfer_event = OutboundTransferEvent.objects.create(
            system=self.system,
            name="Organization payout",
            slug="organization-payout",
            recipient_type=OutboundTransferEvent.RecipientType.B2C,
            payment_method_type=self.mobile_money,
            currency="KES",
        )
        global_account = ProviderAccount.objects.create(
            provider=self.default_provider,
            name="Global transfer account",
            outbound_transfer_debit_account=debit_account,
            outbound_transfer_credit_account=credit_account,
        )
        OutboundTransferEventProviderConfig.objects.create(
            outbound_transfer_event=transfer_event,
            provider=self.default_provider,
            provider_account=global_account,
        )
        blocked_account = ProviderAccount.objects.create(
            provider=self.override_provider,
            name="Blocked organization transfer account",
            organization_remote_code="blocked-org",
            outbound_transfer_debit_account=debit_account,
            outbound_transfer_credit_account=credit_account,
        )
        OutboundTransferEventProviderConfig.objects.create(
            outbound_transfer_event=transfer_event,
            organization_remote_code="blocked-org",
            provider=self.override_provider,
            provider_account=blocked_account,
            is_active=False,
        )

        with self.assertRaisesMessage(OutboundTransferError, "is inactive"):
            OutboundTransferService.initialize_outbound_transfer(
                source_system=self.system,
                system_slug=self.system.slug,
                outbound_transfer_event_slug=transfer_event.slug,
                amount=Decimal("50.00"),
                provider_payload={},
                external_reference="blocked-transfer",
                idempotency_key="blocked-transfer",
                organization_remote_code="blocked-org",
            )

        apply_async.assert_not_called()
