---
name: add-task
description: >-
  Add a Celery task to spin-payments the right way — named, queue-routed to
  payments.high/low, idempotent, with a retry/backoff policy, and (if periodic)
  scheduled via beat. Use when the user wants background/async work, a scheduled
  job, or to offload provider I/O from a request.
---

# Add a Celery task

Provider I/O and other slow/retryable work runs in Celery, never on the request
path. Deep reference: [`AGENTS.md`](../../../AGENTS.md) and the existing
`core/tasks.py`.

## 1. Write the task

Add to `core/tasks.py` (or the owning app):

```python
@shared_task(name="payments.<verb>", queue="payments.high", bind=True, **_PROVIDER_RETRY)
def task_<verb>(self, <id>: str, ...):
    from core.services.payment_services import PaymentServices  # import inside to avoid cycles
    try:
        ...  # delegate to a service; keep the task a thin, idempotent wrapper
    except <Model>.DoesNotExist:
        logger.error("task_<verb>: %s not found", <id>)
```

Rules:
- **Name it** (`name="payments.<verb>"`) and **route it**: `payments.high` for
  money movement (charge/authorize/capture/void/refund/callback), `payments.low`
  for reconcile / webhooks / periodic scans. The Helm worker consumes both
  queues — a task on an unlisted queue never runs.
- **Idempotent.** A task can be retried or delivered twice — re-running it must
  not move money or duplicate an effect twice. Guard with the idempotency key,
  a status check, or a unique constraint.
- **Retry policy** via the shared dicts (`_PROVIDER_RETRY`, `_RECONCILE_RETRY`,
  `_WEBHOOK_RETRY`) or `self.retry(...)`. Bound retries; escalate a stuck item
  (e.g. `ReconciliationRecord.Status.MANUAL_REVIEW`) rather than looping forever.
- Pass **IDs, not ORM objects**, as task args (serialised JSON). Re-fetch with
  `select_related` inside the task.
- Delegate the real logic to `PaymentServices`/a service; keep the task thin.

## 2. Enqueue it

From a view/service: `task_<verb>.apply_async(kwargs={"<id>": str(obj.id)})`
(or `.delay(...)`). Views enqueue and return immediately.

## 3. Schedule it (only if periodic)

For a recurring job (like `scan_overdue_reconciliations`), add a
`django_celery_beat` periodic-task entry. Prefer configuring it in the DB via the
`/cia` admin (`PeriodicTask` + `IntervalSchedule`/`CrontabSchedule`) since the
schedule uses the `DatabaseScheduler` — document the interval in the PR. Ensure
`make beat` is running to dispatch it.

## 4. Verify & land

```sh
make check                 # ruff + tests
make worker                # exercise the task against a local RabbitMQ (make up)
```

Add a test asserting the task is enqueued from its caller, and that a repeat run
is a no-op (idempotency). Branch + PR per the template; hand off to
`payment-flow-reviewer` if it moves money. **Never commit to `main`/`develop`.**
