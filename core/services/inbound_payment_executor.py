import logging
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from core.models import (
    InboundPayment,
    InboundPaymentTransaction,
    InboundPaymentTransactionStateLog,
)
from core.providers.base_provider import (
    BaseProvider,
    ProviderResult,
    ProviderResultStatus,
)
from core.services.registry import get_provider_instance

logger = logging.getLogger(__name__)


def inbound_payment_transaction_status_from_result(result: ProviderResult) -> str:
    return {
        ProviderResultStatus.SUCCESS: InboundPaymentTransaction.Status.SUCCESS,
        ProviderResultStatus.PENDING: InboundPaymentTransaction.Status.PENDING,
        ProviderResultStatus.REQUIRES_ACTION: InboundPaymentTransaction.Status.REQUIRES_ACTION,
        ProviderResultStatus.FAILED: InboundPaymentTransaction.Status.FAILED,
        ProviderResultStatus.UNKNOWN: InboundPaymentTransaction.Status.FAILED,
    }[result.status]


def inbound_payment_status_after_transaction(
    inbound_payment: InboundPayment,
    operation_type: str,
    transaction_status: str,
) -> str | None:
    """
    Map an inbound payment transaction result onto its parent payment status.

    Return ``None`` when the parent status should not change.
    """
    if transaction_status == InboundPaymentTransaction.Status.FAILED:
        return InboundPayment.Status.FAILED

    if transaction_status == InboundPaymentTransaction.Status.REQUIRES_ACTION:
        return InboundPayment.Status.REQUIRES_ACTION

    if transaction_status == InboundPaymentTransaction.Status.PENDING:
        return InboundPayment.Status.PROCESSING

    if operation_type == InboundPaymentTransaction.Operation.COLLECTION:
        return InboundPayment.Status.SETTLED
    if operation_type == InboundPaymentTransaction.Operation.AUTHORIZATION:
        return InboundPayment.Status.AUTHORIZED
    if operation_type == InboundPaymentTransaction.Operation.CAPTURE:
        new_captured = inbound_payment.amount_captured or 0
        if new_captured >= inbound_payment.amount:
            return InboundPayment.Status.CAPTURED
        return InboundPayment.Status.PARTIALLY_CAPTURED
    if operation_type == InboundPaymentTransaction.Operation.VOID:
        return InboundPayment.Status.CANCELLED
    if operation_type in (
        InboundPaymentTransaction.Operation.REFUND,
        InboundPaymentTransaction.Operation.PARTIAL_REFUND,
    ):
        new_refunded = inbound_payment.amount_refunded or 0
        if new_refunded >= inbound_payment.amount_captured:
            return InboundPayment.Status.REFUNDED
        return InboundPayment.Status.PARTIALLY_REFUNDED

    return None


def apply_next_action_from_result(
    inbound_payment: InboundPayment,
    result: ProviderResult,
    new_inbound_payment_status: str | None,
) -> None:
    if result.next_action:
        inbound_payment.next_action = result.next_action
    elif (
        new_inbound_payment_status
        and new_inbound_payment_status != InboundPayment.Status.REQUIRES_ACTION
    ):
        inbound_payment.next_action = None


class InboundPaymentTransactionExecutor:
    def __init__(
        self,
        *,
        inbound_payment: InboundPayment,
        operation_type: str,
        amount: Decimal,
        provider_payload: dict,
        actor: str = "celery",
        # For captures, voids, and refunds: the provider ID from the original transaction.
        parent_provider_transaction_id: str = "",
    ):
        self.inbound_payment = inbound_payment
        self.operation_type = operation_type
        self.provider_account = inbound_payment.inbound_payment_event.provider_account
        self.amount = amount
        self.provider_payload = provider_payload
        self.actor = actor
        self.parent_provider_transaction_id = parent_provider_transaction_id

    def run(self) -> InboundPaymentTransaction:
        inbound_payment_transaction = self._create_transaction()
        try:
            provider = self._load_provider()
            self._inject_callback_url(inbound_payment_transaction)
            result = self._call_provider(provider, inbound_payment_transaction)
        except Exception as exc:
            logger.exception(
                "Failed to prepare provider call for inbound payment transaction %s",
                inbound_payment_transaction.id,
            )
            result = ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="internal_error",
                failure_reason=str(exc),
            )
        return self._persist_result(inbound_payment_transaction, result)

    def _inject_callback_url(self, txn: InboundPaymentTransaction) -> None:
        base_url = settings.BASE_URL
        # base_url = os.getenv("BASE_URL")
        if not base_url:
            raise ValueError("BASE_URL environment variable is not configured.")
        base_url = base_url.strip().rstrip("/")
        callback_path = f"/core/inbound-payments/callbacks/{txn.provider.slug}/{txn.id}/"
        self.provider_payload["callback_url"] = urljoin(f"{base_url}/", callback_path.lstrip("/"))

    def _create_transaction(self) -> InboundPaymentTransaction:
        with db_transaction.atomic():
            txn = InboundPaymentTransaction.objects.create(
                inbound_payment=self.inbound_payment,
                operation_type=self.operation_type,
                provider=self.provider_account.provider,
                provider_account=self.provider_account,
                amount=self.amount,
                currency=self.inbound_payment.currency,
                provider_payload=self.provider_payload,
                status=InboundPaymentTransaction.Status.QUEUED,
            )
            InboundPaymentTransactionStateLog.objects.create(
                inbound_payment_transaction=txn,
                from_status="",
                to_status=InboundPaymentTransaction.Status.QUEUED,
                actor=self.actor,
                reason="Inbound payment transaction created and queued",
            )
        return txn

    def _load_provider(self) -> BaseProvider:
        pa = self.provider_account
        return get_provider_instance(
            class_name=pa.provider.class_name,
            config=pa.get_config(),
        )

    def _call_provider(
        self, provider: BaseProvider, txn: InboundPaymentTransaction
    ) -> ProviderResult:
        self._transition(
            txn, InboundPaymentTransaction.Status.PROCESSING, reason="Calling provider"
        )
        try:
            return self._dispatch(provider)
        except Exception as exc:
            logger.exception(
                "Unhandled exception calling provider for inbound payment transaction %s",
                txn.id,
            )
            return ProviderResult(
                status=ProviderResultStatus.FAILED,
                failure_code="internal_error",
                failure_reason=str(exc),
            )

    def _dispatch(self, provider: BaseProvider) -> ProviderResult:
        operation_type = self.operation_type
        inbound_payment = self.inbound_payment

        if operation_type == InboundPaymentTransaction.Operation.COLLECTION:
            return provider.charge(
                amount=self.amount,
                currency=inbound_payment.currency,
                payload=self.provider_payload,
            )
        if operation_type == InboundPaymentTransaction.Operation.AUTHORIZATION:
            return provider.authorize(
                amount=self.amount,
                currency=inbound_payment.currency,
                payload=self.provider_payload,
            )
        if operation_type == InboundPaymentTransaction.Operation.CAPTURE:
            return provider.capture(
                provider_transaction_id=self.parent_provider_transaction_id,
                amount=self.amount,
                payload=self.provider_payload,
            )
        if operation_type in (
            InboundPaymentTransaction.Operation.REFUND,
            InboundPaymentTransaction.Operation.PARTIAL_REFUND,
        ):
            return provider.refund(
                provider_transaction_id=self.parent_provider_transaction_id,
                amount=self.amount,
                payload=self.provider_payload,
            )
        if operation_type == InboundPaymentTransaction.Operation.VOID:
            return provider.void(
                provider_transaction_id=self.parent_provider_transaction_id,
                payload=self.provider_payload,
            )

        raise ValueError(f"Unsupported operation_type: {operation_type}")

    def _persist_result(
        self, txn: InboundPaymentTransaction, result: ProviderResult
    ) -> InboundPaymentTransaction:
        new_transaction_status = inbound_payment_transaction_status_from_result(result)

        with db_transaction.atomic():
            old_transaction_status = txn.status

            # Update InboundPaymentTransaction
            txn.status = new_transaction_status
            txn.provider_transaction_id = result.provider_transaction_id
            txn.provider_reference = result.provider_reference
            txn.request_payload = result.request_payload
            txn.response_payload = result.raw_response
            txn.failure_code = result.failure_code
            txn.failure_reason = result.failure_reason

            # Schedule reconciliation for async pending results
            if new_transaction_status in (
                InboundPaymentTransaction.Status.PENDING,
                InboundPaymentTransaction.Status.REQUIRES_ACTION,
            ):
                timeout = self.provider_account.provider.reconciliation_timeout_seconds
                txn.reconciliation_due_at = timezone.now() + timedelta(seconds=timeout)

            txn.save()

            # Audit log
            InboundPaymentTransactionStateLog.objects.create(
                inbound_payment_transaction=txn,
                from_status=old_transaction_status,
                to_status=new_transaction_status,
                actor=self.actor,
                reason=result.failure_reason or "Provider response received",
            )

            # Update InboundPayment amounts first, then derive status
            inbound_payment = self.inbound_payment
            if new_transaction_status == InboundPaymentTransaction.Status.SUCCESS:
                operation_type = self.operation_type
                if operation_type == InboundPaymentTransaction.Operation.AUTHORIZATION:
                    inbound_payment.amount_authorized = (
                        inbound_payment.amount_authorized or 0
                    ) + self.amount
                elif operation_type in (
                    InboundPaymentTransaction.Operation.COLLECTION,
                    InboundPaymentTransaction.Operation.CAPTURE,
                ):
                    inbound_payment.amount_captured = (inbound_payment.amount_captured or 0) + (
                        result.amount_processed or self.amount
                    )
                elif operation_type in (
                    InboundPaymentTransaction.Operation.REFUND,
                    InboundPaymentTransaction.Operation.PARTIAL_REFUND,
                ):
                    inbound_payment.amount_refunded = (
                        inbound_payment.amount_refunded or 0
                    ) + self.amount

            new_inbound_payment_status = inbound_payment_status_after_transaction(
                inbound_payment,
                self.operation_type,
                new_transaction_status,
            )
            if new_inbound_payment_status:
                inbound_payment.status = new_inbound_payment_status
            apply_next_action_from_result(
                inbound_payment,
                result,
                new_inbound_payment_status,
            )

            inbound_payment.save(
                update_fields=[
                    "amount_authorized",
                    "amount_captured",
                    "amount_refunded",
                    "next_action",
                    "status",
                ]
            )

        return txn

    def _transition(self, txn: InboundPaymentTransaction, to_status: str, reason: str = ""):
        old = txn.status
        txn.status = to_status
        txn.save(update_fields=["status"])
        InboundPaymentTransactionStateLog.objects.create(
            inbound_payment_transaction=txn,
            from_status=old,
            to_status=to_status,
            actor=self.actor,
            reason=reason,
        )
