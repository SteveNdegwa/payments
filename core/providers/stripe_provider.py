import logging
from decimal import Decimal

import stripe

from core.providers.base_provider import BaseProvider, ProviderResult, ProviderResultStatus
from core.services.registry import register_provider

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "succeeded": ProviderResultStatus.SUCCESS,
    "processing": ProviderResultStatus.PENDING,
    "requires_payment_method": ProviderResultStatus.FAILED,
    "requires_confirmation": ProviderResultStatus.REQUIRES_ACTION,
    "requires_action": ProviderResultStatus.REQUIRES_ACTION,
    "canceled": ProviderResultStatus.FAILED,
    "requires_capture": ProviderResultStatus.SUCCESS
}


def _to_stripe_amount(amount: Decimal, currency: str) -> int:
    """Convert decimal amount to Stripe's smallest-unit integer."""
    zero_decimal = {"JPY", "KRW", "VND"}
    if currency.upper() in zero_decimal:
        return int(amount)
    return int(amount * 100)


def _build_result(intent: dict) -> ProviderResult:
    raw_status = intent.get("status", "")
    status = _STATUS_MAP.get(raw_status, ProviderResultStatus.UNKNOWN)

    next_action = None
    if status == ProviderResultStatus.REQUIRES_ACTION:
        action = intent.get("next_action") or {}
        if action.get("type") == "redirect_to_url":
            next_action = {
                "type": "redirect",
                "url": action["redirect_to_url"]["url"],
            }
        elif action.get("type") == "use_stripe_sdk":
            next_action = {
                "type": "stripe_sdk",
                "client_secret": intent.get("client_secret"),
            }

    last_charge = None
    if intent.get("latest_charge"):
        charges = intent.get("charges", {}).get("data", [])
        last_charge = charges[0] if charges else None

    failure_code = ""
    failure_reason = ""
    if last_charge:
        failure_code = last_charge.get("failure_code") or ""
        failure_reason = last_charge.get("failure_message") or ""

    return ProviderResult(
        status=status,
        provider_transaction_id=intent.get("id", ""),
        provider_reference=intent.get("payment_method", "") or "",
        raw_response=intent,
        failure_code=failure_code,
        failure_reason=failure_reason,
        next_action=next_action,
    )


@register_provider("core.providers.stripe_provider.StripeProvider")
class StripeProvider(BaseProvider):

    def __init__(self, credentials: dict, config: dict | None = None):
        super().__init__(credentials, config)
        self._client = stripe.StripeClient(api_key=credentials["secret_key"])

    @staticmethod
    def _amount_int(amount: Decimal, currency: str) -> int:
        return _to_stripe_amount(amount, currency)

    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        try:
            intent = self._client.payment_intents.create(
                {
                    "amount": self._amount_int(amount, currency),
                    "currency": currency.lower(),
                    "payment_method": payload["payment_method"],
                    "confirm": True,
                    "capture_method": "automatic",
                    "return_url": payload.get("return_url", ""),
                    "customer": payload.get("customer"),
                    "description": payload.get("description", ""),
                    "metadata": payload.get("metadata", {}),
                }
            )
            return _build_result(dict(intent))
        except stripe.StripeError as exc:
            logger.exception("Stripe charge failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=exc.code or "stripe_error",
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def authorize(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        try:
            intent = self._client.payment_intents.create(
                {
                    "amount": self._amount_int(amount, currency),
                    "currency": currency.lower(),
                    "payment_method": payload["payment_method"],
                    "confirm": True,
                    "capture_method": "manual",
                    "return_url": payload.get("return_url", ""),
                    "customer": payload.get("customer"),
                    "metadata": payload.get("metadata", {}),
                }
            )
            return _build_result(dict(intent))
        except stripe.StripeError as exc:
            logger.exception("Stripe authorize failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=exc.code or "stripe_error",
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def capture(self, *, provider_transaction_id: str, amount: Decimal, payload: dict) -> ProviderResult:
        try:
            currency = payload.get("currency", "usd")
            intent = self._client.payment_intents.capture(
                provider_transaction_id,
                {"amount_to_capture": self._amount_int(amount, currency)},
            )
            return _build_result(dict(intent))
        except stripe.StripeError as exc:
            logger.exception("Stripe capture failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=exc.code or "stripe_error",
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def void(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        try:
            intent = self._client.payment_intents.cancel(provider_transaction_id)
            return _build_result(dict(intent))
        except stripe.StripeError as exc:
            logger.exception("Stripe void failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=exc.code or "stripe_error",
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def refund(self, *, provider_transaction_id: str, amount: Decimal, payload: dict) -> ProviderResult:
        try:
            currency = payload.get("currency", "usd")
            refund = self._client.refunds.create(
                {
                    "payment_intent": provider_transaction_id,
                    "amount": self._amount_int(amount, currency),
                    "reason": payload.get("reason", "requested_by_customer"),
                }
            )
            refund_dict = dict(refund)
            status = (
                ProviderResultStatus.SUCCESS
                if refund_dict.get("status") == "succeeded"
                else ProviderResultStatus.FAILED
            )
            return ProviderResult(
                status=status,
                provider_transaction_id=refund_dict.get("id", ""),
                provider_reference=provider_transaction_id,
                raw_response=refund_dict,
            )
        except stripe.StripeError as exc:
            logger.exception("Stripe refund failed")
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code=exc.code or "stripe_error",
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def query_status(self, *, provider_transaction_id: str) -> ProviderResult:
        try:
            intent = self._client.payment_intents.retrieve(provider_transaction_id)
            return _build_result(dict(intent))
        except stripe.StripeError as exc:
            return ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                failure_reason=str(exc),
                raw_response=exc.json_body or {},
            )

    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        sig_header = headers.get("stripe-signature", "")
        webhook_secret = self.credentials.get("webhook_secret", "")
        if not sig_header or not webhook_secret:
            return False
        try:
            # payload here is raw bytes/string
            stripe.WebhookSignature.verify_header(
                payload.get("_raw_body", ""),
                sig_header,
                webhook_secret,
            )
            return True
        except stripe.error.SignatureVerificationError:
            return False

    def parse_callback(self, *, payload: dict) -> ProviderResult:
        event_type = payload.get("type", "")
        obj = payload.get("data", {}).get("object", {})

        if event_type.startswith("payment_intent."):
            return _build_result(obj)
        if event_type.startswith("charge.refund"):
            status = (
                ProviderResultStatus.SUCCESS
                if obj.get("status") == "succeeded"
                else ProviderResultStatus.FAILED
            )
            return ProviderResult(
                status=status,
                provider_transaction_id=obj.get("id", ""),
                raw_response=obj,
            )
        return ProviderResult(status=ProviderResultStatus.UNKNOWN, raw_response=payload)