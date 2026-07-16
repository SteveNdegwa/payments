"""
Usage:
  celery -A payments worker -Q payments.high -c 4 --loglevel=info
  celery -A payments worker -Q payments.low  -c 2 --loglevel=info
  celery -A payments beat   --loglevel=info
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "payments.settings")

app = Celery("payments")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.beat_scheduler = "django_celery_beat.schedulers:DatabaseScheduler"
app.autodiscover_tasks()
