from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
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
