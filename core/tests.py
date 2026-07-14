from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib import admin
from django.test import SimpleTestCase, TestCase

from .admin import (
    ChargeableEventAdmin,
    PaymentMethodTokenAdmin,
    ProviderAccountAdmin,
    ProviderAdmin,
    WebhookDeliveryLogAdmin,
)
from .models import (
    ChargeableEvent,
    PaymentIntent,
    PaymentMethodToken,
    PaymentMethodType,
    Provider,
    ProviderAccount,
    System,
    Transaction,
    WebhookDeliveryLog,
)
from .providers.base_provider import BaseProvider, ProviderResult, ProviderResultStatus
from .providers.mpesa_daraja_provider import MpesaDarajaProvider
from .services.executor import TransactionExecutor
from .services.payment_services import PaymentServices


class AdminDisplayFormattingTests(SimpleTestCase):
    def test_provider_capability_columns_render_html(self):
        model_admin = ProviderAdmin(Provider, admin.site)
        provider = Provider(is_async=True, supports_refund=True, supports_3ds=True)

        self.assertHTMLEqual(
            str(model_admin.is_async_colored(provider)),
            '<span style="color:#6c757d">Yes</span>',
        )
        self.assertHTMLEqual(
            str(model_admin.supports_refund_colored(provider)),
            '<span style="color:#28a745">Yes</span>',
        )
        self.assertHTMLEqual(
            str(model_admin.supports_3ds_colored(provider)),
            '<span style="color:#17a2b8">Yes</span>',
        )

    def test_default_and_active_columns_render_html(self):
        provider_account_admin = ProviderAccountAdmin(ProviderAccount, admin.site)
        chargeable_event_admin = ChargeableEventAdmin(ChargeableEvent, admin.site)
        payment_token_admin = PaymentMethodTokenAdmin(PaymentMethodToken, admin.site)

        self.assertHTMLEqual(
            str(provider_account_admin.is_default_colored(ProviderAccount(is_default=True))),
            '<span style="color:#28a745">Yes</span>',
        )
        self.assertHTMLEqual(
            str(chargeable_event_admin.is_active_colored(ChargeableEvent(is_active=False))),
            '<span style="color:#dc3545">No</span>',
        )
        self.assertHTMLEqual(
            str(payment_token_admin.is_active_colored(PaymentMethodToken(is_active=True))),
            '<span style="color:#28a745">Yes</span>',
        )

    def test_webhook_delivery_null_status_renders_html(self):
        model_admin = WebhookDeliveryLogAdmin(WebhookDeliveryLog, admin.site)

        self.assertHTMLEqual(
            str(model_admin.response_status_colored(WebhookDeliveryLog(response_status_code=None))),
            '<span style="color:#6c757d">\u2014</span>',
        )


class MpesaDarajaProviderTests(SimpleTestCase):
    def test_base_provider_payment_operations_default_to_unsupported(self):
        class CallbackOnlyProvider(BaseProvider):
            def verify_callback(self, *, headers: dict, payload: dict) -> bool:
                return False

            def parse_callback(self, *, payload: dict) -> ProviderResult:
                return ProviderResult(status=ProviderResultStatus.UNKNOWN)

        provider = CallbackOnlyProvider(credentials={})

        result = provider.refund(provider_transaction_id="txn-1", amount=Decimal("10"), payload={})

        self.assertEqual(result.status, ProviderResultStatus.FAILED)
        self.assertEqual(result.failure_code, "unsupported_operation")
        self.assertEqual(
            result.failure_reason,
            "CallbackOnlyProvider does not support refund.",
        )

    def test_card_only_flows_return_unsupported_operation_failure(self):
        provider = MpesaDarajaProvider(
            credentials={
                "consumer_key": "key",
                "consumer_secret": "secret",
                "business_shortcode": "123456",
                "business_passkey": "passkey",
                "base_url": "https://example.test",
            }
        )

        result = provider.authorize(amount=100, currency="KES", payload={})

        self.assertEqual(result.status, ProviderResultStatus.FAILED)
        self.assertEqual(result.failure_code, "unsupported_operation")

    @patch("core.providers.mpesa_daraja_provider.requests.post")
    @patch("core.providers.mpesa_daraja_provider.requests.get")
    def test_charge_returns_stk_push_next_action(self, mock_get, mock_post):
        mock_get.return_value = Mock(
            json=Mock(return_value={"access_token": "token"}),
            raise_for_status=Mock(),
        )
        mock_post.return_value = Mock(
            json=Mock(
                return_value={
                    "MerchantRequestID": "merchant-123",
                    "CheckoutRequestID": "checkout-123",
                    "CustomerMessage": "Success. Request accepted for processing",
                }
            ),
            raise_for_status=Mock(),
        )
        provider = MpesaDarajaProvider(
            credentials={
                "consumer_key": "key",
                "consumer_secret": "secret",
                "business_shortcode": "123456",
                "business_passkey": "passkey",
                "base_url": "https://example.test",
            }
        )

        result = provider.charge(
            amount=Decimal("100"),
            currency="KES",
            payload={
                "phone_number": "254700000000",
                "account_reference": "INV-1",
                "callback_url": "https://example.test/callback",
            },
        )

        self.assertEqual(result.status, ProviderResultStatus.REQUIRES_ACTION)
        self.assertEqual(result.provider_transaction_id, "checkout-123")
        self.assertEqual(
            result.next_action,
            {
                "type": "mobile_money_stk_push",
                "message": "Success. Request accepted for processing",
            },
        )


class TransactionExecutorFailureTests(TestCase):
    def _create_payment_intent(self, *, class_name: str, next_action: dict | None = None):
        system = System.objects.create(
            name="Test System",
            slug="test-system",
            webhook_url="https://example.test/webhooks",
        )
        method_type = PaymentMethodType.objects.create(
            code=PaymentMethodType.Code.MOBILE_MONEY,
            name="Mobile Money",
        )
        provider = Provider.objects.create(
            name="Broken Provider",
            slug="broken-provider",
            class_name=class_name,
            payment_method_type=method_type,
        )
        provider_account = ProviderAccount.objects.create(
            provider=provider,
            name="Default",
            credentials={},
            is_default=True,
        )
        event = ChargeableEvent.objects.create(
            system=system,
            name="Test Charge",
            slug="test-charge",
            provider=provider,
            provider_account=provider_account,
            flow=ChargeableEvent.Flow.CHARGE,
        )
        intent = PaymentIntent.objects.create(
            system=system,
            source_system=system,
            chargeable_event=event,
            amount=100,
            currency="KES",
            idempotency_key="intent-1",
            status=PaymentIntent.Status.INITIATED,
            next_action=next_action,
        )
        return intent

    def test_provider_load_error_marks_transaction_and_intent_failed(self):
        intent = self._create_payment_intent(
            class_name="core.providers.missing.DoesNotExist",
            next_action={"type": "mobile_money_stk_push"},
        )
        txn = TransactionExecutor(
            payment_intent=intent,
            transaction_type=Transaction.Type.PAYMENT,
            amount=intent.amount,
            request_payload={},
            actor="test",
        ).run()

        intent.refresh_from_db()
        txn.refresh_from_db()

        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(txn.status, Transaction.Status.FAILED)
        self.assertEqual(txn.failure_code, "internal_error")
        self.assertEqual(intent.status, PaymentIntent.Status.FAILED)
        self.assertIsNone(intent.next_action)

    def test_callback_failure_clears_existing_next_action(self):
        intent = self._create_payment_intent(
            class_name="core.providers.mpesa_daraja_provider.MpesaDarajaProvider",
            next_action={"type": "mobile_money_stk_push"},
        )
        intent.status = PaymentIntent.Status.REQUIRES_ACTION
        intent.save(update_fields=["status"])
        txn = Transaction.objects.create(
            payment_intent=intent,
            transaction_type=Transaction.Type.PAYMENT,
            provider=intent.chargeable_event.provider,
            provider_account=intent.chargeable_event.provider_account,
            amount=intent.amount,
            currency=intent.currency,
            provider_transaction_id="checkout-123",
            status=Transaction.Status.REQUIRES_ACTION,
        )

        PaymentServices.apply_provider_result(
            txn=txn,
            result=ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="stk_cancelled",
                failure_reason="Customer cancelled the STK push.",
                raw_response={"ResultCode": "1032"},
            ),
            actor="webhook",
        )

        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.FAILED)
        self.assertIsNone(intent.next_action)
