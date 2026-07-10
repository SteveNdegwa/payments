import logging
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from core.models import (
    PaymentIntent,
    Transaction,
    TransactionStateLog,
)
from core.providers.base_provider import BaseProvider, ProviderResult, ProviderResultStatus
from core.services.registry import get_provider_instance

logger = logging.getLogger(__name__)


def txn_status_from_result(result: ProviderResult) -> str:
    return {
        ProviderResultStatus.SUCCESS: Transaction.Status.SUCCESS,
        ProviderResultStatus.PENDING: Transaction.Status.PENDING,
        ProviderResultStatus.REQUIRES_ACTION: Transaction.Status.REQUIRES_ACTION,
        ProviderResultStatus.FAILED: Transaction.Status.FAILED,
        ProviderResultStatus.UNKNOWN: Transaction.Status.FAILED,
    }[result.status]


def intent_status_after_txn(
    intent: PaymentIntent,
    txn_type: str,
    txn_status: str,
) -> str | None:
    """
    Map a completed transaction onto its parent PaymentIntent status.
    Returns None if the intent status should not change.
    """
    if txn_status == Transaction.Status.FAILED:
        return PaymentIntent.Status.FAILED

    if txn_status == Transaction.Status.REQUIRES_ACTION:
        return PaymentIntent.Status.REQUIRES_ACTION

    if txn_status == Transaction.Status.PENDING:
        return PaymentIntent.Status.PROCESSING

    # txn_status == SUCCESS
    if txn_type == Transaction.Type.PAYMENT:
        return PaymentIntent.Status.SETTLED
    if txn_type == Transaction.Type.AUTHORIZATION:
        return PaymentIntent.Status.AUTHORIZED
    if txn_type == Transaction.Type.CAPTURE:
        new_captured = intent.amount_captured or 0
        if new_captured >= intent.amount:
            return PaymentIntent.Status.CAPTURED
        return PaymentIntent.Status.PARTIALLY_CAPTURED
    if txn_type == Transaction.Type.VOID:
        return PaymentIntent.Status.CANCELLED
    if txn_type in (Transaction.Type.REFUND, Transaction.Type.PARTIAL_REFUND):
        new_refunded = intent.amount_refunded or 0
        if new_refunded >= intent.amount_captured:
            return PaymentIntent.Status.REFUNDED
        return PaymentIntent.Status.PARTIALLY_REFUNDED

    return None


class TransactionExecutor:
    def __init__(
        self,
        *,
        payment_intent: PaymentIntent,
        transaction_type: str,
        amount: Decimal,
        request_payload: dict,
        actor: str = "celery",
        # For captures, voids, and refunds — the provider id from the original txn
        parent_provider_transaction_id: str = "",
    ):
        self.payment_intent = payment_intent
        self.transaction_type = transaction_type
        self.provider_account = payment_intent.chargeable_event.provider_account
        self.amount = amount
        self.request_payload = request_payload
        self.actor = actor
        self.parent_provider_transaction_id = parent_provider_transaction_id

    def run(self) -> Transaction:
        txn = self._create_transaction()
        try:
            provider = self._load_provider()
            self._inject_callback_url(txn)
            result = self._call_provider(provider, txn)
        except Exception as exc:
            logger.exception("Failed to prepare provider call for TXN-%s", txn.id)
            result = ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="internal_error",
                failure_reason=str(exc),
            )
        txn = self._persist_result(txn, result)
        return txn

    def _inject_callback_url(self, txn: Transaction) -> None:
        base_url = settings.BASE_URL
        # base_url = os.getenv("BASE_URL")
        if not base_url:
            raise ValueError("BASE_URL environment variable is not configured.")
        base_url = base_url.strip().rstrip("/")
        callback_path = f"/core/payments/callbacks/{txn.provider.slug}/{txn.id}/"
        self.request_payload["callback_url"] = urljoin(f"{base_url}/", callback_path.lstrip("/"))

    def _create_transaction(self) -> Transaction:
        with db_transaction.atomic():
            txn = Transaction.objects.create(
                payment_intent=self.payment_intent,
                transaction_type=self.transaction_type,
                provider=self.provider_account.provider,
                provider_account=self.provider_account,
                amount=self.amount,
                currency=self.payment_intent.currency,
                request_payload=self.request_payload,
                status=Transaction.Status.QUEUED,
            )
            TransactionStateLog.objects.create(
                transaction=txn,
                from_status="",
                to_status=Transaction.Status.QUEUED,
                actor=self.actor,
                reason="Transaction created and queued",
            )
        return txn

    def _load_provider(self) -> BaseProvider:
        pa = self.provider_account
        return get_provider_instance(
            class_name=pa.provider.class_name,
            credentials=pa.credentials,
            config=pa.extra_config,
        )

    def _call_provider(self, provider: BaseProvider, txn: Transaction) -> ProviderResult:
        self._transition(txn, Transaction.Status.PROCESSING, reason="Calling provider")
        try:
            return self._dispatch(provider)
        except Exception as exc:
            logger.exception("Unhandled exception calling provider for TXN-%s", txn.id)
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="internal_error",
                failure_reason=str(exc),
            )

    def _dispatch(self, provider: BaseProvider) -> ProviderResult:
        t = self.transaction_type
        pi = self.payment_intent

        if t == Transaction.Type.PAYMENT:
            return provider.charge(
                amount=self.amount,
                currency=pi.currency,
                payload=self.request_payload,
            )
        if t == Transaction.Type.AUTHORIZATION:
            return provider.authorize(
                amount=self.amount,
                currency=pi.currency,
                payload=self.request_payload,
            )
        if t == Transaction.Type.CAPTURE:
            return provider.capture(
                provider_transaction_id=self.parent_provider_transaction_id,
                amount=self.amount,
                payload=self.request_payload,
            )
        if t in (Transaction.Type.REFUND, Transaction.Type.PARTIAL_REFUND):
            return provider.refund(
                provider_transaction_id=self.parent_provider_transaction_id,
                amount=self.amount,
                payload=self.request_payload,
            )
        if t == Transaction.Type.VOID:
            return provider.void(
                provider_transaction_id=self.parent_provider_transaction_id,
                payload=self.request_payload,
            )

        raise ValueError(f"Unsupported transaction_type: {t}")

    def _persist_result(self, txn: Transaction, result: ProviderResult) -> Transaction:
        new_txn_status = txn_status_from_result(result)

        with db_transaction.atomic():
            old_txn_status = txn.status

            # Update Transaction
            txn.status = new_txn_status
            txn.provider_transaction_id = result.provider_transaction_id
            txn.provider_reference = result.provider_reference
            txn.response_payload = result.raw_response
            txn.failure_code = result.failure_code
            txn.failure_reason = result.failure_reason

            # Schedule reconciliation for async pending results
            if new_txn_status in (Transaction.Status.PENDING, Transaction.Status.REQUIRES_ACTION):
                timeout = self.provider_account.provider.reconciliation_timeout_seconds
                txn.reconciliation_due_at = timezone.now() + timedelta(seconds=timeout)

            txn.save()

            # Audit log
            TransactionStateLog.objects.create(
                transaction=txn,
                from_status=old_txn_status,
                to_status=new_txn_status,
                actor=self.actor,
                reason=result.failure_reason or "Provider response received",
            )

            # Update PaymentIntent amounts first, then derive status
            pi = self.payment_intent
            if new_txn_status == Transaction.Status.SUCCESS:
                t = self.transaction_type
                if t == Transaction.Type.AUTHORIZATION:
                    pi.amount_authorized = (pi.amount_authorized or 0) + self.amount
                elif t in (Transaction.Type.PAYMENT, Transaction.Type.CAPTURE):
                    pi.amount_captured = (pi.amount_captured or 0) + (
                        result.amount_processed or self.amount
                    )
                elif t in (Transaction.Type.REFUND, Transaction.Type.PARTIAL_REFUND):
                    pi.amount_refunded = (pi.amount_refunded or 0) + self.amount

            if result.next_action:
                pi.next_action = result.next_action

            # Derive intent status with freshly updated amounts
            new_intent_status = intent_status_after_txn(pi, self.transaction_type, new_txn_status)
            if new_intent_status:
                pi.status = new_intent_status

            pi.save(
                update_fields=[
                    "amount_authorized",
                    "amount_captured",
                    "amount_refunded",
                    "next_action",
                    "status",
                ]
            )

        return txn

    def _transition(self, txn: Transaction, to_status: str, reason: str = ""):
        old = txn.status
        txn.status = to_status
        txn.save(update_fields=["status"])
        TransactionStateLog.objects.create(
            transaction=txn,
            from_status=old,
            to_status=to_status,
            actor=self.actor,
            reason=reason,
        )
