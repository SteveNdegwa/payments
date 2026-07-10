---
name: add-endpoint
description: >-
  Add a gateway API endpoint to spin-payments the right way — a thin
  function-based view that returns via ResponseProvider, a route under
  /api/v1/…, API-key auth by default, provider I/O deferred to a Celery task.
  Use when the user wants to "expose X via the API" or add an endpoint.
---

# Add an API endpoint

The API is API-key-authenticated by default (the gateway middleware handles
auth, rate limiting, and logging). Views stay thin. Deep reference:
[`AGENTS.md`](../../../AGENTS.md).

## 1. View (thin)

Add `def handle_x(request: ExtendedRequest, …) -> JsonResponse` to the relevant
app's `views.py` (payments live in `core/views.py`):

- Guard the method: `@require_http_methods(["POST"])` (or GET).
- Read the parsed body via `request.data` (the middleware sets it) and the
  caller via `request.api_client` (the authenticated `System`).
- Parse money as `Decimal(str(raw))`, catching `InvalidOperation` →
  `ResponseProvider.bad_request(...)`.
- Delegate to `PaymentServices` (or the app's service) — **do not** call a
  provider / `requests` / the Stripe SDK inline. Anything that talks to a
  provider gets **queued** as a Celery task (`PaymentServices.queue_*`).
- Return **only** through `ResponseProvider` (`success`/`bad_request`/… ) — the
  uniform `{success, message, data}` envelope. Never a bare `JsonResponse` or a
  leaked exception string.

## 2. Route

Register in the app's `urls.py` (payments: `core/urls.py`, included at
`/api/v1/core/`). Use a descriptive `name=` and typed path converters
(`<str:payment_intent_id>`), matching the existing pattern.

## 3. Auth & the gateway

New routes are authenticated by default — a caller needs a valid `X-Api-Key`.
Only if the endpoint must be public (e.g. an unauthenticated provider callback)
do you add its prefix to `GatewayControlMiddleware.API_CLIENT_VALIDATION_EXEMPT_PATHS`
— and that is a **security decision** to call out in the PR. Never add anything
to `HEALTH_CHECK_PATHS` (which also bypasses rate limiting + logging) unless it
is genuinely a probe.

## 4. Test

Add a test in the app's `tests.py`: hit the route with a valid API key (create a
`System` fixture), assert the `ResponseProvider` envelope shape and status, and
assert a missing/invalid key is rejected by the gateway (401). For queued work,
assert the task is enqueued, not that the provider was called.

## 5. Verify & land

```sh
make check        # ruff + tests
uv run python manage.py check
```

Branch + PR per the template. Hand off to `django-backend-reviewer` (and
`payment-flow-reviewer` if it moves money). **Never commit to `main`/`develop`.**
