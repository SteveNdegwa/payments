---
name: add-model
description: >-
  Add or change a Django model in spin-payments the right way — extend BaseModel,
  Decimal for money, generate + commit the migration, register in admin. Use when
  the user wants to add a table/field, change a model, or "store X".
---

# Add / change a model

Deep reference: [`AGENTS.md`](../../../AGENTS.md).

## 1. Model

Add the model to the owning app's `models.py`, subclassing
`base.models.BaseModel` (it supplies the shared PK, timestamps, and audit hooks —
don't re-declare those). Conventions:

- **Money is `DecimalField`** (`max_digits`/`decimal_places` per the existing
  money fields, e.g. `max_digits=20, decimal_places=6`) — never `FloatField`.
- Status/type fields use `models.TextChoices` (see `PaymentIntent.Status`,
  `Transaction.Status`), not free strings.
- FKs use explicit `on_delete`; add `related_name` where it reads better.
- Add DB indexes / `unique` / `unique_together` for anything you filter or upsert
  on (idempotency keys, external references).
- Keep secret-bearing fields out of `__str__`/`repr` and out of anything logged.

## 2. Migration

```sh
make makemigrations        # generates <app>/migrations/NNNN_*.py
make migrate               # apply locally
```

Commit the generated migration. Keep it applying cleanly and reversibly. **Do
not hand-edit a generated migration to satisfy ruff** — ruff already ignores
`**/migrations/*`. If a field is added to a non-empty table, provide a sensible
default or a data migration.

Sanity check that nothing was missed:

```sh
uv run python manage.py makemigrations --check --dry-run   # exits non-zero if a model change has no migration
```

## 3. Admin

Register the model in the app's `admin.py` so it's manageable at `/cia/` —
`list_display`, `search_fields`, and `readonly_fields` for computed/secret
columns, following the existing admin classes.

## 4. Verify & land

```sh
make check        # ruff + tests
```

Add/adjust a test in the app's `tests.py` for any non-trivial method or
constraint. Branch + PR per the template; list the new migration under a
"Migrations" heading. **Never commit to `main`/`develop`.**
