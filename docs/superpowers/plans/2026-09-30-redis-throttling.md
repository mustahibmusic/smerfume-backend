# Redis Shared Throttling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shared (Redis) DRF throttle counters with an explicit failure policy, a correct client IP behind proxies, and a per-mobile OTP send limit in PostgreSQL.

**Architecture:** Django's built-in `RedisCache` becomes the default cache in staging/UAT/production and is used only by DRF throttles. Three small throttle subclasses in `apps/core/throttling.py` define the Redis failure policy (OTP fails closed with 503, everything else fails open). The per-mobile limit is computed from existing `OTPVerification` columns under a user-row lock inside `apps/accounts/services/otp.py`.

**Tech Stack:** Django 6.0.2, DRF, PostgreSQL, `redis` (Python client, already installed in `venv`).

**Spec:** `docs/superpowers/specs/2026-09-30-redis-throttling-design.md` (APPROVED)

## Global Constraints

- Branch: `feat/redis-throttling`. Never commit on `dev`, `uat` or `main`. No merge or `dev` push without explicit instruction.
- Never commit `.claude/CURRENT_STATE.md`, `.claude/DECISIONS.md`, `.claude/CLAUDE.md` or any `.env.*` file. Stage files by exact path only.
- No migrations. No new models. No `django-redis`. Only new dependency: `redis`.
- Staging/UAT/production: `REDIS_URL` and `NUM_PROXIES` required; missing raises `ImproperlyConfigured`. Never fall back to `LocMemCache` there.
- Do not guess `NUM_PROXIES`; it comes from the environment and must match the deployment's proxy topology.
- Initial default rates (configurable, in settings): `anon 120/min`, `user 300/min`, `otp_request 20/hour` (request + resend, per IP), `otp_verify 10/minute`, `OTP_MAX_SENDS_PER_MOBILE_PER_HOUR = 5`. One rate per scope; no burst+sustained tiers.
- Only `redis.exceptions.RedisError` is caught by throttles. Log at `ERROR` with the scope. Never return Redis error detail to clients.
- Do not modify catalogue search.
- Python runs as `venv/Scripts/python.exe`. Tests run with the default `local` settings.
- DRF gotcha: `SimpleRateThrottle.THROTTLE_RATES` is bound at import time, so `override_settings` does not change rates. Tests patch `THROTTLE_RATES` on the throttle class instead. `NUM_PROXIES` is read at call time, so `override_settings` works for it.

## Review Focus

1. Two concurrent OTP sends for one mobile at the limit: exactly one passes (Task 3, concurrency tests).
2. Redis down on resend (not only request): 503, row unchanged, no SMS (Task 2).
3. Behind a proxy, two customers with different `X-Forwarded-For` get separate buckets (Task 2).
4. Resend of a row created more than an hour ago still counts toward the limit (Task 3).
5. Rate-limit 429 message does not differ for existing and new numbers (Task 3: generic message asserted).

---

### Task 1: Settings — Redis cache, `NUM_PROXIES`, rates, dependency

**Files:**
- Modify: `config/settings/base.py` (imports; `REST_FRAMEWORK` at line 65; add helpers above it)
- Modify: `config/settings/local.py`, `config/settings/staging.py`, `config/settings/uat.py`, `config/settings/production.py`
- Modify: `requirements.txt` (UTF-16)
- Test: `apps/core/tests.py`

**Interfaces:**
- Produces: `config.settings.base.require_env(name: str) -> str`, `config.settings.base.redis_cache(url: str) -> dict`, `settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR: int`, `REST_FRAMEWORK["NUM_PROXIES"]: int`.

- [ ] **Step 1: Write the failing tests** — replace `apps/core/tests.py` with:

```python
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.settings.base import redis_cache, require_env


class SettingsHelperTests(SimpleTestCase):

    def test_require_env_returns_value(self):
        with patch.dict("os.environ", {"SMF_TEST_VAR": "redis://x:6379/0"}):
            self.assertEqual(require_env("SMF_TEST_VAR"), "redis://x:6379/0")

    def test_require_env_missing_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                require_env("SMF_TEST_VAR")

    def test_require_env_blank_raises(self):
        with patch.dict("os.environ", {"SMF_TEST_VAR": "  "}):
            with self.assertRaises(ImproperlyConfigured):
                require_env("SMF_TEST_VAR")

    def test_redis_cache_structure(self):
        default = redis_cache("redis://cache:6379/1")["default"]
        self.assertEqual(default["BACKEND"], "django.core.cache.backends.redis.RedisCache")
        self.assertEqual(default["LOCATION"], "redis://cache:6379/1")
        self.assertEqual(default["KEY_PREFIX"], "smerfume")
        self.assertEqual(default["OPTIONS"], {"socket_connect_timeout": 0.5, "socket_timeout": 0.5})

    def test_default_rate_settings(self):
        self.assertEqual(settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR, 5)
        self.assertEqual(settings.REST_FRAMEWORK["NUM_PROXIES"], 0)


class DeployedSettingsFailFastTests(SimpleTestCase):
    """Import each deployed settings module in isolation (never activated;
    django.conf.settings is untouched) with required variables removed."""

    DEPLOYED = ("staging", "uat", "production")
    VALID = {"REDIS_URL": "redis://cache:6379/0", "NUM_PROXIES": "1"}

    def _import(self, name, env):
        module_name = f"config.settings.{name}"
        clean = {k: v for k, v in os.environ.items() if k not in self.VALID}
        with patch.dict("os.environ", {**clean, **env}, clear=True):
            sys.modules.pop(module_name, None)
            try:
                return importlib.import_module(module_name)
            finally:
                sys.modules.pop(module_name, None)

    def test_missing_redis_url_refuses_to_start(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "REDIS_URL"):
                self._import(name, {"NUM_PROXIES": "1"})

    def test_missing_num_proxies_refuses_to_start(self):
        for name in self.DEPLOYED:
            with self.subTest(name), self.assertRaisesMessage(ImproperlyConfigured, "NUM_PROXIES"):
                self._import(name, {"REDIS_URL": "redis://cache:6379/0"})

    def test_valid_config_uses_redis_and_proxy_count(self):
        for name in self.DEPLOYED:
            with self.subTest(name):
                module = self._import(name, self.VALID)
                self.assertEqual(
                    module.CACHES["default"]["BACKEND"],
                    "django.core.cache.backends.redis.RedisCache",
                )
                self.assertEqual(module.REST_FRAMEWORK["NUM_PROXIES"], 1)
```

Add `import importlib`, `import os`, `import sys` at the top of `apps/core/tests.py`.

- [ ] **Step 2: Run to verify failure**

Run: `venv/Scripts/python.exe manage.py test apps.core.tests -v 2`
Expected: ERROR — `ImportError: cannot import name 'redis_cache'`.

- [ ] **Step 3: Implement `config/settings/base.py`**

Add to the imports: `from django.core.exceptions import ImproperlyConfigured`.

Add directly above `REST_FRAMEWORK = {`:

```python
def require_env(name):
    """Return a required environment variable, or refuse to start."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set in this environment.")
    return value


def redis_cache(url):
    """Default cache on Redis. Used only for DRF throttle counters.
    Short socket timeouts stop a dead Redis from blocking requests."""
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": url,
            "KEY_PREFIX": "smerfume",
            "OPTIONS": {
                "socket_connect_timeout": 0.5,
                "socket_timeout": 0.5,
            },
        }
    }
```

In `REST_FRAMEWORK`, replace `DEFAULT_THROTTLE_RATES` and add `NUM_PROXIES` (leave `DEFAULT_THROTTLE_CLASSES` unchanged; Task 2 switches it):

```python
    # Initial defaults, tune from production traffic. See the Redis
    # throttling spec §6 for what each limit protects.
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "user": "300/min",
        "otp_request": "20/hour",
        "otp_verify": "10/minute",
    },
    # Must match the deployment's real proxy count; required outside local.
    "NUM_PROXIES": int(os.getenv("NUM_PROXIES", "0")),
```

Below `REST_FRAMEWORK`, add:

```python
# Per-mobile OTP send limit (request + resend), enforced in PostgreSQL.
OTP_MAX_SENDS_PER_MOBILE_PER_HOUR = int(os.getenv("OTP_MAX_SENDS_PER_MOBILE_PER_HOUR", "5"))
```

- [ ] **Step 4: Environment files**

`config/settings/local.py` — replace the relaxed-rates block with:

```python
# Relax throttle in local dev
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        "anon": "1000/min",
        "user": "3000/min",
        "otp_request": "100/hour",
        "otp_verify": "1000/minute",
    },
}

# Redis is optional locally; without REDIS_URL Django uses LocMemCache.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
if REDIS_URL:
    CACHES = redis_cache(REDIS_URL)
```

`config/settings/staging.py`, `config/settings/uat.py`, `config/settings/production.py` — append to each:

```python
# Shared throttle counters. Required: no LocMemCache fallback here.
CACHES = redis_cache(require_env("REDIS_URL"))
# Must match this environment's proxy topology (0 is a valid explicit value).
REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": int(require_env("NUM_PROXIES"))}
```

- [ ] **Step 5: Pin `redis` in `requirements.txt`** (UTF-16; preserve encoding and line endings)

```bash
venv/Scripts/python.exe - <<'EOF'
from importlib.metadata import version
p = "requirements.txt"
with open(p, encoding="utf-16", newline="") as f:
    text = f.read()
nl = "\r\n" if "\r\n" in text else "\n"
if "\nredis==" not in nl + text:
    text = text.rstrip("\r\n") + nl + f"redis=={version('redis')}" + nl
    with open(p, "w", encoding="utf-16", newline="") as f:
        f.write(text)
print(version("redis"))
EOF
git diff --stat requirements.txt
```

Expected: one line added (diff may show as binary because of UTF-16).

- [ ] **Step 6: Run tests**

Run: `venv/Scripts/python.exe manage.py test apps.core.tests -v 2`
Expected: 8 tests OK.

Deployment-style check (automated coverage is `DeployedSettingsFailFastTests`; this mirrors it through `django.setup()`):

```bash
DJANGO_ENV=uat REDIS_URL= NUM_PROXIES= venv/Scripts/python.exe -c "import os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings.uat'; import django; django.setup()" 2>&1 | tail -1
```

Expected: `django.core.exceptions.ImproperlyConfigured: REDIS_URL must be set in this environment.` (If another required UAT variable fails first, set it to a dummy value in the command and rerun.)

- [ ] **Step 7: Commit**

```bash
git add config/settings/base.py config/settings/local.py config/settings/staging.py config/settings/uat.py config/settings/production.py requirements.txt apps/core/tests.py
git commit -m "feat(core): configure redis throttle cache and proxy count"
```

---

### Task 2: Throttle failure policy and OTP view wiring

**Files:**
- Create: `apps/core/throttling.py`
- Modify: `apps/accounts/views.py` (`RequestOTPView` ~line 36, `VerifyOTPView` ~line 107, `ResendOTPView` line 185)
- Test: `apps/core/tests.py`, `apps/accounts/tests.py`

**Interfaces:**
- Consumes: settings from Task 1.
- Produces: `ThrottleUnavailable` (503, `default_code="throttle_unavailable"`), `FailClosedScopedRateThrottle`, `FailOpenAnonRateThrottle`, `FailOpenUserRateThrottle` in `apps.core.throttling`.

- [ ] **Step 1: Write failing tests**

Append to `apps/core/tests.py`:

```python
from unittest.mock import MagicMock

from django.core.cache import cache
from django.test import TestCase
from redis.exceptions import RedisError
from rest_framework.test import APIClient


def _broken_cache():
    broken = MagicMock()
    broken.get.side_effect = RedisError("connection refused")
    broken.set.side_effect = RedisError("connection refused")
    return broken


class FailOpenThrottleTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_catalogue_stays_up_when_redis_is_down(self):
        from apps.core.throttling import FailOpenAnonRateThrottle

        with patch.object(FailOpenAnonRateThrottle, "cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = APIClient().get("/api/catalog/products/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("scope=anon", logs.output[0])
        self.assertNotIn("connection refused", resp.content.decode())
```

Append to `apps/accounts/tests.py` (add `from django.conf import settings`, `from django.core.cache import cache`, `from django.test import override_settings`, `from redis.exceptions import RedisError` to imports):

```python
def _broken_cache():
    broken = MagicMock()
    broken.get.side_effect = RedisError("connection refused")
    broken.set.side_effect = RedisError("connection refused")
    return broken


_OTP_THROTTLE = "apps.core.throttling.FailClosedScopedRateThrottle"


class OTPThrottleFailClosedTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def _assert_503(self, resp, logs, scope):
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["detail"], "Login is temporarily unavailable. Please try again shortly.")
        self.assertIn(f"scope={scope}", logs.output[0])
        self.assertNotIn("connection refused", resp.content.decode())

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_request_fails_closed(self, sms):
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(reverse("otp_request"), {"mobile_number": "9000000101"})
        self._assert_503(resp, logs, "otp_request")
        self.assertEqual(OTPVerification.objects.count(), 0)
        sms.assert_not_called()

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_fails_closed(self, sms):
        user = _make_user(mobile="+919000000102")
        record, _ = create_otp_record(user)
        sms.reset_mock()
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(
                    reverse("otp_resend"), {"otp_session_token": str(record.otp_session_token)}
                )
        self._assert_503(resp, logs, "otp_request")
        record.refresh_from_db()
        self.assertEqual(record.resend_count, 0)
        sms.assert_not_called()

    def test_verify_fails_closed(self):
        with patch(f"{_OTP_THROTTLE}.cache", _broken_cache()):
            with self.assertLogs("apps.core.throttling", "ERROR") as logs:
                resp = self.client.post(
                    reverse("otp_verify"),
                    {"otp_session_token": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "otp": "123456"},
                )
        self._assert_503(resp, logs, "otp_verify")


class OTPThrottleSharingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "otp_request": "1/hour"}
        patcher = patch(f"{_OTP_THROTTLE}.THROTTLE_RATES", rates)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resend_shares_request_counter(self, _sms):
        resp = self.client.post(reverse("otp_request"), {"mobile_number": "9000000103"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            reverse("otp_resend"), {"otp_session_token": resp.data["otp_session_token"]}
        )
        self.assertEqual(resp.status_code, 429)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_num_proxies_separates_clients(self, _sms):
        url = reverse("otp_request")
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}):
            first = self.client.post(url, {"mobile_number": "9000000104"}, HTTP_X_FORWARDED_FOR="1.1.1.1")
            other = self.client.post(url, {"mobile_number": "9000000105"}, HTTP_X_FORWARDED_FOR="2.2.2.2")
            again = self.client.post(url, {"mobile_number": "9000000106"}, HTTP_X_FORWARDED_FOR="1.1.1.1")
        self.assertEqual((first.status_code, other.status_code, again.status_code), (200, 200, 429))
```

SMS mock note: `patch(_SMS_BACKEND_PATCH, ...)` replaces `_get_sms_backend`; the service calls it once per SMS send, so `sms.call_count` equals the number of sends.

- [ ] **Step 2: Run to verify failure**

Run: `venv/Scripts/python.exe manage.py test apps.core.tests apps.accounts.tests -v 2`
Expected: ERROR — `ModuleNotFoundError: No module named 'apps.core.throttling'`.

- [ ] **Step 3: Create `apps/core/throttling.py`**

```python
"""DRF throttles with an explicit policy for when the cache (Redis) fails.

OTP endpoints fail closed: a missing rate limit would weaken OTP abuse
protection. General anon/user throttles fail open so the storefront stays
up. Only RedisError is handled; the client never sees Redis details.
"""
import logging

from redis.exceptions import RedisError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.throttling import (
    AnonRateThrottle,
    ScopedRateThrottle,
    UserRateThrottle,
)

logger = logging.getLogger(__name__)


class ThrottleUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Login is temporarily unavailable. Please try again shortly."
    default_code = "throttle_unavailable"


def _log_cache_failure(scope):
    logger.error("Throttle cache unavailable (scope=%s).", scope, exc_info=True)


class FailClosedScopedRateThrottle(ScopedRateThrottle):
    """Scoped throttle for OTP endpoints: refuses the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(getattr(view, "throttle_scope", None))
            raise ThrottleUnavailable()


class FailOpenAnonRateThrottle(AnonRateThrottle):
    """Global anonymous burst limit: allows the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(self.scope)
            return True


class FailOpenUserRateThrottle(UserRateThrottle):
    """Global authenticated burst limit: allows the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(self.scope)
            return True
```

- [ ] **Step 4: Wire the throttles**

In `config/settings/base.py` `REST_FRAMEWORK`, set:

```python
    "DEFAULT_THROTTLE_CLASSES": (
        "apps.core.throttling.FailOpenAnonRateThrottle",
        "apps.core.throttling.FailOpenUserRateThrottle",
    ),
```

In `apps/accounts/views.py`:

Replace `from rest_framework.throttling import ScopedRateThrottle` with `from apps.core.throttling import FailClosedScopedRateThrottle`. Then:

- `RequestOTPView`: `throttle_classes = [FailClosedScopedRateThrottle]` (keep `throttle_scope = "otp_request"`).
- `VerifyOTPView`: `throttle_classes = [FailClosedScopedRateThrottle]` (keep `throttle_scope = "otp_verify"`).
- `ResendOTPView`: add under `permission_classes`:

```python
    # Every resend sends an SMS, so it shares the per-IP OTP counter.
    throttle_classes = [FailClosedScopedRateThrottle]
    throttle_scope = "otp_request"
```

Update the `429` descriptions in the request and resend `extend_schema` responses to "Rate limit exceeded." and add `503: OpenApiResponse(description="Login temporarily unavailable (rate-limit store down).")` to request, resend and verify.

- [ ] **Step 5: Run tests**

Run: `venv/Scripts/python.exe manage.py test apps.core.tests apps.accounts.tests -v 2`
Expected: all OK.

- [ ] **Step 6: Commit**

```bash
git add apps/core/throttling.py apps/core/tests.py apps/accounts/views.py apps/accounts/tests.py config/settings/base.py
git commit -m "feat(core): fail-closed OTP throttles and fail-open global throttles"
```

---

### Task 3: Per-mobile OTP send limit with user-row lock

**Files:**
- Modify: `apps/accounts/services/otp.py` (`create_otp_record` line 58, `resend_otp` line 134)
- Modify: `apps/accounts/views.py` (`RequestOTPView.post`, `ResendOTPView.post`)
- Test: `apps/accounts/tests.py`

**Interfaces:**
- Consumes: `settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR` (Task 1).
- Produces: `apps.accounts.services.otp.OTPRateLimitError(Exception)` with class attribute `message`; `_recent_send_count(user) -> int`; `_check_mobile_send_limit(user) -> None`. `create_otp_record` and `resend_otp` keep their signatures and return values.

Lock protocol (both paths, one transaction): lock `User` row → (resend only: lock `OTPVerification` row) → existing checks → `_check_mobile_send_limit` (count + decide) → create/update row → send SMS. The SMS call stays inside the transaction so the next sender for that number waits until the send is recorded.

- [ ] **Step 1: Write failing tests** — append to `apps/accounts/tests.py` (add `import threading`, `import time`, `from django.db import connection`, `from django.test import TransactionTestCase`, `from apps.accounts.services import otp as otp_service`, and `OTPRateLimitError` to the `apps.accounts.services.otp` import):

```python
_LIMIT_MESSAGE = "Too many OTP requests for this number. Please try again later."


def _sent_row(user, *, created_ago=None, resend_count=0, resent_ago=None):
    """OTPVerification row with explicit send timestamps (created_at is auto_now_add)."""
    now = timezone.now()
    row = OTPVerification.objects.create(
        user=user, otp_hash="x", expires_at=now + timedelta(minutes=5),
        resend_count=resend_count,
        last_resend_at=(now - resent_ago) if resent_ago is not None else None,
    )
    if created_ago is not None:
        OTPVerification.objects.filter(pk=row.pk).update(created_at=now - created_ago)
    return row


class MobileSendLimitTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = _make_user(mobile="+919333333333")

    def test_counts_initial_sends_and_resends(self):
        _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))
        _sent_row(self.user)
        self.assertEqual(otp_service._recent_send_count(self.user), 4)

    def test_ignores_sends_older_than_one_hour(self):
        _sent_row(self.user, created_ago=timedelta(hours=2), resend_count=3,
                  resent_ago=timedelta(minutes=90))
        self.assertEqual(otp_service._recent_send_count(self.user), 0)

    def test_recent_resend_on_old_row_counts(self):
        _sent_row(self.user, created_ago=timedelta(hours=2), resend_count=1,
                  resent_ago=timedelta(minutes=10))
        self.assertEqual(otp_service._recent_send_count(self.user), 1)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_sixth_request_is_refused(self, sms):
        for _ in range(5):
            create_otp_record(self.user)
        sms.reset_mock()
        with self.assertRaises(OTPRateLimitError):
            create_otp_record(self.user)
        self.assertEqual(OTPVerification.objects.filter(user=self.user).count(), 5)
        sms.assert_not_called()

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_resends_count_toward_limit(self, _sms):
        _sent_row(self.user)
        row = _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))
        _sent_row(self.user)  # 1 + 3 + 1 = 5 sends
        with self.assertRaises(OTPRateLimitError):
            resend_otp(str(row.otp_session_token))
        row.refresh_from_db()
        self.assertEqual(row.resend_count, 2)

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_other_number_not_affected(self, _sms):
        for _ in range(5):
            create_otp_record(self.user)
        other = _make_user(mobile="+919333333334", username="u_other")
        create_otp_record(other)  # does not raise

    @patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend)
    def test_views_return_generic_429(self, _sms):
        for _ in range(5):
            create_otp_record(self.user)
        client = APIClient()
        resp = client.post(reverse("otp_request"), {"mobile_number": "9333333333"})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["error"], _LIMIT_MESSAGE)
        token = OTPVerification.objects.filter(user=self.user).first().otp_session_token
        resp = client.post(reverse("otp_resend"), {"otp_session_token": str(token)})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["error"], _LIMIT_MESSAGE)


class MobileSendLimitConcurrencyTests(TransactionTestCase):
    """Concurrent sends for one mobile must serialize on the user-row lock
    before counting, deciding, writing OTP state and sending the SMS.
    _recent_send_count is slowed so that, without the lock, both threads
    would read the same stale count and both pass."""

    def setUp(self):
        self.user = _make_user(mobile="+919444444444")

    def _run_parallel(self, *calls):
        results = [None] * len(calls)
        barrier = threading.Barrier(len(calls))

        def _run(index, call):
            barrier.wait()
            try:
                call()
                results[index] = "ok"
            except OTPRateLimitError as exc:
                results[index] = type(exc).__name__
            except Exception as exc:  # deadlock/serialization must surface
                results[index] = f"db:{type(exc).__name__}: {exc}"
            finally:
                connection.close()

        threads = [threading.Thread(target=_run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _slow_count(self):
        real = otp_service._recent_send_count

        def slow(user):
            count = real(user)
            time.sleep(0.3)  # widen the stale-read window
            return count

        return patch("apps.accounts.services.otp._recent_send_count", side_effect=slow)

    def test_two_requests_at_limit_minus_one(self):
        _sent_row(self.user, resend_count=3, resent_ago=timedelta(minutes=1))  # 4 sends
        with patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend) as sms, self._slow_count():
            results = self._run_parallel(
                lambda: create_otp_record(self.user),
                lambda: create_otp_record(self.user),
            )
        self.assertEqual(sorted(results), ["OTPRateLimitError", "ok"], results)
        self.assertEqual(otp_service._recent_send_count(self.user), 5)
        self.assertEqual(sms.call_count, 1)

    def test_request_and_resend_at_limit_minus_one(self):
        row = _sent_row(self.user, resend_count=2, resent_ago=timedelta(minutes=1))  # 3 sends
        _sent_row(self.user)  # 4 sends
        with patch(_SMS_BACKEND_PATCH, side_effect=_mock_sms_backend) as sms, self._slow_count():
            results = self._run_parallel(
                lambda: create_otp_record(self.user),
                lambda: resend_otp(str(row.otp_session_token)),
            )
        self.assertEqual(sorted(results), ["OTPRateLimitError", "ok"], results)
        self.assertEqual(otp_service._recent_send_count(self.user), 5)
        self.assertEqual(sms.call_count, 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `venv/Scripts/python.exe manage.py test apps.accounts.tests -v 2`
Expected: ERROR — `ImportError: cannot import name 'OTPRateLimitError'`.

- [ ] **Step 3: Implement in `apps/accounts/services/otp.py`**

Add imports: `from django.contrib.auth import get_user_model`, `from django.db import transaction`, `from django.db.models import Sum` (and `from django.conf import settings` if not already imported). Add `User = get_user_model()` after imports.

Add above `create_otp_record`:

```python
class OTPRateLimitError(Exception):
    """A mobile number has reached its hourly OTP send limit."""

    message = "Too many OTP requests for this number. Please try again later."

    def __init__(self):
        super().__init__(self.message)


def _recent_send_count(user) -> int:
    """SMS sends for this user in the last hour: one per row created in the
    window plus resend_count of rows resent in the window. Resend updates a
    row in place, so rows alone undercount. This never undercounts; it can
    overcount older resends on a row resent inside the window."""
    cutoff = timezone.now() - timedelta(hours=1)
    rows = OTPVerification.objects.filter(user=user)
    initial = rows.filter(created_at__gte=cutoff).count()
    resends = rows.filter(last_resend_at__gte=cutoff).aggregate(
        total=Sum("resend_count")
    )["total"] or 0
    return initial + resends


def _lock_user(user_pk):
    """Serialize OTP sends per mobile. Caller must be inside a transaction."""
    return User.objects.select_for_update().get(pk=user_pk)


def _check_mobile_send_limit(user) -> None:
    """Raise OTPRateLimitError at the per-mobile limit. The user row must
    already be locked so concurrent senders cannot share a stale count."""
    if _recent_send_count(user) >= settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR:
        raise OTPRateLimitError()
```

`create_otp_record`: add `@transaction.atomic` above `def`, add `Raises: OTPRateLimitError` to the docstring, and insert as the first two statements of the body:

```python
    _lock_user(user.pk)
    _check_mobile_send_limit(user)
```

`resend_otp`: add `@transaction.atomic` above `def`, add `OTPRateLimitError` to the docstring `Raises`, and replace the record lookup (`try: record = ... except ... raise ValueError(...)`) with:

```python
    session = OTPVerification.objects.filter(
        otp_session_token=otp_session_token, is_active=True
    ).values_list("user_id", flat=True).first()
    if session is None:
        raise ValueError("Invalid or expired session. Please request a new OTP.")
    _lock_user(session)  # same lock as create_otp_record, taken first
    record = (
        OTPVerification.objects.select_related("user")
        .select_for_update(of=("self",))
        .get(otp_session_token=otp_session_token, is_active=True)
    )
```

Then, after the existing cooldown check and before `# Generate fresh OTP and update the existing record`, insert:

```python
    _check_mobile_send_limit(record.user)
```

The rest of `resend_otp` is unchanged.

- [ ] **Step 4: Map to 429 in `apps/accounts/views.py`**

Import `OTPRateLimitError` from `apps.accounts.services.otp`.

`RequestOTPView.post` — wrap the service call:

```python
        try:
            record, sms_sent = create_otp_record(user, is_new_user=created, purpose="login")
        except OTPRateLimitError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
```

`ResendOTPView.post` — add before `except ValueError`:

```python
        except OTPRateLimitError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
```

Update both views' `429` schema descriptions to: "Rate limit exceeded (per IP or per mobile number)."

- [ ] **Step 5: Run tests**

Run: `venv/Scripts/python.exe manage.py test apps.accounts.tests -v 2`
Expected: all OK, including both concurrency tests.

Lock proof (do not commit): temporarily comment out `_lock_user(user.pk)` in `create_otp_record`, rerun `apps.accounts.tests.MobileSendLimitConcurrencyTests.test_two_requests_at_limit_minus_one`, expect FAIL with `['ok', 'ok']`; restore the line and confirm `git diff apps/accounts/services/otp.py` shows it present.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/services/otp.py apps/accounts/views.py apps/accounts/tests.py
git commit -m "feat(accounts): per-mobile OTP send limit under user lock"
```

---

### Task 4: Validation and local records

**Files:**
- Local only, never committed: `.claude/DECISIONS.md`, `.claude/CURRENT_STATE.md`

- [ ] **Step 1: Canonical full suite**

```bash
venv/Scripts/python.exe manage.py test apps.accounts.tests apps.cart.tests apps.catalog.tests apps.catalog.tests_parfumly apps.core.tests apps.inventory.tests apps.notifications.tests apps.offers.tests apps.orders.tests apps.payments.tests apps.purchases.tests apps.reviews.tests apps.shipping.tests
```

Expected: OK. Report the actual module and test counts run.

- [ ] **Step 2: Checks**

```bash
venv/Scripts/python.exe manage.py check
venv/Scripts/python.exe manage.py makemigrations --check --dry-run
```

Expected: no issues; "No changes detected".

- [ ] **Step 3: Local records**

Append `DEC-012 — Redis for shared throttle counters only` to `.claude/DECISIONS.md` (Decision / Reason / Impact / Status: Implemented on `feat/redis-throttling`, not merged), summarising spec §4, §6, §7 and the rejected search caching. Update `.claude/CURRENT_STATE.md` Current Focus with the branch, commits, test counts, and the deployment prerequisites (Redis provisioned, `REDIS_URL` and `NUM_PROXIES` set per environment). Do not stage either file.

- [ ] **Step 4: Confirm clean scope**

```bash
git status --short
git log --oneline dev..HEAD
```

Expected: only `.claude/CLAUDE.md` (user's edit) shown as modified; 4 commits on the branch (spec + Tasks 1–3). Stop and report; no merge.
