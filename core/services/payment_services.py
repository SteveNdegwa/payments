import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from core.models import (
    ChargeableEvent,
    PaymentIntent,
    PaymentMethodToken,
    Provider,
    ProviderCallbackLog,
    ReconciliationRecord,
    System,
    Transaction,
    TransactionStateLog,
    WebhookOutbox,
)
from core.services.executor import TransactionExecutor
from core.services.registry import get_provider_instance

logger = logging.getLogger(__name__)


class PaymentError(Exception):
    """Raised on invalid state transitions or unsupported operations."""


class PaymentServices:
    @classmethod
    def initialize_payment(
        cls,
        *,
        source_system: System,
        system_slug: str,
        chargeable_event_slug: str,
        amount: float | None = None,
        currency: str | None = None,
        # Provider-specific data: phone number, return_url, etc.
        payment_payload: dict | None = None,
        payment_method_token_id: str | None = None,
        external_reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        idempotency_key = idempotency_key or str(uuid.uuid4())

        system = cls._get_system(system_slug)
        event = cls._get_chargeable_event(system, chargeable_event_slug)

        resolved_amount = amount or event.fixed_amount
        resolved_currency = currency or event.currency

        if not resolved_amount:
            raise ValidationError(
                "No amount provided and the chargeable event does not define a fixed amount."
            )
        if system.max_transaction_amount and resolved_amount > system.max_transaction_amount:
            raise ValidationError(
                f"Amount {resolved_amount} exceeds the system maximum of "
                f"{system.max_transaction_amount}."
            )
        if system.allowed_currencies and resolved_currency not in system.allowed_currencies:
            raise ValidationError(
                f"Currency '{resolved_currency}' is not allowed for system '{system.name}'."
            )

        token = None
        if payment_method_token_id:
            try:
                token = PaymentMethodToken.objects.get(
                    id=payment_method_token_id, system=system, is_active=True
                )
            except PaymentMethodToken.DoesNotExist as err:
                raise ObjectDoesNotExist(
                    f"Payment method token '{payment_method_token_id}' not found."
                ) from err

        with db_transaction.atomic():
            pi, created = PaymentIntent.objects.get_or_create(
                system=system,
                idempotency_key=idempotency_key,
                defaults={
                    "source_system": source_system,
                    "chargeable_event": event,
                    "payment_method_token": token,
                    "amount": resolved_amount,
                    "currency": resolved_currency,
                    "payment_payload": payment_payload or {},
                    "external_reference": external_reference,
                    "status": PaymentIntent.Status.INITIATED,
                    "expires_at": timezone.now() + timedelta(hours=24),
                },
            )

        if not created:
            return pi

        cls._queue_first_task(pi)

        return pi

    @classmethod
    def _queue_first_task(cls, pi: PaymentIntent) -> None:
        from core.tasks import task_authorize, task_charge

        if pi.chargeable_event.flow == ChargeableEvent.Flow.CHARGE:
            task_charge.apply_async(kwargs={"payment_intent_id": str(pi.id)})

        elif pi.chargeable_event.flow == ChargeableEvent.Flow.AUTHORIZE_CAPTURE:
            task_authorize.apply_async(kwargs={"payment_intent_id": str(pi.id)})

    @classmethod
    def queue_capture(
        cls,
        *,
        payment_intent_id: str,
        amount: Decimal | None = None,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_capture

        pi = cls._get_intent_for_execution(
            payment_intent_id,
            allowed_statuses=[
                PaymentIntent.Status.AUTHORIZED,
                PaymentIntent.Status.PARTIALLY_CAPTURED,
            ],
        )

        capture_amount = amount or pi.amount_remaining
        if capture_amount <= 0:
            raise PaymentError("Nothing left to capture on this payment intent.")
        if capture_amount > pi.amount_remaining:
            raise ValidationError(
                f"Capture amount {capture_amount} exceeds remaining authorized "
                f"amount {pi.amount_remaining}."
            )

        result = task_capture.apply_async(
            kwargs={
                "payment_intent_id": str(pi.id),
                "amount": str(capture_amount),
                "payload": payload or {},
            },
        )
        return result.id

    @classmethod
    def queue_void(
        cls,
        *,
        payment_intent_id: str,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_void

        pi = cls._get_intent_for_execution(
            payment_intent_id,
            allowed_statuses=[PaymentIntent.Status.AUTHORIZED],
        )

        result = task_void.apply_async(
            kwargs={"payment_intent_id": str(pi.id), "payload": payload or {}},
        )
        return result.id

    @classmethod
    def queue_refund(
        cls,
        *,
        payment_intent_id: str,
        amount: Decimal | None = None,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_refund

        pi = cls._get_intent_for_execution(
            payment_intent_id,
            allowed_statuses=[
                PaymentIntent.Status.CAPTURED,
                PaymentIntent.Status.PARTIALLY_CAPTURED,
                PaymentIntent.Status.SETTLED,
                PaymentIntent.Status.PARTIALLY_REFUNDED,
            ],
        )
        cls._require_provider_capability(pi, "supports_refund")

        refund_amount = amount or pi.amount_refundable
        if refund_amount <= 0:
            raise PaymentError("Nothing left to refund on this payment intent.")
        if refund_amount > pi.amount_refundable:
            raise ValidationError(
                f"Refund amount {refund_amount} exceeds the refundable "
                f"amount {pi.amount_refundable}."
            )
        if refund_amount < pi.amount_refundable:
            cls._require_provider_capability(pi, "supports_partial_refund")

        result = task_refund.apply_async(
            kwargs={
                "payment_intent_id": str(pi.id),
                "amount": str(refund_amount),
                "payload": payload or {},
            },
        )
        return result.id

    @classmethod
    def execute_charge(
        cls,
        *,
        payment_intent_id: str,
        extra_payload: dict,
        actor: str,
    ) -> Transaction:
        pi = cls._get_intent_for_execution(
            payment_intent_id, allowed_statuses=[PaymentIntent.Status.INITIATED]
        )
        txn = TransactionExecutor(
            payment_intent=pi,
            transaction_type=Transaction.Type.PAYMENT,
            amount=pi.amount,
            request_payload=cls._build_payload(pi, extra_payload),
            actor=actor,
        ).run()
        cls._enqueue_webhook(pi, txn)
        return txn

    @classmethod
    def execute_authorize(
        cls,
        *,
        payment_intent_id: str,
        extra_payload: dict,
        actor: str,
    ) -> Transaction:
        pi = cls._get_intent_for_execution(
            payment_intent_id, allowed_statuses=[PaymentIntent.Status.INITIATED]
        )
        txn = TransactionExecutor(
            payment_intent=pi,
            transaction_type=Transaction.Type.AUTHORIZATION,
            amount=pi.amount,
            request_payload=cls._build_payload(pi, extra_payload),
            actor=actor,
        ).run()
        cls._enqueue_webhook(pi, txn)

        # Refresh pi from DB to get the status set by the executor
        pi.refresh_from_db()

        # Auto-capture if configured and authorization succeeded
        if pi.status == PaymentIntent.Status.AUTHORIZED and pi.chargeable_event.auto_capture:
            cls.schedule_auto_capture(pi)

        return txn

    @classmethod
    def execute_capture(
        cls,
        *,
        payment_intent_id: str,
        amount: Decimal | None,
        extra_payload: dict,
        actor: str,
    ) -> Transaction:
        pi = cls._get_intent_for_execution(
            payment_intent_id,
            allowed_statuses=[
                PaymentIntent.Status.AUTHORIZED,
                PaymentIntent.Status.PARTIALLY_CAPTURED,
            ],
        )
        auth_txn = cls._get_last_successful_txn(
            pi, Transaction.Type.AUTHORIZATION, "No successful authorization found to capture."
        )
        capture_amount = amount or pi.amount_remaining
        txn = TransactionExecutor(
            payment_intent=pi,
            transaction_type=Transaction.Type.CAPTURE,
            amount=capture_amount,
            request_payload={**extra_payload, "currency": pi.currency},
            actor=actor,
            parent_provider_transaction_id=auth_txn.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(pi, txn)
        return txn

    @classmethod
    def execute_void(
        cls,
        *,
        payment_intent_id: str,
        extra_payload: dict,
        actor: str,
    ) -> Transaction:
        pi = cls._get_intent_for_execution(
            payment_intent_id, allowed_statuses=[PaymentIntent.Status.AUTHORIZED]
        )
        auth_txn = cls._get_last_successful_txn(
            pi, Transaction.Type.AUTHORIZATION, "No successful authorization found to void."
        )
        txn = TransactionExecutor(
            payment_intent=pi,
            transaction_type=Transaction.Type.VOID,
            amount=pi.amount_authorized,
            request_payload=extra_payload,
            actor=actor,
            parent_provider_transaction_id=auth_txn.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(pi, txn)
        return txn

    @classmethod
    def execute_refund(
        cls,
        *,
        payment_intent_id: str,
        amount: Decimal | None,
        extra_payload: dict,
        actor: str,
    ) -> Transaction:
        pi = cls._get_intent_for_execution(
            payment_intent_id,
            allowed_statuses=[
                PaymentIntent.Status.CAPTURED,
                PaymentIntent.Status.PARTIALLY_CAPTURED,
                PaymentIntent.Status.SETTLED,
                PaymentIntent.Status.PARTIALLY_REFUNDED,
            ],
        )
        original_txn = (
            pi.transactions.filter(
                transaction_type__in=[
                    Transaction.Type.PAYMENT,
                    Transaction.Type.CAPTURE,
                ],
                status=Transaction.Status.SUCCESS,
            )
            .order_by("-created_at")
            .first()
        )
        if not original_txn:
            raise PaymentError("No captured transaction found to refund.")

        refund_amount = amount or pi.amount_refundable
        txn_type = (
            Transaction.Type.PARTIAL_REFUND
            if refund_amount < pi.amount_captured
            else Transaction.Type.REFUND
        )
        txn = TransactionExecutor(
            payment_intent=pi,
            transaction_type=txn_type,
            amount=refund_amount,
            request_payload={**extra_payload, "currency": pi.currency},
            actor=actor,
            parent_provider_transaction_id=original_txn.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(pi, txn)
        return txn

    @classmethod
    def receive_provider_callback(
        cls,
        *,
        provider_slug: str,
        transaction_id: str,
        headers: dict,
        raw_payload: dict,
    ) -> None:
        from core.tasks import task_process_provider_callback

        try:
            provider = Provider.objects.get(slug=provider_slug, is_active=True)
        except Provider.DoesNotExist:
            logger.warning("Callback for unknown provider %s", provider_slug)
            return

        try:
            txn = Transaction.objects.get(id=transaction_id, provider=provider)
        except Transaction.DoesNotExist:
            logger.warning("Callback for non-existing transaction %s", transaction_id)
            return

        log = ProviderCallbackLog.objects.create(
            provider=provider,
            transaction=txn,
            raw_headers=headers,
            raw_payload=raw_payload,
            status=ProviderCallbackLog.Status.RECEIVED,
        )

        task_process_provider_callback.apply_async(
            kwargs={"callback_log_id": str(log.id)},
        )

    @classmethod
    def process_provider_callback(cls, log: ProviderCallbackLog) -> None:
        txn = log.transaction
        provider = log.provider
        account = txn.provider_account

        if txn.is_terminal:
            log.status = ProviderCallbackLog.Status.IGNORED
            log.processing_error = f"Ignored: transaction is already terminal ({txn.status})"
            log.save(update_fields=["status", "processing_error"])
            return

        provider_instance = get_provider_instance(
            class_name=provider.class_name,
            credentials=account.credentials,
            config=account.extra_config,
        )

        # Verify signature
        if not provider_instance.verify_callback(headers=log.raw_headers, payload=log.raw_payload):
            log.status = ProviderCallbackLog.Status.REJECTED
            log.processing_error = "Invalid signature"
            log.save(update_fields=["status", "processing_error"])
            return

        result = provider_instance.parse_callback(payload=log.raw_payload)

        log.parsed_status = str(result.status.value)
        log.save(update_fields=["parsed_status"])

        # if result.status in (ProviderResultStatus.PENDING, ProviderResultStatus.UNKNOWN):
        #     cls.schedule_reconciliation(txn)
        #     return

        cls.apply_provider_result(txn=txn, result=result, actor="webhook")

    @classmethod
    def apply_provider_result(
        cls,
        *,
        txn: Transaction,
        result,
        actor: str,
    ) -> None:
        from core.services.executor import (
            ProviderResultStatus,
            apply_next_action_from_result,
            intent_status_after_txn,
            txn_status_from_result,
        )

        if result.status in (ProviderResultStatus.PENDING, ProviderResultStatus.UNKNOWN):
            raise ValueError("apply_provider_result called with inconclusive result.")

        new_txn_status = txn_status_from_result(result)

        response_key = "callback" if actor == "webhook" else "reconciliation"

        with db_transaction.atomic():
            txn.status = new_txn_status
            txn.provider_reference = result.provider_reference or txn.provider_reference
            txn.response_payload = {**txn.response_payload, response_key: result.raw_response}
            txn.failure_code = result.failure_code
            txn.failure_reason = result.failure_reason
            if actor == "webhook":
                txn.provider_callback_received_at = timezone.now()
            txn.save()

            TransactionStateLog.objects.create(
                transaction=txn,
                from_status=Transaction.Status.PENDING,
                to_status=new_txn_status,
                actor=actor,
                reason="Provider result applied",
            )

            pi = txn.payment_intent
            if new_txn_status == Transaction.Status.SUCCESS:
                t = txn.transaction_type
                if t == Transaction.Type.PAYMENT:
                    pi.amount_captured = (pi.amount_captured or 0) + txn.amount
                elif t == Transaction.Type.AUTHORIZATION:
                    pi.amount_authorized = (pi.amount_authorized or 0) + txn.amount

            new_intent_status = intent_status_after_txn(pi, txn.transaction_type, new_txn_status)
            if new_intent_status:
                pi.status = new_intent_status
            apply_next_action_from_result(pi, result, new_intent_status)
            pi.save(update_fields=["amount_authorized", "amount_captured", "next_action", "status"])

            ReconciliationRecord.objects.filter(transaction=txn).update(
                status=ReconciliationRecord.Status.MATCHED,
                provider_reported_status=str(result.status.value),
                resolved_at=timezone.now(),
            )

        cls._enqueue_webhook(pi, txn)

        pi.refresh_from_db()
        if (
            txn.transaction_type == Transaction.Type.AUTHORIZATION
            and pi.status == PaymentIntent.Status.AUTHORIZED
            and pi.chargeable_event.auto_capture
        ):
            cls.schedule_auto_capture(pi)

    @classmethod
    def schedule_auto_capture(cls, pi: PaymentIntent) -> None:
        from core.tasks import task_capture

        event = pi.chargeable_event
        delay_hours = event.capture_delay_hours or 0
        eta = timezone.now() + timedelta(hours=delay_hours)

        task_capture.apply_async(
            kwargs={"payment_intent_id": str(pi.id)},
            eta=eta,
        )

    @classmethod
    def schedule_reconciliation(cls, transaction: Transaction, delay_seconds: int = 60) -> None:
        from core.tasks import task_reconcile_transaction

        if transaction.is_terminal:
            return

        rec, _ = ReconciliationRecord.objects.get_or_create(transaction=transaction)

        eta = timezone.now() + timedelta(seconds=delay_seconds)
        task_reconcile_transaction.apply_async(
            kwargs={"transaction_id": str(transaction.id)},
            eta=eta,
        )
        rec.last_attempted_at = timezone.now()
        rec.save(update_fields=["last_attempted_at"])

    @classmethod
    def _get_intent_for_execution(
        cls,
        payment_intent_id: str,
        allowed_statuses: list[str],
    ) -> PaymentIntent:
        try:
            pi = PaymentIntent.objects.select_related(
                "system",
                "chargeable_event__provider",
                "chargeable_event__provider_account",
            ).get(id=payment_intent_id)
        except PaymentIntent.DoesNotExist as err:
            raise ObjectDoesNotExist(f"PaymentIntent '{payment_intent_id}' not found.") from err

        if pi.status not in allowed_statuses:
            raise PaymentError(
                f"PaymentIntent is in status '{pi.status}'; expected one of {allowed_statuses}."
            )
        if pi.expires_at and pi.expires_at < timezone.now():
            raise PaymentError("PaymentIntent has expired.")

        return pi

    @classmethod
    def _require_provider_capability(cls, pi: PaymentIntent, capability: str) -> None:
        provider = pi.chargeable_event.provider
        if not getattr(provider, capability, False):
            raise PaymentError(f"Provider '{provider.name}' does not support '{capability}'.")

    @classmethod
    def _get_last_successful_txn(
        cls,
        pi: PaymentIntent,
        txn_type: str,
        error_msg: str,
    ) -> Transaction:
        txn = (
            pi.transactions.filter(
                transaction_type=txn_type,
                status=Transaction.Status.SUCCESS,
            )
            .order_by("-created_at")
            .first()
        )
        if not txn:
            raise PaymentError(error_msg)
        return txn

    @classmethod
    def _build_payload(cls, pi: PaymentIntent, extra: dict) -> dict:
        token = pi.payment_method_token
        payload = {
            "amount": str(pi.amount),
            "currency": pi.currency,
            "metadata": pi.metadata or {},
            **extra,
            **(pi.payment_payload or {}),
        }
        # If a stored token exists and payment_method hasn't been set, use it
        if token and "payment_method" not in payload:
            payload["payment_method"] = token.provider_token
        return payload

    @classmethod
    def _enqueue_webhook(cls, pi: PaymentIntent, txn: Transaction) -> None:
        event_type = cls._webhook_event_type(pi.status)

        if pi.chargeable_event.callback_destination == ChargeableEvent.CallbackDestination.SYSTEM:
            system = pi.system
        else:
            system = pi.source_system or pi.system

        outbox = WebhookOutbox.objects.create(
            system=system,
            payment_intent=pi,
            transaction=txn,
            event_type=event_type,
            payload={
                "event": event_type,
                "payment_intent_id": str(pi.id),
                "transaction_id": str(txn.id),
                "status": pi.status,
                "amount": str(pi.amount),
                "currency": pi.currency,
                "amount_authorized": str(pi.amount_authorized),
                "amount_captured": str(pi.amount_captured),
                "amount_refunded": str(pi.amount_refunded),
                "next_action": pi.next_action,
                "metadata": pi.metadata,
            },
            destination_url=system.webhook_url,
        )

        from core.tasks import task_deliver_webhook

        task_deliver_webhook.apply_async(
            kwargs={"outbox_id": str(outbox.id)},
        )

    @staticmethod
    def _webhook_event_type(pi_status: str) -> str:
        return {
            PaymentIntent.Status.SETTLED: "payment.settled",
            PaymentIntent.Status.CAPTURED: "payment.captured",
            PaymentIntent.Status.PARTIALLY_CAPTURED: "payment.partially_captured",
            PaymentIntent.Status.AUTHORIZED: "payment.authorized",
            PaymentIntent.Status.FAILED: "payment.failed",
            PaymentIntent.Status.CANCELLED: "payment.cancelled",
            PaymentIntent.Status.REFUNDED: "payment.refunded",
            PaymentIntent.Status.PARTIALLY_REFUNDED: "payment.partially_refunded",
            PaymentIntent.Status.REQUIRES_ACTION: "payment.requires_action",
            PaymentIntent.Status.PROCESSING: "payment.processing",
        }.get(pi_status, "payment.updated")

    @classmethod
    def _get_system(cls, slug: str) -> System:
        try:
            return System.objects.get(slug=slug, is_active=True)
        except System.DoesNotExist as err:
            raise ObjectDoesNotExist(f"Active system with slug '{slug}' not found.") from err

    @classmethod
    def _get_chargeable_event(cls, system: System, slug: str) -> ChargeableEvent:
        try:
            return ChargeableEvent.objects.select_related("provider", "provider_account").get(
                system=system, slug=slug, is_active=True
            )
        except ChargeableEvent.DoesNotExist as err:
            raise ObjectDoesNotExist(
                f"Active chargeable event '{slug}' not found for system '{system.name}'."
            ) from err
