import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from core.models import (
    LedgerPosting,
    OutboundTransfer,
    OutboundTransferCallbackLog,
    OutboundTransferEvent,
    OutboundTransferStateLog,
    ProviderAccount,
    System,
    WebhookOutbox,
)
from core.providers.base_provider import ProviderResult, ProviderResultStatus
from core.services.exceptions import OutboundTransferError
from core.services.registry import get_provider_instance


class OutboundTransferService:
    @classmethod
    def initialize_outbound_transfer(
        cls,
        *,
        source_system: System,
        system_slug: str,
        outbound_transfer_event_slug: str,
        amount: Decimal,
        provider_payload: dict,
        external_reference: str | None,
        idempotency_key: str,
    ) -> OutboundTransfer:
        idempotency_key = str(idempotency_key)
        if not amount.is_finite() or amount <= 0:
            raise ValidationError("Transfer amount must be greater than zero.")
        if not idempotency_key:
            raise ValidationError("idempotency_key is required for outbound transfers.")
        if len(idempotency_key) > 255:
            raise ValidationError("idempotency_key cannot exceed 255 characters.")
        if not isinstance(provider_payload, dict):
            raise ValidationError("provider_payload must be a JSON object.")

        system = cls._get_system(system_slug)
        event = cls._get_outbound_transfer_event(
            system,
            outbound_transfer_event_slug,
        )
        provider = event.provider
        account = event.provider_account
        payment_method = event.payment_method_type
        currency = event.currency.upper()

        if not provider.is_active:
            raise OutboundTransferError(
                f"Provider configured for outbound transfer event '{event.slug}' is inactive."
            )
        if not payment_method.is_active:
            raise OutboundTransferError(
                f"Payment method configured for outbound transfer event '{event.slug}' is inactive."
            )
        if account.provider_id != provider.id:
            raise OutboundTransferError(
                f"Outbound transfer event '{event.slug}' has a provider account mismatch."
            )
        if not account.is_active:
            raise OutboundTransferError(
                "Provider account configured for outbound transfer event "
                f"'{event.slug}' is inactive."
            )
        if not provider.payment_method_types.filter(id=payment_method.id).exists():
            raise OutboundTransferError(
                f"Provider configured for outbound transfer event '{event.slug}' does not support "
                f"{payment_method.code}."
            )
        cls._validate_ledger_configuration(account, system, currency)
        cls._validate_system_limits(system, amount, currency)
        if source_system.id != system.id:
            cls._validate_system_limits(source_system, amount, currency)

        normalized_provider_payload = dict(provider_payload)
        if event.default_reason and not normalized_provider_payload.get("reason"):
            normalized_provider_payload["reason"] = event.default_reason

        reference = str(external_reference or idempotency_key)
        if len(reference) > 255:
            raise ValidationError("external_reference cannot exceed 255 characters.")
        with db_transaction.atomic():
            transfer, created = OutboundTransfer.objects.get_or_create(
                source_system=source_system,
                idempotency_key=idempotency_key,
                defaults={
                    "system": system,
                    "outbound_transfer_event": event,
                    "provider": provider,
                    "provider_account": account,
                    "recipient_type": event.recipient_type,
                    "payment_method_type": payment_method,
                    "amount": amount,
                    "currency": currency,
                    "external_reference": reference,
                    "provider_payload": normalized_provider_payload,
                    "status": OutboundTransfer.Status.QUEUED,
                },
            )
            if not created:
                cls._validate_idempotent_replay(
                    transfer,
                    event=event,
                    amount=amount,
                    provider_payload=normalized_provider_payload,
                    external_reference=reference,
                )
            else:
                OutboundTransferStateLog.objects.create(
                    outbound_transfer=transfer,
                    from_status="",
                    to_status=OutboundTransfer.Status.QUEUED,
                    actor="api",
                    reason="Outbound transfer queued",
                )

        if created:
            from core.tasks import task_execute_outbound_transfer

            task_execute_outbound_transfer.apply_async(
                kwargs={"outbound_transfer_id": str(transfer.id)}
            )
        return transfer

    @staticmethod
    def _validate_ledger_configuration(
        account: ProviderAccount,
        system: System,
        currency: str,
    ) -> None:
        debit = account.outbound_transfer_debit_account
        credit = account.outbound_transfer_credit_account
        if not debit or not credit:
            raise ValidationError(
                "Provider account must configure transfer debit and credit ledger accounts."
            )
        if debit.id == credit.id:
            raise ValidationError("Transfer debit and credit accounts must be different.")
        if debit.currency != currency or credit.currency != currency:
            raise ValidationError("Transfer ledger account currencies must match the transfer.")
        for ledger_account in (debit, credit):
            if not ledger_account.is_active:
                raise ValidationError("Transfer ledger accounts must be active.")
            if ledger_account.system_id not in (None, system.id):
                raise ValidationError("Transfer ledger accounts must belong to the event owner.")

    @staticmethod
    def _validate_system_limits(system: System, amount: Decimal, currency: str) -> None:
        if system.max_transaction_amount and amount > system.max_transaction_amount:
            raise ValidationError(
                f"Transfer amount exceeds the transaction limit for system '{system.name}'."
            )
        if system.allowed_currencies and currency not in system.allowed_currencies:
            raise ValidationError(
                f"Currency '{currency}' is not allowed for system '{system.name}'."
            )

    @staticmethod
    def _validate_idempotent_replay(
        transfer: OutboundTransfer,
        *,
        event: OutboundTransferEvent,
        amount: Decimal,
        provider_payload: dict,
        external_reference: str,
    ) -> None:
        matches = (
            transfer.outbound_transfer_event_id == event.id
            and transfer.amount == amount
            and transfer.provider_payload == provider_payload
            and transfer.external_reference == external_reference
        )
        if not matches:
            raise ValidationError("idempotency_key was already used for a different transfer.")

    @classmethod
    def execute_transfer(cls, *, outbound_transfer_id: str, actor: str) -> OutboundTransfer:
        with db_transaction.atomic():
            transfer = (
                OutboundTransfer.objects.select_for_update()
                .select_related(
                    "outbound_transfer_event",
                    "provider",
                    "provider_account",
                    "payment_method_type",
                    "source_system",
                    "system",
                )
                .get(id=outbound_transfer_id)
            )
            if transfer.status != OutboundTransfer.Status.QUEUED:
                return transfer
            cls._transition_locked(
                transfer,
                OutboundTransfer.Status.PROCESSING,
                actor=actor,
                reason="Calling provider",
            )
            transfer.reconciliation_due_at = timezone.now() + timedelta(
                seconds=transfer.provider.reconciliation_timeout_seconds
            )
            transfer.save(update_fields=["reconciliation_due_at"])

        provider = get_provider_instance(
            class_name=transfer.provider.class_name,
            config=transfer.provider_account.get_config(),
        )
        payload = {
            **transfer.provider_payload,
            "external_reference": transfer.external_reference,
            "idempotency_key": transfer.idempotency_key,
            "callback_url": cls._callback_url(transfer),
        }
        try:
            result = provider.transfer(
                recipient_type=transfer.recipient_type,
                payment_method_code=transfer.payment_method_type.code,
                amount=transfer.amount,
                currency=transfer.currency,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 -- an unknown result must reconcile, never resubmit
            result = ProviderResult(
                status=ProviderResultStatus.UNKNOWN,
                failure_code="ambiguous_provider_result",
                failure_reason=str(exc),
            )
        cls.apply_provider_result(transfer=transfer, result=result, actor=actor)
        transfer.refresh_from_db()
        return transfer

    @staticmethod
    def _callback_url(transfer: OutboundTransfer) -> str:
        base_url = settings.BASE_URL
        if not base_url:
            raise ValueError("BASE_URL environment variable is not configured.")
        base_url = base_url.strip().rstrip("/")
        path = f"/core/outbound-transfers/callbacks/{transfer.provider.slug}/{transfer.id}/"
        return urljoin(f"{base_url}/", path.lstrip("/"))

    @classmethod
    def apply_provider_result(
        cls,
        *,
        transfer: OutboundTransfer,
        result: ProviderResult,
        actor: str,
    ) -> None:
        status = {
            ProviderResultStatus.SUCCESS: OutboundTransfer.Status.SUCCESS,
            ProviderResultStatus.FAILED: OutboundTransfer.Status.FAILED,
            ProviderResultStatus.PENDING: OutboundTransfer.Status.PENDING,
            ProviderResultStatus.REQUIRES_ACTION: OutboundTransfer.Status.PENDING,
            ProviderResultStatus.UNKNOWN: OutboundTransfer.Status.PENDING,
        }[result.status]

        with db_transaction.atomic():
            transfer = (
                OutboundTransfer.objects.select_for_update()
                .select_related(
                    "provider",
                    "payment_method_type",
                    "outbound_transfer_event",
                    "source_system",
                    "system",
                    "provider_account__outbound_transfer_debit_account",
                    "provider_account__outbound_transfer_credit_account",
                )
                .get(id=transfer.id)
            )
            if transfer.is_terminal:
                return
            old_status = transfer.status
            transfer.status = status
            transfer.provider_transaction_id = (
                result.provider_transaction_id or transfer.provider_transaction_id
            )
            transfer.provider_reference = result.provider_reference or transfer.provider_reference
            response_key = "callback" if actor == "webhook" else "provider"
            transfer.response_payload = {
                **transfer.response_payload,
                response_key: result.raw_response,
            }
            transfer.failure_code = result.failure_code
            transfer.failure_reason = result.failure_reason
            if actor == "webhook":
                transfer.provider_callback_received_at = timezone.now()
            if status == OutboundTransfer.Status.PENDING:
                transfer.reconciliation_due_at = timezone.now() + timedelta(
                    seconds=transfer.provider.reconciliation_timeout_seconds
                )
            transfer.save()
            OutboundTransferStateLog.objects.create(
                outbound_transfer=transfer,
                from_status=old_status,
                to_status=status,
                actor=actor,
                reason=result.failure_reason or "Provider result applied",
            )

            if status == OutboundTransfer.Status.SUCCESS:
                cls._post_ledger_entries(transfer)
            if (
                result.account_balance is not None
                and transfer.provider_account.balance_tracking_enabled
            ):
                account = transfer.provider_account
                account.provider_available_balance = result.account_balance
                account.balance_currency = result.account_balance_currency
                account.balance_checked_at = timezone.now()
                account.save(
                    update_fields=[
                        "provider_available_balance",
                        "balance_currency",
                        "balance_checked_at",
                    ]
                )

        if status == OutboundTransfer.Status.PENDING:
            from core.tasks import task_reconcile_outbound_transfer

            task_reconcile_outbound_transfer.apply_async(
                kwargs={"outbound_transfer_id": str(transfer.id)},
                eta=transfer.reconciliation_due_at,
            )
        else:
            cls._enqueue_webhook(transfer)
            if transfer.provider_account.balance_tracking_enabled:
                from core.tasks import task_refresh_provider_account_balance

                task_refresh_provider_account_balance.apply_async(
                    kwargs={"provider_account_id": str(transfer.provider_account_id)}
                )

    @staticmethod
    def _post_ledger_entries(transfer: OutboundTransfer) -> None:
        if transfer.postings.exists():
            return
        account = transfer.provider_account
        posting_ref = uuid.uuid4()
        LedgerPosting.objects.bulk_create(
            [
                LedgerPosting(
                    outbound_transfer=transfer,
                    account=account.outbound_transfer_debit_account,
                    entry_type=LedgerPosting.EntryType.DEBIT,
                    amount=transfer.amount,
                    currency=transfer.currency,
                    description=f"Outbound {transfer.recipient_type} transfer",
                    posting_ref=posting_ref,
                ),
                LedgerPosting(
                    outbound_transfer=transfer,
                    account=account.outbound_transfer_credit_account,
                    entry_type=LedgerPosting.EntryType.CREDIT,
                    amount=transfer.amount,
                    currency=transfer.currency,
                    description=f"Outbound {transfer.recipient_type} transfer",
                    posting_ref=posting_ref,
                ),
            ]
        )

    @staticmethod
    def _transition_locked(
        transfer: OutboundTransfer,
        status: str,
        *,
        actor: str,
        reason: str,
    ) -> None:
        old_status = transfer.status
        transfer.status = status
        transfer.save(update_fields=["status"])
        OutboundTransferStateLog.objects.create(
            outbound_transfer=transfer,
            from_status=old_status,
            to_status=status,
            actor=actor,
            reason=reason,
        )

    @staticmethod
    def _enqueue_webhook(transfer: OutboundTransfer) -> None:
        event_type = f"outbound_transfer.{transfer.status.lower()}"
        event = transfer.outbound_transfer_event
        if event.callback_destination == OutboundTransferEvent.CallbackDestination.SOURCE_SYSTEM:
            destination_system = transfer.source_system
        else:
            destination_system = transfer.system
        outbox = WebhookOutbox.objects.create(
            system=destination_system,
            outbound_transfer=transfer,
            event_type=event_type,
            payload={
                "event": event_type,
                "outbound_transfer_id": str(transfer.id),
                "outbound_transfer_event": event.slug,
                "status": transfer.status,
                "recipient_type": transfer.recipient_type,
                "payment_method_type": transfer.payment_method_type.code,
                "amount": str(transfer.amount),
                "currency": transfer.currency,
                "external_reference": transfer.external_reference,
                "provider_transaction_id": transfer.provider_transaction_id,
                "failure_code": transfer.failure_code,
                "failure_reason": transfer.failure_reason,
            },
            destination_url=destination_system.webhook_url,
        )
        from core.tasks import task_deliver_webhook

        task_deliver_webhook.apply_async(kwargs={"outbox_id": str(outbox.id)})

    @classmethod
    def receive_callback(
        cls,
        *,
        provider_slug: str,
        outbound_transfer_id: str,
        headers: dict,
        raw_payload: dict,
    ) -> None:
        from core.tasks import task_process_outbound_transfer_callback

        try:
            transfer = OutboundTransfer.objects.select_related("provider").get(
                id=outbound_transfer_id,
                provider__slug=provider_slug,
                provider__is_active=True,
            )
        except OutboundTransfer.DoesNotExist:
            return
        log = OutboundTransferCallbackLog.objects.create(
            provider=transfer.provider,
            outbound_transfer=transfer,
            raw_headers=headers,
            raw_payload=raw_payload,
        )
        task_process_outbound_transfer_callback.apply_async(kwargs={"callback_log_id": str(log.id)})

    @classmethod
    def process_callback(cls, log: OutboundTransferCallbackLog) -> None:
        transfer = log.outbound_transfer
        if transfer.is_terminal:
            log.status = OutboundTransferCallbackLog.Status.IGNORED
            log.processing_error = "Transfer is already terminal."
            log.save(update_fields=["status", "processing_error"])
            return

        provider = get_provider_instance(
            class_name=log.provider.class_name,
            config=transfer.provider_account.get_config(),
        )
        if not provider.callback_verification_supported:
            log.status = OutboundTransferCallbackLog.Status.IGNORED
            log.processing_error = "Provider callback cannot be authenticated; reconciling instead."
            log.save(update_fields=["status", "processing_error"])
            cls.queue_reconciliation(transfer)
            return
        if not provider.verify_callback(headers=log.raw_headers, payload=log.raw_payload):
            log.status = OutboundTransferCallbackLog.Status.REJECTED
            log.processing_error = "Invalid callback signature."
            log.save(update_fields=["status", "processing_error"])
            return

        result = provider.parse_callback(payload=log.raw_payload)
        log.parsed_status = result.status.value
        log.save(update_fields=["parsed_status"])
        callback_ids = {result.provider_transaction_id, result.provider_reference} - {""}
        if (
            transfer.provider_transaction_id
            and transfer.provider_transaction_id not in callback_ids
        ):
            log.status = OutboundTransferCallbackLog.Status.REJECTED
            log.processing_error = "Callback transaction does not match the transfer."
            log.save(update_fields=["status", "processing_error"])
            return
        if result.status in (
            ProviderResultStatus.PENDING,
            ProviderResultStatus.UNKNOWN,
        ):
            cls.queue_reconciliation(transfer)
            return
        cls.apply_provider_result(transfer=transfer, result=result, actor="webhook")

    @staticmethod
    def queue_reconciliation(transfer: OutboundTransfer) -> None:
        from core.tasks import task_reconcile_outbound_transfer

        task_reconcile_outbound_transfer.apply_async(
            kwargs={"outbound_transfer_id": str(transfer.id)}
        )

    @classmethod
    def get_outbound_transfer(
        cls,
        *,
        outbound_transfer_id: str,
        system: System,
    ) -> OutboundTransfer:
        try:
            return OutboundTransfer.objects.get(
                Q(system=system) | Q(source_system=system),
                id=outbound_transfer_id,
            )
        except OutboundTransfer.DoesNotExist as exc:
            raise ObjectDoesNotExist(
                f"Outbound transfer '{outbound_transfer_id}' not found."
            ) from exc

    @staticmethod
    def _get_system(slug: str) -> System:
        try:
            return System.objects.get(slug=slug, is_active=True)
        except System.DoesNotExist as exc:
            raise ObjectDoesNotExist(f"Active system with slug '{slug}' not found.") from exc

    @staticmethod
    def _get_outbound_transfer_event(
        system: System,
        slug: str,
    ) -> OutboundTransferEvent:
        try:
            return OutboundTransferEvent.objects.select_related(
                "system",
                "provider",
                "provider_account__outbound_transfer_debit_account",
                "provider_account__outbound_transfer_credit_account",
                "payment_method_type",
            ).get(
                system=system,
                slug=slug,
                is_active=True,
            )
        except OutboundTransferEvent.DoesNotExist as exc:
            raise ObjectDoesNotExist(
                f"Active outbound transfer event '{slug}' not found for system '{system.name}'."
            ) from exc
