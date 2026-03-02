"""
Usage:
  celery -A your_project worker -Q payments.high -c 4 --loglevel=info
  celery -A your_project worker -Q payments.low  -c 2 --loglevel=info
  celery -A your_project beat   --loglevel=info
"""

import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "spin_payments.settings")

app = Celery("payments")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# Beat schedule
app.conf.beat_schedule = {
    # Reconcile any overdue async transactions every minute
    "scan-overdue-reconciliations": {
        "task": "payments.scan_overdue_reconciliations",
        "schedule": 60.0,
        "options": {"queue": "payments.low"},
    },
    # Re-queue failed webhook deliveries past their next_attempt_at
    "scan-failed-webhooks": {
        "task": "payments.scan_failed_webhooks",
        "schedule": 60.0,
        "options": {"queue": "payments.low"},
    },
    # Expire any stale INITIATED / PENDING intents every hour
    "expire-stale-payment-intents": {
        "task": "payments.expire_stale_intents",
        "schedule": crontab(minute=0),
        "options": {"queue": "payments.low"},
    },
}


# Queue routing
app.conf.task_routes = {
    "payments.charge": {"queue": "payments.high"},
    "payments.authorize": {"queue": "payments.high"},
    "payments.capture": {"queue": "payments.high"},
    "payments.void": {"queue": "payments.high"},
    "payments.refund": {"queue": "payments.high"},
    "payments.reconcile_transaction": {"queue": "payments.low"},
    "payments.scan_overdue_reconciliations": {"queue": "payments.low"},
    "payments.deliver_webhook": {"queue": "payments.low"},
    "payments.scan_failed_webhooks": {"queue": "payments.low"},
    "payments.expire_stale_intents": {"queue": "payments.low"},
}