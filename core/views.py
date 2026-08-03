from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from core.services.inbound_payment_service import InboundPaymentService
from core.services.outbound_transfer_service import OutboundTransferService
from utils.extended_request import ExtendedRequest
from utils.response_provider import ResponseProvider


@require_http_methods(["POST"])
def initiate_inbound_payment(
    request: ExtendedRequest,
    system_slug: str,
    inbound_payment_event_slug: str,
) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    inbound_payment = InboundPaymentService.initialize_inbound_payment(
        source_system=request.api_client,
        system_slug=system_slug,
        inbound_payment_event_slug=inbound_payment_event_slug,
        amount=amount,
        currency=request.data.get("currency"),
        payment_method_token_id=request.data.get("payment_method_token_id"),
        provider_payload=request.data.get("provider_payload"),
        external_reference=request.data.get("external_reference"),
        idempotency_key=request.data.get("idempotency_key"),
    )

    return ResponseProvider.success(
        message="Inbound payment initiated successfully",
        data={
            "inbound_payment_id": str(inbound_payment.id),
            "status": inbound_payment.status,
            "amount": str(inbound_payment.amount),
            "currency": inbound_payment.currency,
            "next_action": inbound_payment.next_action,
        },
    )


@require_http_methods(["POST"])
def capture_inbound_payment(request: ExtendedRequest, inbound_payment_id: str) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    InboundPaymentService.queue_capture(
        inbound_payment_id=inbound_payment_id,
        amount=amount,
    )
    return ResponseProvider.success(message="Capture queued successfully")


@require_http_methods(["POST"])
def void_inbound_payment(request: ExtendedRequest, inbound_payment_id: str) -> JsonResponse:
    InboundPaymentService.queue_void(inbound_payment_id=inbound_payment_id)
    return ResponseProvider.success(message="Void queued successfully")


@require_http_methods(["POST"])
def refund_inbound_payment(request: ExtendedRequest, inbound_payment_id: str) -> JsonResponse:
    amount = None
    raw_amount = request.data.get("amount")
    if raw_amount is not None:
        try:
            amount = Decimal(str(raw_amount))
        except InvalidOperation:
            return ResponseProvider.bad_request(message="Invalid amount format")

    InboundPaymentService.queue_refund(
        inbound_payment_id=inbound_payment_id,
        amount=amount,
    )
    return ResponseProvider.success(message="Refund queued successfully")


@require_http_methods(["POST"])
def inbound_payment_callback(
    request: ExtendedRequest,
    provider_slug: str,
    inbound_payment_transaction_id: str,
) -> JsonResponse:
    callback_payload = dict(request.data)
    callback_payload["_raw_body"] = request.body.decode("utf-8")
    callback_payload["_source_ip"] = request.client_ip
    InboundPaymentService.receive_callback(
        provider_slug=provider_slug,
        inbound_payment_transaction_id=inbound_payment_transaction_id,
        headers=dict(request.headers),
        raw_payload=callback_payload,
    )
    return ResponseProvider.success(message="Received")


@require_http_methods(["POST"])
def initiate_outbound_transfer(
    request: ExtendedRequest,
    system_slug: str,
    outbound_transfer_event_slug: str,
) -> JsonResponse:
    event_managed_fields = {
        "provider",
        "provider_account",
        "provider_slug",
        "recipient_type",
        "payment_method_type",
        "currency",
    }
    supplied_event_managed_fields = sorted(event_managed_fields.intersection(request.data))
    if supplied_event_managed_fields:
        return ResponseProvider.bad_request(
            message="Outbound transfer routing is configured by the event",
            error=(f"Remove event-managed fields: {', '.join(supplied_event_managed_fields)}."),
        )

    try:
        amount = Decimal(str(request.data.get("amount")))
    except (InvalidOperation, TypeError):
        return ResponseProvider.bad_request(message="Invalid amount format")

    outbound_transfer = OutboundTransferService.initialize_outbound_transfer(
        source_system=request.api_client,
        system_slug=system_slug,
        outbound_transfer_event_slug=outbound_transfer_event_slug,
        amount=amount,
        provider_payload=request.data.get("provider_payload") or {},
        external_reference=request.data.get("external_reference"),
        idempotency_key=request.data.get("idempotency_key", ""),
    )
    return ResponseProvider.accepted(
        message="Outbound transfer queued",
        data={
            "outbound_transfer_id": str(outbound_transfer.id),
            "outbound_transfer_event": outbound_transfer.outbound_transfer_event.slug,
            "status": outbound_transfer.status,
            "recipient_type": outbound_transfer.recipient_type,
            "payment_method_type": outbound_transfer.payment_method_type.code,
            "amount": str(outbound_transfer.amount),
            "currency": outbound_transfer.currency,
        },
    )


@require_http_methods(["GET"])
def outbound_transfer_status(request: ExtendedRequest, outbound_transfer_id: str) -> JsonResponse:
    outbound_transfer = OutboundTransferService.get_outbound_transfer(
        outbound_transfer_id=outbound_transfer_id,
        system=request.api_client,
    )
    return ResponseProvider.success(
        message="Outbound transfer retrieved",
        data={
            "outbound_transfer_id": str(outbound_transfer.id),
            "status": outbound_transfer.status,
            "recipient_type": outbound_transfer.recipient_type,
            "payment_method_type": outbound_transfer.payment_method_type.code,
            "amount": str(outbound_transfer.amount),
            "currency": outbound_transfer.currency,
            "external_reference": outbound_transfer.external_reference,
            "provider_transaction_id": outbound_transfer.provider_transaction_id,
            "failure_code": outbound_transfer.failure_code,
            "failure_reason": outbound_transfer.failure_reason,
        },
    )


@require_http_methods(["POST"])
def outbound_transfer_callback(
    request: ExtendedRequest,
    provider_slug: str,
    outbound_transfer_id: str,
) -> JsonResponse:
    callback_payload = dict(request.data)
    callback_payload["_raw_body"] = request.body.decode("utf-8")
    callback_payload["_source_ip"] = request.client_ip
    OutboundTransferService.receive_callback(
        provider_slug=provider_slug,
        outbound_transfer_id=outbound_transfer_id,
        headers=dict(request.headers),
        raw_payload=callback_payload,
    )
    return ResponseProvider.success(message="Received")
