from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    # Provider configuration
    path("providers/", views.provider_list, name="provider-list"),
    path("providers/<slug:provider_slug>/", views.provider_detail, name="provider-detail"),
    path(
        "organization-overrides/",
        views.organization_override_list,
        name="organization-override-list",
    ),
    path(
        "organization-overrides/create/",
        views.create_organization_override,
        name="organization-override-create",
    ),
    path(
        "organization-provider-configs/",
        views.organization_provider_config_list,
        name="organization-provider-config-list",
    ),
    path(
        "organization-provider-configs/<str:config_id>/",
        views.organization_provider_config_detail,
        name="organization-provider-config-detail",
    ),
    # Outbound transfers
    path(
        "outbound-transfers/<str:system_slug>/<str:outbound_transfer_event_slug>/initiate/",
        views.initiate_outbound_transfer,
        name="initiate-outbound-transfer",
    ),
    path(
        "outbound-transfers/<str:outbound_transfer_id>/status/",
        views.outbound_transfer_status,
        name="outbound-transfer-status",
    ),
    path(
        "outbound-transfers/callbacks/<str:provider_slug>/<str:outbound_transfer_id>/",
        views.outbound_transfer_callback,
        name="outbound-transfer-callback",
    ),
    # Initiation
    path(
        "inbound-payments/<str:system_slug>/<str:inbound_payment_event_slug>/initiate/",
        views.initiate_inbound_payment,
        name="initiate-inbound-payment",
    ),
    path(
        "inbound-payments/<str:inbound_payment_id>/status/",
        views.inbound_payment_status,
        name="inbound-payment-status",
    ),
    # Post-authorization operations
    path(
        "inbound-payments/<str:inbound_payment_id>/capture/",
        views.capture_inbound_payment,
        name="capture-inbound-payment",
    ),
    path(
        "inbound-payments/<str:inbound_payment_id>/void/",
        views.void_inbound_payment,
        name="void-inbound-payment",
    ),
    path(
        "inbound-payments/<str:inbound_payment_id>/refund/",
        views.refund_inbound_payment,
        name="refund-inbound-payment",
    ),
    # Provider callbacks
    path(
        "inbound-payments/callbacks/<str:provider_slug>/<str:inbound_payment_transaction_id>/",
        views.inbound_payment_callback,
        name="inbound-payment-callback",
    ),
]
