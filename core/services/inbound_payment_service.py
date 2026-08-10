import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    InboundPayment,
    InboundPaymentCallbackLog,
    InboundPaymentEvent,
    InboundPaymentMethodToken,
    InboundPaymentReconciliation,
    InboundPaymentTransaction,
    InboundPaymentTransactionStateLog,
    Provider,
    System,
    WebhookOutbox,
)
from core.providers.base_provider import ProviderResultStatus
from core.services.exceptions import InboundPaymentError
from core.services.inbound_payment_executor import InboundPaymentTransactionExecutor
from core.services.registry import get_provider_instance

logger = logging.getLogger(__name__)


class InboundPaymentService:
    @classmethod
    def get_inbound_payment(
        cls,
        *,
        inbound_payment_id: str,
        system: System,
    ) -> InboundPayment:
        try:
            return InboundPayment.objects.select_related("inbound_payment_event").get(
                Q(system=system) | Q(source_system=system),
                id=inbound_payment_id,
            )
        except InboundPayment.DoesNotExist as err:
            raise ObjectDoesNotExist(
                f"Inbound payment '{inbound_payment_id}' not found."
            ) from err

    @classmethod
    def initialize_inbound_payment(
        cls,
        *,
        source_system: System,
        system_slug: str,
        inbound_payment_event_slug: str,
        amount: float | None = None,
        currency: str | None = None,
        # Provider-specific data: phone number, return_url, etc.
        provider_payload: dict | None = None,
        payment_method_token_id: str | None = None,
        external_reference: str | None = None,
        idempotency_key: str | None = None,
    ) -> InboundPayment:
        idempotency_key = idempotency_key or str(uuid.uuid4())

        system = cls._get_system(system_slug)
        event = cls._get_inbound_payment_event(system, inbound_payment_event_slug)

        resolved_amount = amount or event.fixed_amount
        resolved_currency = currency or event.currency

        if not resolved_amount:
            raise ValidationError(
                "No amount provided and the inbound payment event has no fixed amount."
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
                token = InboundPaymentMethodToken.objects.get(
                    id=payment_method_token_id, system=system, is_active=True
                )
            except InboundPaymentMethodToken.DoesNotExist as err:
                raise ObjectDoesNotExist(
                    f"Payment method token '{payment_method_token_id}' not found."
                ) from err

        with db_transaction.atomic():
            inbound_payment, created = InboundPayment.objects.get_or_create(
                system=system,
                idempotency_key=idempotency_key,
                defaults={
                    "source_system": source_system,
                    "inbound_payment_event": event,
                    "payment_method_token": token,
                    "amount": resolved_amount,
                    "currency": resolved_currency,
                    "provider_payload": provider_payload or {},
                    "external_reference": external_reference,
                    "status": InboundPayment.Status.INITIATED,
                    "expires_at": timezone.now() + timedelta(hours=24),
                },
            )

        if not created:
            return inbound_payment

        cls._queue_initial_operation(inbound_payment)

        return inbound_payment

    @classmethod
    def _queue_initial_operation(cls, inbound_payment: InboundPayment) -> None:
        from core.tasks import (
            task_authorize_inbound_payment,
            task_collect_inbound_payment,
        )

        if inbound_payment.inbound_payment_event.flow == InboundPaymentEvent.Flow.COLLECTION:
            task_collect_inbound_payment.apply_async(
                kwargs={"inbound_payment_id": str(inbound_payment.id)}
            )

        elif (
            inbound_payment.inbound_payment_event.flow == InboundPaymentEvent.Flow.AUTHORIZE_CAPTURE
        ):
            task_authorize_inbound_payment.apply_async(
                kwargs={"inbound_payment_id": str(inbound_payment.id)}
            )

    @classmethod
    def queue_capture(
        cls,
        *,
        inbound_payment_id: str,
        amount: Decimal | None = None,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_capture_inbound_payment

        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id,
            allowed_statuses=[
                InboundPayment.Status.AUTHORIZED,
                InboundPayment.Status.PARTIALLY_CAPTURED,
            ],
        )

        capture_amount = amount or inbound_payment.amount_remaining
        if capture_amount <= 0:
            raise InboundPaymentError("Nothing left to capture on this inbound payment.")
        if capture_amount > inbound_payment.amount_remaining:
            raise ValidationError(
                f"Capture amount {capture_amount} exceeds remaining authorized "
                f"amount {inbound_payment.amount_remaining}."
            )

        result = task_capture_inbound_payment.apply_async(
            kwargs={
                "inbound_payment_id": str(inbound_payment.id),
                "amount": str(capture_amount),
                "payload": payload or {},
            },
        )
        return result.id

    @classmethod
    def queue_void(
        cls,
        *,
        inbound_payment_id: str,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_void_inbound_payment

        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id,
            allowed_statuses=[InboundPayment.Status.AUTHORIZED],
        )

        result = task_void_inbound_payment.apply_async(
            kwargs={
                "inbound_payment_id": str(inbound_payment.id),
                "payload": payload or {},
            },
        )
        return result.id

    @classmethod
    def queue_refund(
        cls,
        *,
        inbound_payment_id: str,
        amount: Decimal | None = None,
        payload: dict | None = None,
    ) -> str:
        from core.tasks import task_refund_inbound_payment

        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id,
            allowed_statuses=[
                InboundPayment.Status.CAPTURED,
                InboundPayment.Status.PARTIALLY_CAPTURED,
                InboundPayment.Status.SETTLED,
                InboundPayment.Status.PARTIALLY_REFUNDED,
            ],
        )
        cls._require_provider_capability(inbound_payment, "supports_refund")

        refund_amount = amount or inbound_payment.amount_refundable
        if refund_amount <= 0:
            raise InboundPaymentError("Nothing left to refund on this inbound payment.")
        if refund_amount > inbound_payment.amount_refundable:
            raise ValidationError(
                f"Refund amount {refund_amount} exceeds the refundable "
                f"amount {inbound_payment.amount_refundable}."
            )
        if refund_amount < inbound_payment.amount_refundable:
            cls._require_provider_capability(inbound_payment, "supports_partial_refund")

        result = task_refund_inbound_payment.apply_async(
            kwargs={
                "inbound_payment_id": str(inbound_payment.id),
                "amount": str(refund_amount),
                "payload": payload or {},
            },
        )
        return result.id

    @classmethod
    def execute_collection(
        cls,
        *,
        inbound_payment_id: str,
        extra_payload: dict,
        actor: str,
    ) -> InboundPaymentTransaction:
        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id, allowed_statuses=[InboundPayment.Status.INITIATED]
        )
        inbound_payment_transaction = InboundPaymentTransactionExecutor(
            inbound_payment=inbound_payment,
            operation_type=InboundPaymentTransaction.Operation.COLLECTION,
            amount=inbound_payment.amount,
            provider_payload=cls._build_provider_payload(inbound_payment, extra_payload),
            actor=actor,
        ).run()
        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)
        return inbound_payment_transaction

    @classmethod
    def execute_authorize(
        cls,
        *,
        inbound_payment_id: str,
        extra_payload: dict,
        actor: str,
    ) -> InboundPaymentTransaction:
        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id, allowed_statuses=[InboundPayment.Status.INITIATED]
        )
        inbound_payment_transaction = InboundPaymentTransactionExecutor(
            inbound_payment=inbound_payment,
            operation_type=InboundPaymentTransaction.Operation.AUTHORIZATION,
            amount=inbound_payment.amount,
            provider_payload=cls._build_provider_payload(inbound_payment, extra_payload),
            actor=actor,
        ).run()
        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)

        inbound_payment.refresh_from_db()

        if (
            inbound_payment.status == InboundPayment.Status.AUTHORIZED
            and inbound_payment.inbound_payment_event.auto_capture
        ):
            cls.schedule_auto_capture(inbound_payment)

        return inbound_payment_transaction

    @classmethod
    def execute_capture(
        cls,
        *,
        inbound_payment_id: str,
        amount: Decimal | None,
        extra_payload: dict,
        actor: str,
    ) -> InboundPaymentTransaction:
        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id,
            allowed_statuses=[
                InboundPayment.Status.AUTHORIZED,
                InboundPayment.Status.PARTIALLY_CAPTURED,
            ],
        )
        authorization_transaction = cls._get_last_successful_transaction(
            inbound_payment,
            InboundPaymentTransaction.Operation.AUTHORIZATION,
            "No successful authorization found to capture.",
        )
        capture_amount = amount or inbound_payment.amount_remaining
        inbound_payment_transaction = InboundPaymentTransactionExecutor(
            inbound_payment=inbound_payment,
            operation_type=InboundPaymentTransaction.Operation.CAPTURE,
            amount=capture_amount,
            provider_payload={**extra_payload, "currency": inbound_payment.currency},
            actor=actor,
            parent_provider_transaction_id=authorization_transaction.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)
        return inbound_payment_transaction

    @classmethod
    def execute_void(
        cls,
        *,
        inbound_payment_id: str,
        extra_payload: dict,
        actor: str,
    ) -> InboundPaymentTransaction:
        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id, allowed_statuses=[InboundPayment.Status.AUTHORIZED]
        )
        authorization_transaction = cls._get_last_successful_transaction(
            inbound_payment,
            InboundPaymentTransaction.Operation.AUTHORIZATION,
            "No successful authorization found to void.",
        )
        inbound_payment_transaction = InboundPaymentTransactionExecutor(
            inbound_payment=inbound_payment,
            operation_type=InboundPaymentTransaction.Operation.VOID,
            amount=inbound_payment.amount_authorized,
            provider_payload=extra_payload,
            actor=actor,
            parent_provider_transaction_id=authorization_transaction.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)
        return inbound_payment_transaction

    @classmethod
    def execute_refund(
        cls,
        *,
        inbound_payment_id: str,
        amount: Decimal | None,
        extra_payload: dict,
        actor: str,
    ) -> InboundPaymentTransaction:
        inbound_payment = cls._get_inbound_payment_for_execution(
            inbound_payment_id,
            allowed_statuses=[
                InboundPayment.Status.CAPTURED,
                InboundPayment.Status.PARTIALLY_CAPTURED,
                InboundPayment.Status.SETTLED,
                InboundPayment.Status.PARTIALLY_REFUNDED,
            ],
        )
        original_transaction = (
            inbound_payment.transactions.filter(
                operation_type__in=[
                    InboundPaymentTransaction.Operation.COLLECTION,
                    InboundPaymentTransaction.Operation.CAPTURE,
                ],
                status=InboundPaymentTransaction.Status.SUCCESS,
            )
            .order_by("-created_at")
            .first()
        )
        if not original_transaction:
            raise InboundPaymentError("No captured transaction found to refund.")

        refund_amount = amount or inbound_payment.amount_refundable
        operation_type = (
            InboundPaymentTransaction.Operation.PARTIAL_REFUND
            if refund_amount < inbound_payment.amount_captured
            else InboundPaymentTransaction.Operation.REFUND
        )
        inbound_payment_transaction = InboundPaymentTransactionExecutor(
            inbound_payment=inbound_payment,
            operation_type=operation_type,
            amount=refund_amount,
            provider_payload={**extra_payload, "currency": inbound_payment.currency},
            actor=actor,
            parent_provider_transaction_id=original_transaction.provider_transaction_id,
        ).run()
        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)
        return inbound_payment_transaction

    @classmethod
    def receive_callback(
        cls,
        *,
        provider_slug: str,
        inbound_payment_transaction_id: str,
        headers: dict,
        raw_payload: dict,
    ) -> None:
        from core.tasks import task_process_inbound_payment_callback

        try:
            provider = Provider.objects.get(slug=provider_slug, is_active=True)
        except Provider.DoesNotExist:
            logger.warning("Callback for unknown provider %s", provider_slug)
            return

        try:
            inbound_payment_transaction = InboundPaymentTransaction.objects.get(
                id=inbound_payment_transaction_id,
                provider=provider,
            )
        except InboundPaymentTransaction.DoesNotExist:
            logger.warning(
                "Callback for non-existing inbound payment transaction %s",
                inbound_payment_transaction_id,
            )
            return

        log = InboundPaymentCallbackLog.objects.create(
            provider=provider,
            inbound_payment_transaction=inbound_payment_transaction,
            raw_headers=headers,
            raw_payload=raw_payload,
            status=InboundPaymentCallbackLog.Status.RECEIVED,
        )

        task_process_inbound_payment_callback.apply_async(
            kwargs={"callback_log_id": str(log.id)},
        )

    @classmethod
    def process_callback(cls, log: InboundPaymentCallbackLog) -> None:
        inbound_payment_transaction = log.inbound_payment_transaction
        provider = log.provider
        account = inbound_payment_transaction.provider_account

        if inbound_payment_transaction.is_terminal:
            log.status = InboundPaymentCallbackLog.Status.IGNORED
            log.processing_error = (
                "Ignored: inbound payment transaction is already terminal "
                f"({inbound_payment_transaction.status})"
            )
            log.save(update_fields=["status", "processing_error"])
            return

        provider_instance = get_provider_instance(
            class_name=provider.class_name,
            config=account.get_config(),
        )

        if not provider_instance.verify_callback(headers=log.raw_headers, payload=log.raw_payload):
            log.status = InboundPaymentCallbackLog.Status.REJECTED
            log.processing_error = "Invalid signature"
            log.save(update_fields=["status", "processing_error"])
            return

        result = provider_instance.parse_callback(payload=log.raw_payload)

        log.parsed_status = str(result.status.value)
        log.save(update_fields=["parsed_status"])

        callback_ids = {result.provider_transaction_id, result.provider_reference} - {""}
        if (
            inbound_payment_transaction.provider_transaction_id
            and inbound_payment_transaction.provider_transaction_id not in callback_ids
        ):
            log.status = InboundPaymentCallbackLog.Status.REJECTED
            log.processing_error = "Callback transaction does not match the inbound payment."
            log.save(update_fields=["status", "processing_error"])
            return

        if result.status in (
            ProviderResultStatus.PENDING,
            ProviderResultStatus.UNKNOWN,
        ):
            cls.schedule_reconciliation(inbound_payment_transaction)
            return

        cls.apply_provider_result(
            inbound_payment_transaction=inbound_payment_transaction,
            result=result,
            actor="webhook",
        )

    @classmethod
    def apply_provider_result(
        cls,
        *,
        inbound_payment_transaction: InboundPaymentTransaction,
        result,
        actor: str,
    ) -> None:
        from core.services.inbound_payment_executor import (
            apply_next_action_from_result,
            inbound_payment_status_after_transaction,
            inbound_payment_transaction_status_from_result,
        )

        new_transaction_status = inbound_payment_transaction_status_from_result(result)

        response_key = "callback" if actor == "webhook" else "reconciliation"

        with db_transaction.atomic():
            old_transaction_status = inbound_payment_transaction.status
            inbound_payment_transaction.status = new_transaction_status
            inbound_payment_transaction.provider_transaction_id = (
                result.provider_transaction_id
                or inbound_payment_transaction.provider_transaction_id
            )
            inbound_payment_transaction.provider_reference = (
                result.provider_reference or inbound_payment_transaction.provider_reference
            )
            inbound_payment_transaction.response_payload = {
                **inbound_payment_transaction.response_payload,
                response_key: result.raw_response,
            }
            inbound_payment_transaction.failure_code = result.failure_code
            inbound_payment_transaction.failure_reason = result.failure_reason
            if actor == "webhook":
                inbound_payment_transaction.provider_callback_received_at = timezone.now()
            inbound_payment_transaction.save()

            InboundPaymentTransactionStateLog.objects.create(
                inbound_payment_transaction=inbound_payment_transaction,
                from_status=old_transaction_status,
                to_status=new_transaction_status,
                actor=actor,
                reason="Provider result applied",
            )

            inbound_payment = inbound_payment_transaction.inbound_payment
            if new_transaction_status == InboundPaymentTransaction.Status.SUCCESS:
                operation_type = inbound_payment_transaction.operation_type
                if operation_type == InboundPaymentTransaction.Operation.COLLECTION:
                    inbound_payment.amount_captured = (
                        inbound_payment.amount_captured or 0
                    ) + inbound_payment_transaction.amount
                elif operation_type == InboundPaymentTransaction.Operation.AUTHORIZATION:
                    inbound_payment.amount_authorized = (
                        inbound_payment.amount_authorized or 0
                    ) + inbound_payment_transaction.amount

            new_inbound_payment_status = inbound_payment_status_after_transaction(
                inbound_payment,
                inbound_payment_transaction.operation_type,
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
                    "next_action",
                    "status",
                ]
            )

            InboundPaymentReconciliation.objects.filter(
                inbound_payment_transaction=inbound_payment_transaction
            ).update(
                status=InboundPaymentReconciliation.Status.MATCHED,
                provider_reported_status=str(result.status.value),
                resolved_at=timezone.now(),
            )

            provider_account = inbound_payment_transaction.provider_account
            if result.account_balance is not None and provider_account.balance_tracking_enabled:
                provider_account.provider_available_balance = result.account_balance
                provider_account.balance_currency = result.account_balance_currency
                provider_account.balance_checked_at = timezone.now()
                provider_account.save(
                    update_fields=[
                        "provider_available_balance",
                        "balance_currency",
                        "balance_checked_at",
                    ]
                )

        cls._enqueue_webhook(inbound_payment, inbound_payment_transaction)

        if inbound_payment_transaction.provider_account.balance_tracking_enabled:
            from core.tasks import task_refresh_provider_account_balance

            task_refresh_provider_account_balance.apply_async(
                kwargs={"provider_account_id": str(inbound_payment_transaction.provider_account_id)}
            )

        inbound_payment.refresh_from_db()
        if (
            inbound_payment_transaction.operation_type
            == InboundPaymentTransaction.Operation.AUTHORIZATION
            and inbound_payment.status == InboundPayment.Status.AUTHORIZED
            and inbound_payment.inbound_payment_event.auto_capture
        ):
            cls.schedule_auto_capture(inbound_payment)

    @classmethod
    def schedule_auto_capture(cls, inbound_payment: InboundPayment) -> None:
        from core.tasks import task_capture_inbound_payment

        event = inbound_payment.inbound_payment_event
        delay_hours = event.capture_delay_hours or 0
        eta = timezone.now() + timedelta(hours=delay_hours)

        task_capture_inbound_payment.apply_async(
            kwargs={"inbound_payment_id": str(inbound_payment.id)},
            eta=eta,
        )

    @classmethod
    def schedule_reconciliation(
        cls,
        inbound_payment_transaction: InboundPaymentTransaction,
        delay_seconds: int = 60,
    ) -> None:
        from core.tasks import task_reconcile_inbound_payment_transaction

        if inbound_payment_transaction.is_terminal:
            return

        rec, _ = InboundPaymentReconciliation.objects.get_or_create(
            inbound_payment_transaction=inbound_payment_transaction
        )

        eta = timezone.now() + timedelta(seconds=delay_seconds)
        task_reconcile_inbound_payment_transaction.apply_async(
            kwargs={"inbound_payment_transaction_id": str(inbound_payment_transaction.id)},
            eta=eta,
        )
        rec.last_attempted_at = timezone.now()
        rec.save(update_fields=["last_attempted_at"])

    @classmethod
    def _get_inbound_payment_for_execution(
        cls,
        inbound_payment_id: str,
        allowed_statuses: list[str],
    ) -> InboundPayment:
        try:
            inbound_payment = InboundPayment.objects.select_related(
                "system",
                "inbound_payment_event__provider",
                "inbound_payment_event__provider_account",
            ).get(id=inbound_payment_id)
        except InboundPayment.DoesNotExist as err:
            raise ObjectDoesNotExist(f"Inbound payment '{inbound_payment_id}' not found.") from err

        if inbound_payment.status not in allowed_statuses:
            raise InboundPaymentError(
                f"Inbound payment is in status '{inbound_payment.status}'; "
                f"expected one of {allowed_statuses}."
            )
        if inbound_payment.expires_at and inbound_payment.expires_at < timezone.now():
            raise InboundPaymentError("Inbound payment has expired.")

        return inbound_payment

    @classmethod
    def _require_provider_capability(
        cls,
        inbound_payment: InboundPayment,
        capability: str,
    ) -> None:
        provider = inbound_payment.inbound_payment_event.provider
        if not getattr(provider, capability, False):
            raise InboundPaymentError(
                f"Provider '{provider.name}' does not support '{capability}'."
            )

    @classmethod
    def _get_last_successful_transaction(
        cls,
        inbound_payment: InboundPayment,
        operation_type: str,
        error_msg: str,
    ) -> InboundPaymentTransaction:
        inbound_payment_transaction = (
            inbound_payment.transactions.filter(
                operation_type=operation_type,
                status=InboundPaymentTransaction.Status.SUCCESS,
            )
            .order_by("-created_at")
            .first()
        )
        if not inbound_payment_transaction:
            raise InboundPaymentError(error_msg)
        return inbound_payment_transaction

    @classmethod
    def _build_provider_payload(cls, inbound_payment: InboundPayment, extra: dict) -> dict:
        token = inbound_payment.payment_method_token
        payload = {
            "amount": str(inbound_payment.amount),
            "currency": inbound_payment.currency,
            "metadata": inbound_payment.metadata or {},
            **extra,
            **(inbound_payment.provider_payload or {}),
            # Provider-facing correlation must use the gateway's immutable ID.
            # The caller's external reference remains on InboundPayment for
            # client-facing reconciliation and webhook delivery.
            "external_reference": str(inbound_payment.id),
            "idempotency_key": inbound_payment.idempotency_key,
        }
        # If a stored token exists and payment_method hasn't been set, use it
        if token and "payment_method" not in payload:
            payload["payment_method"] = token.provider_token
        return payload

    @classmethod
    def _enqueue_webhook(
        cls,
        inbound_payment: InboundPayment,
        inbound_payment_transaction: InboundPaymentTransaction,
    ) -> None:
        event_type = cls._inbound_payment_webhook_event_type(inbound_payment.status)

        if (
            inbound_payment.inbound_payment_event.callback_destination
            == InboundPaymentEvent.CallbackDestination.SYSTEM
        ):
            system = inbound_payment.system
        else:
            system = inbound_payment.source_system or inbound_payment.system

        outbox = WebhookOutbox.objects.create(
            system=system,
            inbound_payment=inbound_payment,
            inbound_payment_transaction=inbound_payment_transaction,
            event_type=event_type,
            payload={
                "event": event_type,
                "inbound_payment_id": str(inbound_payment.id),
                "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
                "status": inbound_payment.status,
                "amount": str(inbound_payment.amount),
                "currency": inbound_payment.currency,
                "amount_authorized": str(inbound_payment.amount_authorized),
                "amount_captured": str(inbound_payment.amount_captured),
                "amount_refunded": str(inbound_payment.amount_refunded),
                "external_reference": inbound_payment.external_reference,
                "next_action": inbound_payment.next_action,
                "metadata": inbound_payment.metadata,
            },
            destination_url=system.webhook_url,
        )

        from core.tasks import task_deliver_webhook

        task_deliver_webhook.apply_async(
            kwargs={"outbox_id": str(outbox.id)},
        )

    @staticmethod
    def _inbound_payment_webhook_event_type(inbound_payment_status: str) -> str:
        return {
            InboundPayment.Status.SETTLED: "inbound_payment.settled",
            InboundPayment.Status.CAPTURED: "inbound_payment.captured",
            InboundPayment.Status.PARTIALLY_CAPTURED: "inbound_payment.partially_captured",
            InboundPayment.Status.AUTHORIZED: "inbound_payment.authorized",
            InboundPayment.Status.FAILED: "inbound_payment.failed",
            InboundPayment.Status.CANCELLED: "inbound_payment.cancelled",
            InboundPayment.Status.REFUNDED: "inbound_payment.refunded",
            InboundPayment.Status.PARTIALLY_REFUNDED: "inbound_payment.partially_refunded",
            InboundPayment.Status.REQUIRES_ACTION: "inbound_payment.requires_action",
            InboundPayment.Status.PROCESSING: "inbound_payment.processing",
        }.get(inbound_payment_status, "inbound_payment.updated")

    @classmethod
    def _get_system(cls, slug: str) -> System:
        try:
            return System.objects.get(slug=slug, is_active=True)
        except System.DoesNotExist as err:
            raise ObjectDoesNotExist(f"Active system with slug '{slug}' not found.") from err

    @classmethod
    def _get_inbound_payment_event(cls, system: System, slug: str) -> InboundPaymentEvent:
        try:
            return InboundPaymentEvent.objects.select_related("provider", "provider_account").get(
                system=system, slug=slug, is_active=True
            )
        except InboundPaymentEvent.DoesNotExist as err:
            raise ObjectDoesNotExist(
                f"Active inbound payment event '{slug}' not found for system '{system.name}'."
            ) from err
