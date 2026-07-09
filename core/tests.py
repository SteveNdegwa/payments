from django.contrib import admin
from django.test import SimpleTestCase

from .admin import (
    ChargeableEventAdmin,
    PaymentMethodTokenAdmin,
    ProviderAccountAdmin,
    ProviderAdmin,
    WebhookDeliveryLogAdmin,
)
from .models import (
    ChargeableEvent,
    PaymentMethodToken,
    Provider,
    ProviderAccount,
    WebhookDeliveryLog,
)


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
