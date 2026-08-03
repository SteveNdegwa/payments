import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core.models import (
    InboundPayment,
    InboundPaymentCallbackLog,
    InboundPaymentReconciliation,
    InboundPaymentTransaction,
    OutboundTransfer,
    OutboundTransferCallbackLog,
    OutboundTransferStateLog,
    ProviderAccount,
    WebhookDeliveryLog,
    WebhookOutbox,
)

logger = logging.getLogger(__name__)


_PROVIDER_RETRY = {
    "max_retries": 3,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 120,
    "retry_jitter": True,
}

_RECONCILE_RETRY = {
    "max_retries": 10,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
}

_WEBHOOK_RETRY = {
    "max_retries": 5,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_backoff_max": 3600,
    "retry_jitter": True,
}


@shared_task(
    name="payments.refresh_provider_account_balance",
    queue="payments.low",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_refresh_provider_account_balance(self, provider_account_id: str):
    from core.services.registry import get_provider_instance

    try:
        account = ProviderAccount.objects.select_related("provider").get(id=provider_account_id)
    except ProviderAccount.DoesNotExist:
        logger.error("Balance refresh: provider account %s not found", provider_account_id)
        return

    if not account.is_active or not account.balance_tracking_enabled:
        return

    provider = get_provider_instance(
        class_name=account.provider.class_name,
        config=account.get_config(),
    )
    balance = provider.query_balance()
    if balance is None:
        logger.warning("Provider %s does not support balance tracking", account.provider.slug)
        return

    account.provider_available_balance = balance.amount
    account.balance_currency = balance.currency
    account.balance_checked_at = timezone.now()
    account.save(
        update_fields=[
            "provider_available_balance",
            "balance_currency",
            "balance_checked_at",
        ]
    )


@shared_task(
    name="payments.execute_outbound_transfer",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_execute_outbound_transfer(self, outbound_transfer_id: str):
    from core.services.outbound_transfer_service import OutboundTransferService

    try:
        transfer = OutboundTransferService.execute_transfer(
            outbound_transfer_id=outbound_transfer_id,
            actor=f"celery:outbound-transfer:{self.request.id}",
        )
    except OutboundTransfer.DoesNotExist:
        logger.error("Outbound transfer %s not found", outbound_transfer_id)
        return
    return {"outbound_transfer_id": str(transfer.id), "status": transfer.status}


@shared_task(
    name="payments.process_outbound_transfer_callback",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_process_outbound_transfer_callback(self, callback_log_id: str):
    from core.services.outbound_transfer_service import OutboundTransferService

    try:
        log = OutboundTransferCallbackLog.objects.select_related(
            "provider",
            "outbound_transfer__provider_account__provider",
        ).get(id=callback_log_id)
    except OutboundTransferCallbackLog.DoesNotExist:
        logger.error("Outbound transfer callback %s not found", callback_log_id)
        return
    if log.status in {
        OutboundTransferCallbackLog.Status.PROCESSED,
        OutboundTransferCallbackLog.Status.REJECTED,
        OutboundTransferCallbackLog.Status.IGNORED,
    }:
        return

    log.status = OutboundTransferCallbackLog.Status.PROCESSING
    log.save(update_fields=["status"])
    try:
        OutboundTransferService.process_callback(log)
        log.refresh_from_db()
        if log.status == OutboundTransferCallbackLog.Status.PROCESSING:
            log.status = OutboundTransferCallbackLog.Status.PROCESSED
            log.save(update_fields=["status"])
    except Exception as exc:
        log.status = OutboundTransferCallbackLog.Status.FAILED
        log.processing_error = f"{type(exc).__name__}: {exc!s}"
        log.save(update_fields=["status", "processing_error"])
        raise


@shared_task(
    name="payments.reconcile_outbound_transfer",
    queue="payments.low",
    bind=True,
    **_RECONCILE_RETRY,
)
def task_reconcile_outbound_transfer(self, outbound_transfer_id: str):
    from core.providers.base_provider import ProviderResultStatus
    from core.services.outbound_transfer_service import OutboundTransferService
    from core.services.registry import get_provider_instance

    try:
        transfer = OutboundTransfer.objects.select_related(
            "provider_account__provider",
            "payment_method_type",
        ).get(id=outbound_transfer_id)
    except OutboundTransfer.DoesNotExist:
        logger.error(
            "Outbound transfer %s not found during reconciliation",
            outbound_transfer_id,
        )
        return
    if transfer.is_terminal or transfer.status == OutboundTransfer.Status.MANUAL_REVIEW:
        return
    if not transfer.provider_transaction_id:
        old_status = transfer.status
        transfer.status = OutboundTransfer.Status.MANUAL_REVIEW
        transfer.save(update_fields=["status"])
        OutboundTransferStateLog.objects.create(
            outbound_transfer=transfer,
            from_status=old_status,
            to_status=OutboundTransfer.Status.MANUAL_REVIEW,
            actor="celery:reconcile",
            reason="Provider response was ambiguous and supplied no queryable transaction ID",
        )
        return

    provider = get_provider_instance(
        class_name=transfer.provider.class_name,
        config=transfer.provider_account.get_config(),
    )
    result = provider.query_status(
        provider_transaction_id=transfer.provider_transaction_id,
        payload={
            "recipient_type": transfer.recipient_type,
            "payment_method_code": transfer.payment_method_type.code,
        },
    )
    transfer.reconciliation_attempts += 1
    transfer.save(update_fields=["reconciliation_attempts"])

    if result.status in (ProviderResultStatus.PENDING, ProviderResultStatus.UNKNOWN):
        if self.request.retries >= self.max_retries:
            old_status = transfer.status
            transfer.status = OutboundTransfer.Status.MANUAL_REVIEW
            transfer.save(update_fields=["status"])
            OutboundTransferStateLog.objects.create(
                outbound_transfer=transfer,
                from_status=old_status,
                to_status=OutboundTransfer.Status.MANUAL_REVIEW,
                actor="celery:reconcile",
                reason="Reconciliation retries exhausted",
            )
            return
        raise self.retry(countdown=60 * min(transfer.reconciliation_attempts, 10))

    OutboundTransferService.apply_provider_result(
        transfer=transfer,
        result=result,
        actor="celery:reconcile",
    )


@shared_task(name="payments.scan_overdue_outbound_transfers", queue="payments.low")
def task_scan_overdue_outbound_transfers():
    outbound_transfer_ids = OutboundTransfer.objects.filter(
        status__in=[
            OutboundTransfer.Status.PROCESSING,
            OutboundTransfer.Status.PENDING,
        ],
        reconciliation_due_at__lte=timezone.now(),
    ).values_list("id", flat=True)
    count = 0
    for outbound_transfer_id in outbound_transfer_ids:
        task_reconcile_outbound_transfer.apply_async(
            kwargs={"outbound_transfer_id": str(outbound_transfer_id)}
        )
        count += 1
    return count


@shared_task(
    name="payments.collect_inbound_payment",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_collect_inbound_payment(self, inbound_payment_id: str):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        inbound_payment_transaction = InboundPaymentService.execute_collection(
            inbound_payment_id=inbound_payment_id,
            extra_payload={},
            actor=f"celery:collection:{self.request.id}",
        )
        return {
            "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
            "status": inbound_payment_transaction.status,
        }
    except InboundPayment.DoesNotExist:
        logger.error("Inbound payment %s not found during collection", inbound_payment_id)


@shared_task(
    name="payments.authorize_inbound_payment",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_authorize_inbound_payment(self, inbound_payment_id: str):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        inbound_payment_transaction = InboundPaymentService.execute_authorize(
            inbound_payment_id=inbound_payment_id,
            extra_payload={},
            actor=f"celery:authorize:{self.request.id}",
        )
        return {
            "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
            "status": inbound_payment_transaction.status,
        }
    except InboundPayment.DoesNotExist:
        logger.error("Inbound payment %s not found during authorization", inbound_payment_id)


@shared_task(
    name="payments.capture_inbound_payment",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_capture_inbound_payment(
    self,
    inbound_payment_id: str,
    amount: str | None = None,
    payload: dict | None = None,
):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        inbound_payment_transaction = InboundPaymentService.execute_capture(
            inbound_payment_id=inbound_payment_id,
            amount=Decimal(amount) if amount else None,
            extra_payload=payload or {},
            actor=f"celery:capture:{self.request.id}",
        )
        return {
            "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
            "status": inbound_payment_transaction.status,
        }
    except InboundPayment.DoesNotExist:
        logger.error("Inbound payment %s not found during capture", inbound_payment_id)


@shared_task(
    name="payments.void_inbound_payment",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_void_inbound_payment(self, inbound_payment_id: str, payload: dict | None = None):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        inbound_payment_transaction = InboundPaymentService.execute_void(
            inbound_payment_id=inbound_payment_id,
            extra_payload=payload or {},
            actor=f"celery:void:{self.request.id}",
        )
        return {
            "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
            "status": inbound_payment_transaction.status,
        }
    except InboundPayment.DoesNotExist:
        logger.error("Inbound payment %s not found during void", inbound_payment_id)


@shared_task(
    name="payments.refund_inbound_payment",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_refund_inbound_payment(
    self,
    inbound_payment_id: str,
    amount: str | None = None,
    payload: dict | None = None,
):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        inbound_payment_transaction = InboundPaymentService.execute_refund(
            inbound_payment_id=inbound_payment_id,
            amount=Decimal(amount) if amount else None,
            extra_payload=payload or {},
            actor=f"celery:refund:{self.request.id}",
        )
        return {
            "inbound_payment_transaction_id": str(inbound_payment_transaction.id),
            "status": inbound_payment_transaction.status,
        }
    except InboundPayment.DoesNotExist:
        logger.error("Inbound payment %s not found during refund", inbound_payment_id)


@shared_task(
    name="payments.process_inbound_payment_callback",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_process_inbound_payment_callback(self, callback_log_id: str):
    from core.services.inbound_payment_service import InboundPaymentService

    try:
        log = InboundPaymentCallbackLog.objects.select_related(
            "inbound_payment_transaction__provider_account__provider",
            "inbound_payment_transaction__inbound_payment__inbound_payment_event",
        ).get(id=callback_log_id)
    except InboundPaymentCallbackLog.DoesNotExist:
        logger.error("Callback log %s disappeared", callback_log_id)
        return

    if log.status == InboundPaymentCallbackLog.Status.PROCESSED:
        logger.info("Callback %s already processed — skipping", log.id)
        return

    log.status = InboundPaymentCallbackLog.Status.PROCESSING
    log.save(update_fields=["status"])

    try:
        InboundPaymentService.process_callback(log)
        log.refresh_from_db()
        if log.status == InboundPaymentCallbackLog.Status.PROCESSING:
            log.status = InboundPaymentCallbackLog.Status.PROCESSED
            log.save(update_fields=["status"])
    except Exception as exc:
        log.status = InboundPaymentCallbackLog.Status.FAILED
        log.processing_error = f"{type(exc).__name__}: {exc!s}"
        log.save(update_fields=["status", "processing_error"])
        raise


@shared_task(
    name="payments.reconcile_inbound_payment_transaction",
    queue="payments.low",
    bind=True,
    **_RECONCILE_RETRY,
)
def task_reconcile_inbound_payment_transaction(self, inbound_payment_transaction_id: str):
    from core.providers.base_provider import ProviderResultStatus
    from core.services.inbound_payment_service import InboundPaymentService
    from core.services.registry import get_provider_instance

    try:
        inbound_payment_transaction = InboundPaymentTransaction.objects.select_related(
            "inbound_payment__inbound_payment_event",
            "provider_account__provider",
        ).get(id=inbound_payment_transaction_id)
    except InboundPaymentTransaction.DoesNotExist:
        logger.error(
            "Inbound payment transaction %s not found during reconciliation",
            inbound_payment_transaction_id,
        )
        return

    if inbound_payment_transaction.is_terminal:
        return

    rec, _ = InboundPaymentReconciliation.objects.get_or_create(
        inbound_payment_transaction=inbound_payment_transaction
    )

    provider_account = inbound_payment_transaction.provider_account
    provider_instance = get_provider_instance(
        class_name=provider_account.provider.class_name,
        config=provider_account.get_config(),
    )

    base_url = settings.BASE_URL
    if not base_url:
        raise ValueError("BASE_URL environment variable is not configured.")

    base_url = base_url.strip().rstrip("/")
    callback_path = (
        "/core/inbound-payments/callbacks/"
        f"{inbound_payment_transaction.provider.slug}/{inbound_payment_transaction.id}/"
    )
    callback_url = urljoin(f"{base_url}/", callback_path.lstrip("/"))

    payload = {"callback_url": callback_url}

    result = provider_instance.query_status(
        provider_transaction_id=inbound_payment_transaction.provider_transaction_id,
        payload=payload,
    )

    rec.attempts += 1
    rec.last_attempted_at = timezone.now()

    if result.status in (ProviderResultStatus.PENDING, ProviderResultStatus.UNKNOWN):
        if self.request.retries >= self.max_retries:
            # Retries exhausted — escalate for operator review.
            rec.status = InboundPaymentReconciliation.Status.MANUAL_REVIEW
            rec.save(update_fields=["attempts", "last_attempted_at", "status"])
            logger.error(
                "Inbound payment transaction %s exhausted %s reconciliation retries; "
                "escalated to manual review",
                inbound_payment_transaction.id,
                self.max_retries,
            )
            return

        rec.save(update_fields=["attempts", "last_attempted_at"])
        raise self.retry(countdown=60 * min(rec.attempts, 10))

    InboundPaymentService.apply_provider_result(
        inbound_payment_transaction=inbound_payment_transaction,
        result=result,
        actor="celery:reconcile",
    )


@shared_task(name="payments.scan_overdue_inbound_payment_transactions", queue="payments.low")
def task_scan_overdue_inbound_payment_transactions():
    overdue_ids = InboundPaymentTransaction.objects.filter(
        status__in=[
            InboundPaymentTransaction.Status.PENDING,
            InboundPaymentTransaction.Status.REQUIRES_ACTION,
        ],
        reconciliation_due_at__lte=timezone.now(),
    ).values_list("id", flat=True)

    count = 0
    for inbound_payment_transaction_id in overdue_ids:
        task_reconcile_inbound_payment_transaction.apply_async(
            kwargs={"inbound_payment_transaction_id": str(inbound_payment_transaction_id)},
        )
        count += 1

    return count


@shared_task(name="payments.expire_stale_inbound_payments", queue="payments.low")
def task_expire_stale_inbound_payments():
    count = InboundPayment.objects.filter(
        expires_at__lt=timezone.now(),
        status__in=[
            InboundPayment.Status.INITIATED,
            InboundPayment.Status.PROCESSING,
        ],
    ).update(status=InboundPayment.Status.EXPIRED)
    return count


@shared_task(name="payments.deliver_webhook", queue="payments.low", bind=True)
def task_deliver_webhook(self, outbox_id: str):
    try:
        outbox = WebhookOutbox.objects.select_related("system").get(id=outbox_id)
    except WebhookOutbox.DoesNotExist:
        logger.error("task_deliver_webhook: outbox %s not found", outbox_id)
        return

    if outbox.status == WebhookOutbox.Status.DELIVERED:
        return

    outbox.status = WebhookOutbox.Status.PROCESSING
    outbox.attempt_count += 1
    outbox.last_attempted_at = timezone.now()
    outbox.save(update_fields=["status", "attempt_count", "last_attempted_at"])

    payload_bytes = json.dumps(outbox.payload).encode()
    signature = ""
    if outbox.system.webhook_secret:
        signature = hmac.new(
            outbox.system.webhook_secret.encode(),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "X-Event-Type": outbox.event_type,
    }

    start = time.monotonic()
    status_code = None
    response_body = ""
    response_headers = {}
    error_message = ""

    try:
        resp = requests.post(
            outbox.destination_url,
            data=payload_bytes,
            headers=headers,
            timeout=15,
        )
        status_code = resp.status_code
        response_body = resp.text[:4000]
        response_headers = dict(resp.headers)
        resp.raise_for_status()
        new_status = WebhookOutbox.Status.DELIVERED
    except Exception as exc:
        error_message = str(exc)
        if outbox.attempt_count >= outbox.max_attempts:
            new_status = WebhookOutbox.Status.EXHAUSTED
        else:
            new_status = WebhookOutbox.Status.FAILED

    duration_ms = int((time.monotonic() - start) * 1000)

    WebhookDeliveryLog.objects.create(
        outbox=outbox,
        attempt_number=outbox.attempt_count,
        request_headers=headers,
        request_payload=outbox.payload,
        response_status_code=status_code,
        response_body=response_body,
        response_headers=response_headers,
        duration_ms=duration_ms,
        error_message=error_message,
    )

    outbox.status = new_status
    if new_status == WebhookOutbox.Status.FAILED:
        # Exponential backoff: 1m, 5m, 15m, 1h, 4h
        delays = [60, 300, 900, 3600, 14400]
        delay = delays[min(outbox.attempt_count - 1, len(delays) - 1)]
        outbox.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    outbox.save(update_fields=["status", "next_attempt_at"])


@shared_task(name="payments.scan_failed_webhooks", queue="payments.low")
def task_scan_failed_webhooks():
    now = timezone.now()
    stale_processing_cutoff = now - timedelta(minutes=5)

    due = WebhookOutbox.objects.filter(
        status=WebhookOutbox.Status.FAILED,
        next_attempt_at__lte=now,
    ).values_list("id", flat=True)

    stale = WebhookOutbox.objects.filter(
        status=WebhookOutbox.Status.PROCESSING,
        last_attempted_at__lte=stale_processing_cutoff,
    ).values_list("id", flat=True)

    count = 0
    for outbox_id in list(due) + list(stale):
        task_deliver_webhook.apply_async(
            kwargs={"outbox_id": str(outbox_id)},
        )
        count += 1

    return count
