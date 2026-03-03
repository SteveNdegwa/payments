import hashlib
import hmac
import json
import logging
import os
import time
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urljoin

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from core.models import (
    PaymentIntent,
    ReconciliationRecord,
    Transaction,
    WebhookOutbox,
    WebhookDeliveryLog, ProviderCallbackLog,
)

logger = logging.getLogger(__name__)


_PROVIDER_RETRY = dict(
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
)

_RECONCILE_RETRY = dict(
    max_retries=10,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)

_WEBHOOK_RETRY = dict(
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=3600,
    retry_jitter=True,
)


@shared_task(name="payments.charge", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_charge(self, payment_intent_id: str):
    from core.services.payment_services import PaymentServices
    try:
        txn = PaymentServices.execute_charge(
            payment_intent_id=payment_intent_id,
            extra_payload={},
            actor=f"celery:charge:{self.request.id}",
        )
        return {"transaction_id": str(txn.id), "status": txn.status}
    except PaymentIntent.DoesNotExist:
        logger.error("task_charge: PI %s not found", payment_intent_id)


@shared_task(name="payments.authorize", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_authorize(self, payment_intent_id: str):
    from core.services.payment_services import PaymentServices
    try:
        txn = PaymentServices.execute_authorize(
            payment_intent_id=payment_intent_id,
            extra_payload={},
            actor=f"celery:authorize:{self.request.id}",
        )
        return {"transaction_id": str(txn.id), "status": txn.status}
    except PaymentIntent.DoesNotExist:
        logger.error("task_authorize: PI %s not found", payment_intent_id)


@shared_task(name="payments.capture", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_capture(
    self,
    payment_intent_id: str,
    amount: str | None = None,
    payload: dict | None = None,
):
    from core.services.payment_services import PaymentServices
    try:
        txn = PaymentServices.execute_capture(
            payment_intent_id=payment_intent_id,
            amount=Decimal(amount) if amount else None,
            extra_payload=payload or {},
            actor=f"celery:capture:{self.request.id}",
        )
        return {"transaction_id": str(txn.id), "status": txn.status}
    except PaymentIntent.DoesNotExist:
        logger.error("task_capture: PI %s not found", payment_intent_id)


@shared_task(name="payments.void", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_void(self, payment_intent_id: str, payload: dict | None = None):
    from core.services.payment_services import PaymentServices
    try:
        txn = PaymentServices.execute_void(
            payment_intent_id=payment_intent_id,
            extra_payload=payload or {},
            actor=f"celery:void:{self.request.id}",
        )
        return {"transaction_id": str(txn.id), "status": txn.status}
    except PaymentIntent.DoesNotExist:
        logger.error("task_void: PI %s not found", payment_intent_id)


@shared_task(name="payments.refund", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_refund(
    self,
    payment_intent_id: str,
    amount: str | None = None,
    payload: dict | None = None,
):
    from core.services.payment_services import PaymentServices
    try:
        txn = PaymentServices.execute_refund(
            payment_intent_id=payment_intent_id,
            amount=Decimal(amount) if amount else None,
            extra_payload=payload or {},
            actor=f"celery:refund:{self.request.id}",
        )
        return {"transaction_id": str(txn.id), "status": txn.status}
    except PaymentIntent.DoesNotExist:
        logger.error("task_refund: PI %s not found", payment_intent_id)


@shared_task(
    name="payments.process_provider_callback",
    queue="payments.high",
    bind=True,
    **_PROVIDER_RETRY,
)
def task_process_provider_callback(self, callback_log_id: str):
    from core.services.payment_services import PaymentServices

    try:
        log = ProviderCallbackLog.objects.select_related(
            "transaction__provider_account__provider",
            "transaction__payment_intent__chargeable_event",
        ).get(id=callback_log_id)
    except ProviderCallbackLog.DoesNotExist:
        logger.error("Callback log %s disappeared", callback_log_id)
        return

    if log.status == ProviderCallbackLog.Status.PROCESSED:
        logger.info("Callback %s already processed — skipping", log.id)
        return

    log.status = ProviderCallbackLog.Status.PROCESSING
    log.save(update_fields=["status"])

    try:
        PaymentServices.process_provider_callback(log)
        log.status = ProviderCallbackLog.Status.PROCESSED
        log.save(update_fields=["status"])
    except Exception as exc:
        log.status = ProviderCallbackLog.Status.FAILED
        log.processing_error = f"{type(exc).__name__}: {str(exc)}"
        log.save(update_fields=["status", "processing_error"])


@shared_task(
    name="payments.reconcile_transaction",
    queue="payments.low",
    bind=True,
    **_RECONCILE_RETRY,
)
def task_reconcile_transaction(self, transaction_id: str):
    from core.providers.base_provider import ProviderResultStatus
    from core.services.registry import get_provider_instance
    from core.services.payment_services import PaymentServices
    try:
        txn = Transaction.objects.select_related(
            "payment_intent__chargeable_event",
            "provider_account__provider",
        ).get(id=transaction_id)
    except Transaction.DoesNotExist:
        logger.error("Reconcile: TXN %s not found", transaction_id)
        return

    if txn.is_terminal:
        return

    rec, _ = ReconciliationRecord.objects.get_or_create(transaction=txn)

    pa = txn.provider_account
    provider_instance = get_provider_instance(
        class_name=pa.provider.class_name,
        credentials=pa.credentials,
        config=pa.extra_config,
    )

    # base_url = os.getenv("BASE_URL")
    base_url = settings.BASE_URL
    if not base_url:
        raise ValueError("BASE_URL environment variable is not configured.")

    base_url = base_url.strip().rstrip("/")
    callback_path = f"/core/payments/callbacks/{txn.provider.slug}/{txn.id}/"
    callback_url = urljoin(f"{base_url}/", callback_path.lstrip("/"))

    payload = {
        "callback_url": callback_url
    }

    result = provider_instance.query_status(
        provider_transaction_id=txn.provider_transaction_id,
        payload=payload
    )

    rec.attempts += 1
    rec.last_attempted_at = timezone.now()

    if result.status in (ProviderResultStatus.PENDING, ProviderResultStatus.UNKNOWN):
        if self.request.retries >= self.max_retries:
            # Retries exhausted — escalate for operator review.
            rec.status = ReconciliationRecord.Status.MANUAL_REVIEW
            rec.save(update_fields=["attempts", "last_attempted_at", "status"])
            logger.error(
                "Reconcile: TXN %s exhausted %s retries without a final status — "
                "escalated to MANUAL_REVIEW", txn.id, self.max_retries,
            )
            return

        rec.save(update_fields=["attempts", "last_attempted_at"])
        raise self.retry(countdown=60 * min(rec.attempts, 10))

    PaymentServices.apply_provider_result(
        txn=txn,
        result=result,
        actor="celery:reconcile",
    )


@shared_task(name="payments.scan_overdue_reconciliations", queue="payments.low")
def task_scan_overdue_reconciliations():
    overdue_ids = Transaction.objects.filter(
        status__in=[Transaction.Status.PENDING, Transaction.Status.REQUIRES_ACTION],
        reconciliation_due_at__lte=timezone.now(),
    ).values_list("id", flat=True)

    count = 0
    for txn_id in overdue_ids:
        task_reconcile_transaction.apply_async(
            kwargs={"transaction_id": str(txn_id)},
        )
        count += 1

    return count


@shared_task(name="payments.expire_stale_intents", queue="payments.low")
def task_expire_stale_intents():
    count = PaymentIntent.objects.filter(
        expires_at__lt=timezone.now(),
        status__in=[
            PaymentIntent.Status.INITIATED,
            PaymentIntent.Status.PENDING,
            PaymentIntent.Status.PROCESSING,
        ],
    ).update(status=PaymentIntent.Status.EXPIRED)
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

