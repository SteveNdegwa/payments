from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from core.services.payment_services import PaymentServices
from utils.extended_request import ExtendedRequest
from utils.response_provider import ResponseProvider


@require_http_methods(["POST"])
def initiate_payment(
    request: ExtendedRequest,
    system_slug: str,
    chargeable_event_slug: str,
) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    payment = PaymentServices.initialize_payment(
        source_system=request.api_client,
        system_slug=system_slug,
        chargeable_event_slug=chargeable_event_slug,
        amount=amount,
        currency=request.data.get("currency"),
        payment_method_token_id=request.data.get("payment_method_token_id"),
        payment_payload=request.data.get("payment_payload"),
        external_reference=request.data.get("external_reference"),
        idempotency_key=request.data.get("idempotency_key"),
    )

    return ResponseProvider.success(
        message="Payment initiated successfully",
        data={
            "payment_intent_id": str(payment.id),
            "status": payment.status,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "next_action": payment.next_action,
        },
    )


@require_http_methods(["POST"])
def capture_payment(request: ExtendedRequest, payment_intent_id: str) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    PaymentServices.queue_capture(
        payment_intent_id=payment_intent_id,
        amount=amount,
    )
    return ResponseProvider.success(message="Capture queued successfully")


@require_http_methods(["POST"])
def void_payment(request: ExtendedRequest, payment_intent_id: str) -> JsonResponse:
    PaymentServices.queue_void(payment_intent_id=payment_intent_id)
    return ResponseProvider.success(message="Void queued successfully")


@require_http_methods(["POST"])
def refund_payment(request: ExtendedRequest, payment_intent_id: str) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    PaymentServices.queue_refund(
        payment_intent_id=payment_intent_id,
        amount=amount,
    )
    return ResponseProvider.success(message="Refund queued successfully")


@require_http_methods(["POST"])
def provider_callback(
    request: ExtendedRequest,
    provider_slug: str,
    transaction_id: str,
) -> JsonResponse:
    PaymentServices.receive_provider_callback(
        provider_slug=provider_slug,
        transaction_id=transaction_id,
        headers=dict(request.headers),
        raw_payload=request.data,
    )
    return ResponseProvider.success(message="Received")
