from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    # Initiation
    path(
        "payments/<str:system_slug>/<str:chargeable_event_slug>/initiate/",
        views.initiate_payment,
        name="initiate-payment",
    ),

    # Post-authorization operations
    path(
        "payments/<str:payment_intent_id>/capture/",
        views.capture_payment,
        name="capture-payment",
    ),
    path(
        "payments/<str:payment_intent_id>/void/",
        views.void_payment,
        name="void-payment",
    ),
    path(
        "payments/<str:payment_intent_id>/refund/",
        views.refund_payment,
        name="refund-payment",
    ),

    # Provider callbacks
    path(
        "payments/callbacks/<str:provider_slug>/<str:transaction_id>/",
        views.provider_callback,
        name="provider-callback",
    ),
]