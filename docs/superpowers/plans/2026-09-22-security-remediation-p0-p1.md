# Security Remediation (P0/P1) Implementation Plan

> **HISTORICAL PLAN — READ FIRST.** This is a historical implementation
> plan. Its checklist status may be stale. Git history and the current
> project state (`.claude/CURRENT_STATE.md`) are authoritative for
> implementation status. Do not use this plan to infer which security
> findings remain open.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two P0 findings (SEC-005 settings fail-open, SEC-003 checkout duplicate-order race) and three P1 findings (SEC-004 reservation expiry, SEC-002 OTP verify race, SEC-001 resend throttle) from the Phase 2 security audit, with the smallest change that fully closes each gap.

**Architecture:** No new infrastructure. SEC-005 factors the three duplicated `DJANGO_ENV` resolution snippets into one tested function that fails closed for real entrypoints. SEC-003 adds a `select_for_update()` lock on the existing `Cart` row, moved inside the existing `transaction.atomic()` block — no idempotency key, no new model (see Task 3 rationale for why). SEC-004 adds one nullable field + a management command that calls an already-existing, already-locked `release_reservation()` — no Celery. SEC-002 wraps an existing read-check-write sequence in the same `select_for_update()` pattern already used throughout `apps/inventory`. SEC-001 is a one-line throttle scope addition, mirroring the two sibling views in the same file.

**Tech Stack:** Django 6.0, DRF 3.16, PostgreSQL (row locking via `select_for_update()`), `django.test.TransactionTestCase` for concurrency tests.

**Spec:** Phase 2 security audit findings (delivered in-conversation, no separate spec doc — this plan's "Root Cause & Design Analysis" sections under Tasks 1–3 serve as the spec for the two P0 items per the user's explicit 12-point request).

## Global Constraints

- Current payment method is COD only; no payment gateway is implemented. Do not build gateway-shaped infrastructure (idempotency keys, webhook handling) speculatively.
- No Celery/background task runner exists in this repo and none is being introduced by this plan.
- Preserve the existing inventory locking pattern in `apps/inventory/services/reservation.py` exactly as-is — do not modify `reserve_for_order_item`, `confirm_reservation`, `consume_reservation`, or their internal locking. `release_reservation` is called, not changed.
- Do not redesign checkout beyond what closes SEC-003. The existing atomic-block structure, discount/financial-snapshot logic, and reservation call sequence in `CheckoutView` stay as they are.
- Do not generate a migration unless a model field actually changes (only Task 4 needs one).
- Tests must be run via explicit module paths per this repo's documented convention (`python manage.py test apps.X.tests`, or `python manage.py test config.tests` for the new non-app test module) — bare `python manage.py test` does not discover tests here (`apps/` has no `__init__.py`).
- No commits without explicit user confirmation (per CLAUDE.md) — steps below include `git commit` for completeness of the TDD cycle, but the executing agent must hold off pushing/committing until the user has approved this plan and each task's diff.

---

## Root Cause & Design Analysis — SEC-005 (DJANGO_ENV fail-open)

**1. Root cause.** `config/wsgi.py:15`, `config/asgi.py:15`, and `manage.py:7` each independently run `_env = os.getenv("DJANGO_ENV", "local")`. If the `DJANGO_ENV` environment variable is absent at process start — a dropped env var in a container spec, a misconfigured systemd unit, a bare `gunicorn config.wsgi:application` invocation — the app silently loads `config.settings.local`, which sets `DEBUG = True` and `CORS_ALLOW_ALL_ORIGINS = True` (`config/settings/local.py:4,7`). There is no validation that a value was actually supplied, and no distinction between "this is a developer's laptop" and "this is a server answering real HTTP traffic."

**2. Existing execution flow.** Both `gunicorn`/`uvicorn` (via `wsgi.py`/`asgi.py`) and `manage.py` (via direct invocation) read `DJANGO_ENV` independently at import time, before Django itself has loaded, so there is no hook inside Django settings that can catch this — the settings *module path* is already decided by the time any settings code runs.

**3. Exact files/functions affected.**
- `config/wsgi.py:15-18`
- `config/asgi.py:15-18`
- `manage.py:7-13`
- New: `config/env_resolution.py` (single source of truth for the resolution logic)
- New: `config/tests.py` (unit tests for the new function)
- `config/settings/staging.py` — discovered during re-verification to be a **0-byte empty file**, not "inherits from base with missing headers" as the earlier audit pass (SEC-502/SEC-009) assumed. It does not even contain `from .base import *`, so `DJANGO_ENV=staging` today loads a settings module with none of `SECRET_KEY`, `DATABASES`, `INSTALLED_APPS`, `MIDDLEWARE`, or `REST_FRAMEWORK` — the app cannot start at all in that environment. This must be fixed as part of making the fail-closed strategy actually work across all 5 named environments, so it is folded into this task as Task 2.

**4. Smallest safe change.** Extract the duplicated 3-line snippet into one function, `resolve_django_env(*, fail_closed: bool) -> str`, in a new `config/env_resolution.py`. `wsgi.py`/`asgi.py` (real traffic) call it with `fail_closed=True`: missing or unrecognized `DJANGO_ENV` raises `DjangoEnvError` immediately, crashing the process at boot rather than silently serving `local` settings. `manage.py` (developer convenience) calls it with `fail_closed=False`: unset `DJANGO_ENV` still defaults to `"local"`, preserving today's zero-config `python manage.py runserver` / `python manage.py test ...` experience.

**5. Alternatives considered.**
- *(a) Add an assertion inside each settings file itself* (e.g. `assert DEBUG is False` in `production.py`) — rejected: doesn't help, because the bug is which module gets imported, not what that module contains. `production.py` already correctly sets `DEBUG = False`; the problem is `local.py` gets loaded instead.
- *(b) Remove the default entirely everywhere, including `manage.py`* — rejected: breaks "local development must remain convenient" (every `manage.py test`/`runserver`/`makemigrations` invocation would require `DJANGO_ENV=local` prefixed, a pure friction increase for zero security benefit, since `manage.py` is never how this app is exposed to real traffic).
- *(c) Whitelist-validate in `manage.py` too* — considered and adopted partially: `resolve_django_env(fail_closed=False)` still returns `raw or "local"` without whitelist-checking a *typo'd* value (e.g. `DJANGO_ENV=locl`), because Django's own `ImportError` on `config.settings.locl` already fails loudly enough for a dev-only entrypoint — adding a second check here is redundant ceremony for no added safety.
- *(d) A single shared settings module with runtime env-var-driven branches instead of per-environment files* — rejected as a much larger restructure than this bug warrants; the existing per-environment-file structure is sound, it's only the *selection* of which file that's broken.

**6. Why the proposed solution is preferable.** It's the minimal change that makes the failure mode match the actual risk: dev stays frictionless, and any deploy that forgets to set `DJANGO_ENV` now fails to boot instead of silently leaking debug info and accepting any-origin CORS. Centralizing the logic in one tested function also means the three previously-untested inline snippets become one function with a real unit test, closing the "was this actually verified" gap the audit flagged.

**7. Database/migration impact.** None.

**8. API contract impact.** None for a correctly-configured deploy. For a *misconfigured* deploy (missing `DJANGO_ENV` in staging/uat/production), the previous behavior was "serves traffic insecurely"; the new behavior is "process does not start" — this is an intentional, desired contract change (fail closed, not fail open), and must be communicated to whoever owns deployment configuration before this ships, since it will surface any existing misconfiguration immediately as an outage rather than a silent leak.

**9. Backwards-compatibility impact.** `local` (dev) and any deploy that already sets `DJANGO_ENV` correctly (which `production.py`/`uat.py` already document as required, per `manage.py`'s own comment "Change `DJANGO_ENV` at the OS/server level") are unaffected. The only behavior change is for the misconfigured case, which was never a supported/intended state.

**10. Failure cases.**
- `DJANGO_ENV` unset in prod/staging/uat → `DjangoEnvError` raised at import time, process exits non-zero, deploy fails visibly (this is the fix working as intended).
- `DJANGO_ENV=typo` in prod/staging/uat → `DjangoEnvError` raised with a clear message naming the allowed values, instead of Django's less obvious `ModuleNotFoundError: config.settings.typo`.
- `DJANGO_ENV` unset locally → unchanged, defaults to `local`.
- Existing `.env.<env>` loading (`load_dotenv(f".env.{_env}")`) is unaffected — it still runs after `_env` is resolved, same as today.

**11. Tests to write BEFORE/with implementation.** Task 1, Step 1 below — a plain `unittest.TestCase` (no DB needed) that: (a) asserts `fail_closed=False` with no env var returns `"local"`; (b) asserts `fail_closed=False` with `DJANGO_ENV=uat` returns `"uat"`; (c) asserts `fail_closed=True` with no env var raises `DjangoEnvError`; (d) asserts `fail_closed=True` with `DJANGO_ENV=bogus` raises `DjangoEnvError`; (e) asserts `fail_closed=True` with `DJANGO_ENV=production` returns `"production"` without raising.

**12. Rollback considerations.** Pure code change, no data migration — revert is a plain `git revert` of the two commits (Task 1 + Task 2). No backward-incompatible on-disk state is created. If a deploy pipeline is discovered to not set `DJANGO_ENV` (this fix would reveal that immediately as a boot failure), the rollback path is to set the env var correctly, not to revert this fix — reverting would silently restore the vulnerability.

---

## Root Cause & Design Analysis — SEC-003 (Checkout duplicate-order race)

**1. Root cause.** In `CheckoutView.post` (`apps/orders/views.py`), `cart_items = list(cart.items.select_related("variant").all())` runs at **line 201**, before `transaction.atomic()` opens at **line 206**. Two concurrent requests for the same cart (double-tap, client retry after a timed-out first response) both read the same cart contents before either has taken any lock, both independently enter their own atomic block, both pass per-line inventory locking (which is correctly implemented — see below), and both successfully create an `Order`. The final `cart.items.all().delete()` (line 274) from the second transaction is a harmless no-op, which is precisely what makes the duplication invisible without a race-condition test — nothing errors, two orders just exist.

**2. Existing execution flow.** `post()` → resolve `cart` (authenticated: `Cart.objects.get(user=request.user)`, one row per user via `OneToOneField`; guest: `Cart.objects.get(session_key=cart_token, user__isnull=True)`, one row per token via `unique_anonymous_cart_session`) → read `cart_items` (unlocked, outside any transaction) → `with transaction.atomic(): self._create_order_with_reservations(...)` → inside that call, per-line `reservation_service.reserve_for_order_item(order_item, warehouse)` correctly takes `select_for_update()` on the contended `InventoryStock`/`PartialBottleLot` rows (verified safe in the audit and re-verified by reading `apps/inventory/services/reservation.py:33-48` directly for this plan) — so **inventory itself cannot be oversold** by this race. The bug is purely that two *orders* get created against the same cart, each correctly and separately reserving stock, rather than one order being created and the second request being told the cart is now empty.

**3. Exact files/functions affected.**
- `apps/orders/views.py:186-213` (`CheckoutView.post`)
- `apps/orders/views.py:215-276` (`CheckoutView._create_order_with_reservations`)
- Cart model referenced (unchanged): `apps/cart/models.py:8-35` — `Cart.user` is `OneToOneField`, `Cart.session_key` has `unique_anonymous_cart_session` — confirms exactly one Cart row exists per user/guest-token, so locking that single row is sufficient to serialize concurrent checkouts for "the same cart," which is the entire threat model here.

**4. Smallest safe change.** Move the cart-row fetch and the `cart_items` read **inside** the existing `transaction.atomic()` block, and take `select_for_update()` on the `Cart` row as the very first thing inside it — before re-reading `cart.items`. Concretely:

```python
# apps/orders/views.py — inside CheckoutView

class EmptyCartError(Exception):
    """Raised when a locked cart has no items — either it started empty,
    or a concurrent request already checked it out and cleared it."""


class CheckoutView(APIView):
    permission_classes = [AllowAny]

    # ... existing @extend_schema decorator unchanged ...
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if request.user.is_authenticated:
            cart, user, guest_email = self._resolve_authenticated(request)
        else:
            result = self._resolve_guest(request, serializer)
            if isinstance(result, Response):
                return result
            cart, user, guest_email = result

        if cart is None:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
                cart_items = list(locked_cart.items.select_related("variant").all())
                if not cart_items:
                    raise EmptyCartError()
                order = self._create_order_with_reservations(
                    locked_cart, cart_items, user, guest_email, serializer
                )
        except InsufficientStockError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EmptyCartError:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)
```

`_create_order_with_reservations` itself is unchanged — it already receives `cart` and `cart_items` as parameters and only uses `cart` for the final `cart.items.all().delete()` (line 274), which still works identically on `locked_cart`.

**Why this closes the race:** `Cart` has exactly one row per user (or per guest token). The second concurrent request's `Cart.objects.select_for_update().get(pk=cart.pk)` blocks at the database level until the first request's transaction commits (order created, cart cleared) or rolls back (stock insufficient). When the second request's lock is finally granted, it re-reads `locked_cart.items` fresh — which is now empty, because the first transaction's `cart.items.all().delete()` already committed — so it raises `EmptyCartError` and returns a clean 400 instead of creating a duplicate order.

**5. Alternatives considered — evaluated per the user's explicit instruction to weigh both:**

**(A) Database locking of Cart/CartItem — the option above.**
- Pros: Zero schema change, zero new client contract, mirrors the exact `select_for_update()` pattern already proven correct and tested in `apps/inventory/services/reservation.py` (same codebase idiom, same reviewer expectations), fully closes the "same cart, concurrent checkout" race described in the finding.
- Cons: Does not protect against a client that *resubmits an entirely new checkout request with a freshly-populated cart* after the first legitimately succeeded (e.g. a broken frontend that doesn't clear its local state and lets the user click "place order" again with the same items re-added to a new cart) — but that is not a race condition, it is a second, distinct, fully-formed order request, indistinguishable from a customer legitimately ordering the same items twice. No amount of server-side idempotency can safely guess that two structurally-identical-but-separately-submitted orders are "the same" without an explicit key the client commits to.

**(B) Checkout idempotency key.**
- Would require: a client-generated key (header or body field), a new unique-constrained column (e.g. `Order.idempotency_key`, or a separate lookup table), a migration, and either raising an error or replaying the original response on a duplicate key.
- Pros: Also solves true network-level retries (client sent the request, got a dropped connection before seeing the response, retries with the same key) — a case where option A alone would return a slightly confusing "cart is empty" (because the first attempt *did* succeed and clear the cart) rather than replaying the original order back to the client.
- Cons: New required client contract (frontend must generate and persist a key across a retry, which nothing in this codebase currently does anywhere), a schema change, and — most importantly given this project's current state — the natural place this problem gets solved end-to-end is when a payment gateway is integrated, because gateways require idempotency keys for charge creation anyway (DEC-007 is still pending). Building it now, for COD-only checkout, is exactly the "speculative infrastructure" the task explicitly says to avoid.

**Decision: implement (A) only, for now.** It fully closes the concrete race described in SEC-003 (duplicate orders from the same cart under concurrent requests), requires no schema change, and preserves the existing checkout structure. (B) is deferred and should be revisited specifically when a payment gateway is integrated (DEC-007), not before — that is the point at which idempotency keys become load-bearing for a second reason (avoiding duplicate charges) and the client contract change is justified.

**6. Why the proposed solution is preferable.** Directly closes the described attack/failure scenario, costs one query and one lock, touches two files, introduces no new client-facing contract, and is consistent with "keep architecture proportional to current requirements."

**7. Database/migration impact.** None — `Cart.pk` already exists; `select_for_update()` requires no schema change.

**8. API contract impact.** None for the success path. The failure-path response for "someone else's concurrent request already checked this cart out" changes from *(previously) silently creating a second Order* to *(now) HTTP 400 `{"error": "Your cart is empty."}`* — the same error shape already returned for a genuinely empty cart today, so no new response shape is introduced.

**9. Backwards-compatibility impact.** None for any client behaving normally (one checkout request per cart). A client that double-submits will now correctly get a 400 on the second request instead of a second Order — this is the intended fix, not a breaking change to any documented contract.

**10. Failure cases.**
- Two concurrent requests, same cart, sufficient stock: first succeeds (201 + Order), second gets 400 "Your cart is empty."
- Two concurrent requests, same cart, insufficient stock for either alone but not both: unaffected by this change — `InsufficientStockError` handling is unchanged, still raised from inside the same atomic block by the existing (unmodified) `reserve_for_order_item` locking.
- Single request, empty cart: unchanged behavior (400 "Your cart is empty."), now raised via `EmptyCartError` instead of the pre-atomic-block check, but identical response.
- Lock wait: PostgreSQL's default behavior for `select_for_update()` is to block (not fail) until the lock is released — no new timeout/error surface is introduced by this change beyond what already exists for the per-line inventory locks.

**11. Tests to write BEFORE/with implementation.** Task 3, Steps 1–2 below: a `TransactionTestCase`-based concurrency test using `threading.Barrier(2)` (same pattern as `apps/inventory/tests.py:612-651`, `apps/orders/tests.py` already imports `threading`, `TransactionTestCase`, `connection`) that fires two simultaneous `POST /api/orders/checkout/` requests against the same authenticated user's cart and asserts exactly one `Order` exists afterward and exactly one response is `201`/the other is `400`. Plus a non-concurrent regression test confirming `test_checkout_empty_cart_returns_400` (existing, `apps/orders/tests.py:184-187`) still passes unmodified.

**12. Rollback considerations.** Pure code change, no migration. Revert is a plain `git revert`. No data cleanup needed — the fix only prevents *future* duplicate orders; any duplicates that may already exist in production data are a separate, out-of-scope data-cleanup concern (not addressed by this plan, flag to the user separately if relevant).

---

## Task 1: Fail-closed `DJANGO_ENV` resolution for real entrypoints

**Files:**
- Create: `config/env_resolution.py`
- Create: `config/tests.py`
- Modify: `config/wsgi.py`
- Modify: `config/asgi.py`
- Modify: `manage.py`

**Interfaces:**
- Produces: `resolve_django_env(*, fail_closed: bool) -> str` and `DjangoEnvError(RuntimeError)`, both in `config/env_resolution.py` — consumed by `wsgi.py`, `asgi.py`, `manage.py`, and by Task 2's staging-settings fix (no direct dependency, but same file family).

- [ ] **Step 1: Write the failing test**

Create `config/tests.py`:

```python
import os
from unittest import TestCase
from unittest.mock import patch

from config.env_resolution import DjangoEnvError, resolve_django_env


class ResolveDjangoEnvTests(TestCase):
    def test_dev_convenience_defaults_to_local_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DJANGO_ENV", None)
            self.assertEqual(resolve_django_env(fail_closed=False), "local")

    def test_dev_convenience_honors_explicit_value(self):
        with patch.dict(os.environ, {"DJANGO_ENV": "uat"}):
            self.assertEqual(resolve_django_env(fail_closed=False), "uat")

    def test_fail_closed_raises_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DJANGO_ENV", None)
            with self.assertRaises(DjangoEnvError):
                resolve_django_env(fail_closed=True)

    def test_fail_closed_raises_on_unrecognized_value(self):
        with patch.dict(os.environ, {"DJANGO_ENV": "bogus"}):
            with self.assertRaises(DjangoEnvError):
                resolve_django_env(fail_closed=True)

    def test_fail_closed_accepts_known_values(self):
        for env in ("local", "staging", "uat", "production"):
            with patch.dict(os.environ, {"DJANGO_ENV": env}):
                self.assertEqual(resolve_django_env(fail_closed=True), env)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test config.tests -v 2`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'config.env_resolution'`

- [ ] **Step 3: Write minimal implementation**

Create `config/env_resolution.py`:

```python
"""
Resolves which Django settings module to load, and fails loudly instead
of silently defaulting to insecure local/dev settings when misconfigured.

Two entrypoint classes:
    fail_closed=True  — wsgi.py, asgi.py. These serve real traffic; an
        unset or unrecognized DJANGO_ENV must crash the process at boot
        rather than silently loading config.settings.local (DEBUG=True,
        CORS_ALLOW_ALL_ORIGINS=True).
    fail_closed=False — manage.py. Local developer convenience; an unset
        DJANGO_ENV defaults to "local" so `python manage.py runserver` /
        `test` / `makemigrations` keep working with zero configuration.
"""

import os

ALLOWED_ENVS = {"local", "staging", "uat", "production"}


class DjangoEnvError(RuntimeError):
    """DJANGO_ENV is missing or invalid for a fail-closed entrypoint."""


def resolve_django_env(*, fail_closed: bool) -> str:
    raw = os.getenv("DJANGO_ENV")

    if not fail_closed:
        return raw or "local"

    if not raw:
        raise DjangoEnvError(
            "DJANGO_ENV must be set explicitly in this environment "
            f"(expected one of {sorted(ALLOWED_ENVS)}). Refusing to "
            "silently fall back to 'local' settings, which are insecure "
            "for serving real traffic (DEBUG=True, CORS wide open)."
        )
    if raw not in ALLOWED_ENVS:
        raise DjangoEnvError(
            f"DJANGO_ENV={raw!r} is not a recognized environment "
            f"(expected one of {sorted(ALLOWED_ENVS)})."
        )
    return raw
```

Modify `config/wsgi.py`:

```python
"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os
from dotenv import load_dotenv

from django.core.wsgi import get_wsgi_application

from .env_resolution import resolve_django_env

_env = resolve_django_env(fail_closed=True)
load_dotenv(f".env.{_env}")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", f"config.settings.{_env}")

application = get_wsgi_application()
```

Modify `config/asgi.py` identically (same diff shape, `get_asgi_application`).

Modify `manage.py`:

```python
import os
from dotenv import load_dotenv

from config.env_resolution import resolve_django_env

# Load the environment-specific .env file.
# Change DJANGO_ENV at the OS/server level to switch environments.
# Supported values: local | staging | uat | production
_env = resolve_django_env(fail_closed=False)
load_dotenv(f".env.{_env}")

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    f"config.settings.{_env}"
)

from django.core.management import execute_from_command_line

execute_from_command_line()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test config.tests -v 2`
Expected: PASS (5 tests)

- [ ] **Step 5: Manual verification of the fail-closed behavior (not automatable via the Django test runner, since it's a process-boot check)**

Run: `DJANGO_ENV= python -c "import config.wsgi"` (or on Windows PowerShell: `$env:DJANGO_ENV=''; python -c "import config.wsgi"`)
Expected: raises `config.env_resolution.DjangoEnvError` with the "must be set explicitly" message, process exits non-zero.

Run: `python -c "import config.wsgi"` with `DJANGO_ENV` fully unset in the shell (not just empty)
Expected: same `DjangoEnvError`.

Confirm unchanged dev convenience: `python manage.py check` with no `DJANGO_ENV` set
Expected: runs normally against `config.settings.local` (unchanged from today).

- [ ] **Step 6: Commit**

```bash
git add config/env_resolution.py config/tests.py config/wsgi.py config/asgi.py manage.py
git commit -m "fix(security): fail closed on missing DJANGO_ENV instead of silently loading local settings (SEC-005)"
```

---

## Task 2: Fix broken `staging.py` settings module

**Context:** Discovered during re-verification for this plan (not simply "missing headers" as the original audit pass characterized it) — `config/settings/staging.py` is a completely empty file. It does not `from .base import *`, so `DJANGO_ENV=staging` currently cannot boot the application at all (missing `SECRET_KEY`, `DATABASES`, `INSTALLED_APPS`, etc.). This must be fixed for the fail-closed strategy in Task 1 to actually be exercisable in a staging deploy, and is a genuine functional bug independent of SEC-005.

**Files:**
- Modify: `config/settings/staging.py` (currently 0 bytes)

**Interfaces:**
- Consumes: `config/settings/base.py` (all base settings, via `from .base import *`)
- Produces: nothing consumed by other tasks — this is a leaf settings module.

- [ ] **Step 1: Write the failing test**

Add to `config/tests.py` (same file created in Task 1):

```python
class StagingSettingsModuleTests(TestCase):
    def test_staging_settings_imports_base_and_disables_debug(self):
        import importlib
        staging = importlib.import_module("config.settings.staging")
        importlib.reload(staging)  # ensure a fresh import, not a cached empty module
        self.assertFalse(staging.DEBUG)
        self.assertTrue(hasattr(staging, "SECRET_KEY"))
        self.assertTrue(hasattr(staging, "DATABASES"))
        self.assertTrue(hasattr(staging, "INSTALLED_APPS"))
        self.assertIn("apps.orders", staging.INSTALLED_APPS)
        self.assertTrue(hasattr(staging, "ALLOWED_HOSTS"))
        self.assertTrue(hasattr(staging, "CORS_ALLOWED_ORIGINS"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test config.tests.StagingSettingsModuleTests -v 2`
Expected: FAIL — `AttributeError: module 'config.settings.staging' has no attribute 'DEBUG'` (proves the file is currently empty)

- [ ] **Step 3: Write minimal implementation**

Replace the entire contents of `config/settings/staging.py`:

```python
import os
from datetime import timedelta
from .base import *  # noqa: F401,F403

DEBUG = False

# No hardcoded fallback domain — staging must be configured explicitly,
# same fail-closed principle as production/uat.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

# ── Security headers ─────────────────────────────────────────────────────
# Shorter HSTS window than production (staging domains/certs churn more)
# and no preload — preload is a one-way public commitment inappropriate
# for a non-production host.
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 3600
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test config.tests.StagingSettingsModuleTests -v 2`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/settings/staging.py config/tests.py
git commit -m "fix: populate empty staging settings module (was 0 bytes, app could not boot with DJANGO_ENV=staging)"
```

---

## Task 3: Lock the Cart row during checkout to prevent duplicate orders

**Files:**
- Modify: `apps/orders/views.py:186-276` (`CheckoutView.post`, add `EmptyCartError`)
- Test: `apps/orders/tests.py` (append new test class)

**Interfaces:**
- Consumes: `apps.cart.models.Cart` (unchanged), `apps.orders.services.allocate_discount` / `build_order_item_financials` (unchanged), `apps.inventory.services.reservation.reserve_for_order_item` / `InsufficientStockError` (unchanged, not modified by this task).
- Produces: `EmptyCartError` (new, module-level in `apps/orders/views.py`) — not consumed elsewhere, purely internal control flow for this view.

- [ ] **Step 1: Write the failing test**

Append to `apps/orders/tests.py` (the file already imports `threading`, `connection`, `TransactionTestCase`, `_make_user`, `_make_variant`, `_auth_header`, `_SHIPPING`, `CHECKOUT_URL` — reuse all of them):

```python
# ── Checkout concurrency (SEC-003) ───────────────────────────────────────────

class CheckoutConcurrencyTests(TransactionTestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.user = _make_user()
        self.variant = _make_variant()
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, variant=self.variant, quantity=1)

    def test_concurrent_checkout_on_same_cart_creates_only_one_order(self):
        results = {}
        barrier = threading.Barrier(2)

        def _run(key):
            barrier.wait()
            client = APIClient()
            client.credentials(**_auth_header(self.user))
            try:
                resp = client.post(
                    CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json"
                )
                results[key] = resp.status_code
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("a",))
        t2 = threading.Thread(target=_run, args=("b",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), [201, 400])
        self.assertEqual(Order.objects.filter(user=self.user).count(), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test apps.orders.tests.CheckoutConcurrencyTests -v 2`
Expected: FAIL (or flaky-pass) — without the lock, both threads can read `cart_items` before either commits, so `Order.objects.filter(user=self.user).count()` is `2`, not `1`, and both responses may be `201`. (This race is timing-dependent; if it happens not to fail on a given run, re-run — the fix in Step 3 makes it deterministic regardless.)

- [ ] **Step 3: Write minimal implementation**

In `apps/orders/views.py`, add the exception class near the top (after existing imports, before `_generate_order_number`):

```python
class EmptyCartError(Exception):
    """Raised when a locked cart has no items — either it started empty,
    or a concurrent request already checked it out and cleared it."""
```

Replace `CheckoutView.post` (currently lines 186-213):

```python
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if request.user.is_authenticated:
            cart, user, guest_email = self._resolve_authenticated(request)
        else:
            result = self._resolve_guest(request, serializer)
            if isinstance(result, Response):
                return result
            cart, user, guest_email = result

        if cart is None:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
                cart_items = list(locked_cart.items.select_related("variant").all())
                if not cart_items:
                    raise EmptyCartError()
                order = self._create_order_with_reservations(
                    locked_cart, cart_items, user, guest_email, serializer
                )
        except InsufficientStockError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EmptyCartError:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)
```

`_create_order_with_reservations` (lines 215-276) is **not modified** — it already accepts `cart` and `cart_items` as parameters.

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test apps.orders.tests.CheckoutConcurrencyTests -v 2`
Expected: PASS, deterministically (run it 3-5 times to confirm it isn't a lucky pass)

Run the full existing regression suite for this file to confirm nothing else broke:
Run: `python manage.py test apps.orders.tests -v 2`
Expected: PASS (all existing tests, including `AuthenticatedCheckoutTests`, `GuestCheckoutTests`, `test_checkout_empty_cart_returns_400`)

- [ ] **Step 5: Commit**

```bash
git add apps/orders/views.py apps/orders/tests.py
git commit -m "fix(security): lock Cart row during checkout to prevent duplicate orders from concurrent requests (SEC-003)"
```

---

## Task 4: Add expiry to StockReservation + on-demand release command

**Files:**
- Modify: `apps/inventory/models.py` (StockReservation, add `expires_at` field + `DEFAULT_TTL_MINUTES` constant)
- Create: `apps/inventory/migrations/0005_stockreservation_expires_at.py`
- Modify: `apps/inventory/services/reservation.py:79-85, 110-116` (set `expires_at` at creation)
- Create: `apps/inventory/management/commands/release_expired_reservations.py`
- Create: `apps/inventory/management/__init__.py`, `apps/inventory/management/commands/__init__.py` (if not already present — check first)
- Test: `apps/inventory/tests.py` (append)

**Interfaces:**
- Consumes: `apps.inventory.services.reservation.release_reservation(reservation)` (existing, unmodified — already `@transaction.atomic`, already locks the reservation row and its allocations, already guards against releasing a non-held/confirmed reservation).
- Produces: `StockReservation.expires_at` (nullable `DateTimeField`), `StockReservation.DEFAULT_TTL_MINUTES` (class constant, default `30`) — consumed by the new management command only.

**Note on scope (matches the audit's own recommendation and this plan's "avoid speculative infrastructure" constraint):** this task adds the *data* (expiry timestamp) and an *on-demand tool* (management command) that ops can wire to any scheduler they already have (cron, a hosting platform's scheduled-task feature, or manually) — it does not add Celery or any in-process scheduler, and it does not change `reserve_for_order_item`'s locking behavior at all. Whether/how to automate running the command on a schedule is a deployment decision, not a code decision, and is explicitly left to the user per the audit's own note ("flag as a product decision rather than silently implement").

- [ ] **Step 1: Write the failing test**

Append to `apps/inventory/tests.py`:

```python
class StockReservationExpiryTests(TestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.variant = _make_variant("SKU-EXPIRY-TEST")
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )

    def test_reservation_gets_expires_at_on_creation(self):
        order_item = _make_order_item(self.variant, quantity=1)
        before = timezone.now()
        reservation = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        after = timezone.now()
        self.assertIsNotNone(reservation.expires_at)
        expected_min = before + timedelta(minutes=StockReservation.DEFAULT_TTL_MINUTES)
        expected_max = after + timedelta(minutes=StockReservation.DEFAULT_TTL_MINUTES)
        self.assertGreaterEqual(reservation.expires_at, expected_min)
        self.assertLessEqual(reservation.expires_at, expected_max)


class ReleaseExpiredReservationsCommandTests(TestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.variant = _make_variant("SKU-EXPIRY-CMD-TEST")
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )

    def test_command_releases_only_expired_held_reservations(self):
        from django.core.management import call_command

        expired_item = _make_order_item(self.variant, quantity=1)
        expired = reservation_service.reserve_for_order_item(expired_item, self.warehouse)
        expired.expires_at = timezone.now() - timedelta(minutes=1)
        expired.save(update_fields=["expires_at"])

        fresh_item = _make_order_item(self.variant, quantity=1)
        fresh = reservation_service.reserve_for_order_item(fresh_item, self.warehouse)

        call_command("release_expired_reservations")

        expired.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(expired.status, StockReservation.STATUS_RELEASED)
        self.assertEqual(fresh.status, StockReservation.STATUS_HELD)

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("1"))  # only fresh's unit still held
```

Confirm `_make_order_item` helper already exists in `apps/inventory/tests.py` (used by the existing concurrency tests at line 627) — reuse it, do not redefine.

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test apps.inventory.tests.StockReservationExpiryTests apps.inventory.tests.ReleaseExpiredReservationsCommandTests -v 2`
Expected: FAIL — `AttributeError: 'StockReservation' object has no attribute 'expires_at'`

- [ ] **Step 3: Write minimal implementation**

In `apps/inventory/models.py`, modify `StockReservation` (currently lines 355-410): add the constant and field.

```python
class StockReservation(BaseModel):
    """A hold placed against inventory at checkout, for one OrderItem.
    ...
    """

    DEFAULT_TTL_MINUTES = 30

    PURPOSE_DIRECT_SALE = "direct_sale"
    # ... (rest of the existing class body unchanged up to the fields) ...

    resolved_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this held reservation becomes eligible for release by "
            "release_expired_reservations. Set at creation; never extended. "
            "Null for reservations created before this field existed."
        ),
    )
```

Generate the migration:
Run: `python manage.py makemigrations inventory`
Expected: creates `apps/inventory/migrations/0005_stockreservation_expires_at.py` with a single `AddField` operation (nullable, no default needed since `null=True`).

In `apps/inventory/services/reservation.py`, add the import and set the field at both creation sites:

```python
from datetime import timedelta  # add to existing imports at the top
```

Line 79-85 (`_reserve_direct`), add `expires_at`:

```python
    reservation = inv_models.StockReservation.objects.create(
        order_item=order_item,
        variant=variant,
        warehouse=warehouse,
        purpose=inv_models.StockReservation.PURPOSE_DIRECT_SALE,
        quantity=needed,
        expires_at=timezone.now() + timedelta(minutes=inv_models.StockReservation.DEFAULT_TTL_MINUTES),
    )
```

Line 110-116 (`_reserve_decant`), same addition:

```python
    reservation = inv_models.StockReservation.objects.create(
        order_item=order_item,
        variant=source_variant,
        warehouse=warehouse,
        purpose=inv_models.StockReservation.PURPOSE_DECANT_FULFILLMENT,
        quantity=needed_ml,
        expires_at=timezone.now() + timedelta(minutes=inv_models.StockReservation.DEFAULT_TTL_MINUTES),
    )
```

Check whether `apps/inventory/management/` exists first (`ls apps/inventory/management` or Glob) — if not, create both `__init__.py` files (empty) alongside the command.

Create `apps/inventory/management/commands/release_expired_reservations.py`:

```python
"""
Releases StockReservation rows that are still `held` past their
expires_at timestamp — e.g. abandoned/never-verified COD orders that
never progressed past pending. Intended to be run periodically by
whatever scheduler the deployment already has (cron, hosting-platform
scheduled task, etc.) — this repo has no in-process task runner, and
this command does not add one.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import StockReservation
from apps.inventory.services.reservation import release_reservation


class Command(BaseCommand):
    help = "Release held StockReservation rows past their expires_at timestamp."

    def handle(self, *args, **options):
        now = timezone.now()
        expired_ids = list(
            StockReservation.objects.filter(
                status=StockReservation.STATUS_HELD,
                expires_at__isnull=False,
                expires_at__lt=now,
            ).values_list("pk", flat=True)
        )

        released = 0
        for reservation_id in expired_ids:
            with transaction.atomic():
                reservation = StockReservation.objects.get(pk=reservation_id)
                # release_reservation re-checks status under its own lock,
                # so a reservation that was confirmed/consumed between the
                # query above and this call is safely skipped, not double-released.
                if reservation.status != StockReservation.STATUS_HELD:
                    continue
                release_reservation(reservation)
                released += 1

        self.stdout.write(
            self.style.SUCCESS(f"Released {released} expired reservation(s).")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test apps.inventory.tests.StockReservationExpiryTests apps.inventory.tests.ReleaseExpiredReservationsCommandTests -v 2`
Expected: PASS

Run the full inventory suite to confirm the migration and field addition don't break existing tests:
Run: `python manage.py test apps.inventory.tests -v 2`
Expected: PASS (all existing tests, including `ReservationConcurrencyTests`, `ReservationConcurrencyDecantVsRetailTests`)

- [ ] **Step 5: Commit**

```bash
git add apps/inventory/models.py apps/inventory/migrations/0005_stockreservation_expires_at.py apps/inventory/services/reservation.py apps/inventory/management apps/inventory/tests.py
git commit -m "feat(inventory): add StockReservation expiry + on-demand release command (SEC-004)"
```

---

## Task 5: Lock the OTPVerification row during verify to close the attempt-counter race

**Files:**
- Modify: `apps/accounts/services/otp.py:88-131` (`verify_otp_record`)
- Test: `apps/accounts/tests.py` (append)

**Interfaces:**
- Consumes: `apps.accounts.models.OTPVerification` (unchanged model).
- Produces: nothing new — `verify_otp_record`'s signature and return value are unchanged, only its internal locking changes.

- [ ] **Step 1: Write the failing test**

Append to `apps/accounts/tests.py` (file already imports `threading`? — check; if not, add `import threading` and `from django.db import connection` and `from django.test import TransactionTestCase` to the existing import block at the top):

```python
class OTPVerifyRaceConditionTests(TransactionTestCase):
    def setUp(self):
        self.user = _make_user()

    def test_concurrent_wrong_guesses_cannot_exceed_max_attempts(self):
        record, _ = create_otp_record(self.user)
        # otp_hash is for a real code the attacker doesn't know — every
        # guess below is deliberately wrong.
        results = []
        barrier = threading.Barrier(5)

        def _guess():
            barrier.wait()
            try:
                verify_otp_record(str(record.otp_session_token), "000000")
            except ValueError as exc:
                results.append(str(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=_guess) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        record.refresh_from_db()
        self.assertEqual(record.attempt_count, record.MAX_ATTEMPTS)
        self.assertTrue(record.is_locked)
```

(This asserts `attempt_count` never exceeds `MAX_ATTEMPTS` even under 5 simultaneous wrong guesses — without the lock, more than `MAX_ATTEMPTS` increments can land because each thread reads the pre-increment count before any of the others have saved.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test apps.accounts.tests.OTPVerifyRaceConditionTests -v 2`
Expected: FAIL (or flaky-pass) — `record.attempt_count` can exceed `MAX_ATTEMPTS` (e.g. `5` instead of `3`) because all 5 threads read `attempt_count=0` before any of them save. Timing-dependent; the fix in Step 3 makes the invariant hold deterministically.

- [ ] **Step 3: Write minimal implementation**

In `apps/accounts/services/otp.py`, add `transaction` to the imports:

```python
from django.db import transaction
```

Replace `verify_otp_record` (currently lines 88-131):

```python
def verify_otp_record(otp_session_token: str, otp_submitted: str) -> "OTPVerification":
    """Verify a submitted OTP against the stored hash.

    Args:
        otp_session_token: UUID token returned at OTP request time.
        otp_submitted: 6-digit string entered by the user.

    Returns:
        The verified OTPVerification instance (is_verified is set to True).

    Raises:
        ValueError: With a user-safe message on any failure condition.
    """
    with transaction.atomic():
        try:
            record = OTPVerification.objects.select_for_update().select_related("user").get(
                otp_session_token=otp_session_token,
                is_active=True,
            )
        except OTPVerification.DoesNotExist:
            raise ValueError("Invalid or expired session. Please request a new OTP.")

        if record.is_verified:
            raise ValueError("This OTP has already been used. Please request a new one.")

        if record.is_expired:
            raise ValueError("OTP has expired. Please request a new one.")

        if record.is_locked:
            raise ValueError(
                "Too many incorrect attempts. Please request a new OTP."
            )

        if not check_password(otp_submitted, record.otp_hash):
            record.attempt_count += 1
            record.save(update_fields=["attempt_count", "updated_at"])
            attempts_left = OTPVerification.MAX_ATTEMPTS - record.attempt_count
            if attempts_left <= 0:
                raise ValueError("Too many incorrect attempts. Please request a new OTP.")
            raise ValueError(f"Incorrect OTP. {attempts_left} attempt(s) remaining.")

        record.is_verified = True
        record.save(update_fields=["is_verified", "updated_at"])
        return record
```

(`select_for_update()` added to the existing query; the rest of the function body is unchanged, now indented one level inside `with transaction.atomic():`. This serializes concurrent verify attempts on the *same* OTP session — each thread now blocks until the previous one's transaction commits, then re-reads the up-to-date `attempt_count` before deciding whether to raise `is_locked`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test apps.accounts.tests.OTPVerifyRaceConditionTests -v 2`
Expected: PASS, deterministically (run 3-5 times to confirm)

Run the full existing OTP/accounts suite:
Run: `python manage.py test apps.accounts.tests -v 2`
Expected: PASS (all existing tests, including `OTPServiceTests`, `VerifyOTPViewTests`)

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/services/otp.py apps/accounts/tests.py
git commit -m "fix(security): lock OTPVerification row during verify to close attempt-counter race (SEC-002)"
```

---

## Task 6: Add scoped throttle to ResendOTPView

**Files:**
- Modify: `apps/accounts/views.py:185-186` (`ResendOTPView`)
- Modify: `config/settings/base.py:75-80` (`DEFAULT_THROTTLE_RATES`)
- Modify: `config/settings/local.py:19-27` (`DEFAULT_THROTTLE_RATES` override — this dict fully replaces base's, it does not merge, so `otp_resend` must be added here too or `ResendOTPView` will raise `ImproperlyConfigured` in local dev)
- Test: `apps/accounts/tests.py` (append)

**Interfaces:** None — purely a throttle-scope addition, no new function signatures.

- [ ] **Step 1: Write the failing test**

Append to `apps/accounts/tests.py` (reusing existing `ResendOTPViewTests` conventions in that file — check its imports/helpers first, e.g. how it creates an OTP session, and mirror that setup):

```python
class ResendOTPThrottleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.record, _ = create_otp_record(self.user)

    def test_resend_is_throttled_after_configured_rate(self):
        # DEFAULT_THROTTLE_RATES["otp_resend"] will be "3/minute" (Step 3) —
        # 3 requests succeed at the DRF-throttle layer (the 3rd is separately
        # rejected by the app-level MAX_RESENDS=3 business rule, which is
        # expected and unrelated to this test), the 4th must be blocked by
        # the throttle before it ever reaches the view logic.
        last_status = None
        for _ in range(4):
            resp = self.client.post(
                "/api/accounts/otp/resend/",
                {"otp_session_token": str(self.record.otp_session_token)},
                format="json",
            )
            last_status = resp.status_code
        self.assertEqual(last_status, 429)
```

(Adjust the URL path if `apps/accounts/tests.py`'s existing `ResendOTPViewTests` class uses a different literal or a `reverse()` call — check that class first and match its convention exactly rather than guessing the path.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test apps.accounts.tests.ResendOTPThrottleTests -v 2`
Expected: FAIL — no `429` is ever returned (falls back to the global `anon` throttle at `100/day`, which 4 requests in one test run never trips).

- [ ] **Step 3: Write minimal implementation**

In `config/settings/base.py`, add `"otp_resend": "3/minute"` to `DEFAULT_THROTTLE_RATES` (currently lines 75-80):

```python
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/day",
        "user": "1000/day",
        "otp_request": "5/hour",
        "otp_verify": "10/minute",
        "otp_resend": "3/minute",
    },
```

In `config/settings/local.py`, add the same key to its fully-overriding dict (currently lines 19-27):

```python
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        "anon": "1000/day",
        "user": "10000/day",
        "otp_request": "100/hour",
        "otp_verify": "1000/minute",
        "otp_resend": "100/minute",
    },
}
```

In `apps/accounts/views.py`, modify `ResendOTPView` (currently lines 185-186):

```python
class ResendOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_resend"
```

(`ScopedRateThrottle` is already imported at the top of this file, line 18 — used by the two sibling views.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test apps.accounts.tests.ResendOTPThrottleTests -v 2`
Expected: PASS

Run the full existing accounts suite:
Run: `python manage.py test apps.accounts.tests -v 2`
Expected: PASS (all existing tests, including `ResendOTPViewTests`)

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/views.py config/settings/base.py config/settings/local.py apps/accounts/tests.py
git commit -m "fix(security): add scoped throttle to ResendOTPView (SEC-001)"
```

---

## Execution Order

Tasks 1–2 (SEC-005) and Task 3 (SEC-003) are independent of each other and of Tasks 4–6, and can be done in any order or in parallel. Task 4 (SEC-004) is independent of everything else. Tasks 5 and 6 both touch `apps/accounts` but different files (`services/otp.py` vs `views.py`+settings) and are independent of each other. No task depends on another task's code changes — they only share this plan document and, in Tasks 1-2 and 6, the same settings files (different keys/sections, no overlap).
