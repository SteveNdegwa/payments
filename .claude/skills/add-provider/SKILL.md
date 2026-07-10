---
name: add-provider
description: >-
  Add a new payment provider to spin-payments the right way — a BaseProvider
  subclass returning ProviderResult, registered in the registry, wired to
  Provider/ProviderAccount data, with callbacks verified and money math in
  Decimal. Use when the user wants to "integrate <provider>", add a gateway/PSP,
  or support a new payment rail.
---

# Add a payment provider

Providers are pluggable behind `BaseProvider`. The executor and tasks already
handle status mapping, retries, reconciliation, and webhooks — a new provider
just implements the contract and returns a well-formed `ProviderResult`. Deep
reference: [`AGENTS.md`](../../../AGENTS.md) → architecture + invariants.

## 1. Implement the provider

Create `core/providers/<name>_provider.py` subclassing
`core.providers.base_provider.BaseProvider`. Implement **every** abstract method
— returning a `ProviderResult` where relevant:

- `charge` — single-step charge (mobile money, wallets).
- `authorize` / `capture` / `void` — the auth→capture card flow.
- `refund` — full or partial (set `amount_processed`).
- `query_status` — poll for reconciliation.
- `verify_callback` — authenticate an inbound webhook (**must** be real, not
  `return True`).
- `parse_callback` — translate a callback payload into a `ProviderResult`.

Rules:
- Amounts are `Decimal`; currency is explicit. Never `float`.
- For `REQUIRES_ACTION` (3DS redirect, STK push), populate `next_action`.
- Read credentials/config from `self.credentials` / `self.config` (injected from
  the `ProviderAccount`) — never hard-code keys, never log them.
- Map provider outcomes to the right `ProviderResultStatus`
  (`SUCCESS`/`PENDING`/`REQUIRES_ACTION`/`FAILED`/`UNKNOWN`). Don't stuff raw
  secrets into `raw_response` if it may be returned to a caller.

## 2. Register it

Either decorate the class with `@register_provider("core.providers.<name>_provider.<Class>")`
or rely on lazy import — `get_provider_instance` imports by the `class_name`
stored on the `Provider` row. Make sure the dotted path resolves.

## 3. Wire the data

The provider is selected at runtime via DB rows, not code:
- A `Provider` row with `class_name` = the importable path above.
- One or more `ProviderAccount` rows carrying `credentials` + `extra_config`
  (and `is_default` where relevant), linked to the account type.
- The `ChargeableEvent` / routing that points a `System` at this provider.

For deployed envs, provider credentials come from **Vault** (see
`helm/{stage,prod}.yml` `secretEnv`) — add keys there, don't commit secrets.

## 4. Test

Add tests in `core/tests.py` that:
- decode a real sample callback and assert `verify_callback` rejects a tampered
  one and accepts a valid one;
- assert `parse_callback` and `query_status` produce the right
  `ProviderResultStatus`;
- assert a **retried** charge/capture doesn't move money twice (idempotency).

Mock the network — never hit a live provider in tests.

## 5. Verify & land

```sh
make check        # ruff (lint + format) + tests
uv run python manage.py makemigrations --check --dry-run   # if you touched models
```

Branch + PR per `.github/PULL_REQUEST_TEMPLATE.md`. Note new Vault keys / config
under a "Deploy notes" heading. **Never commit to `main` or `develop`** — hand
off to `payment-flow-reviewer` first.
