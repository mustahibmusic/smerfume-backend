# Redis Shared Throttling and OTP Abuse Protection — Design

Date: 2026-09-30
Branch: `feat/redis-throttling`
Status: APPROVED (2026-09-30)

## 1. Goal

Make API rate limits and OTP abuse protection work correctly when the
application runs as several processes or servers. Redis is used for one
thing only: shared storage for DRF throttle counters.

## 2. Current Problems

1. No `CACHES` setting exists. DRF throttles use Django's per-process
   `LocMemCache`, so every worker keeps its own counters. With N workers
   the OTP limits (`otp_request 5/hour`, `otp_verify 10/minute`) are N
   times weaker, and all counters reset on every restart.
2. `NUM_PROXIES` is not set. Behind a load balancer or reverse proxy, DRF
   sees the proxy address as the client IP. After counters become shared,
   every customer would share one OTP bucket and one anonymous bucket.
3. The global `anon: 100/day` limit applies to every public endpoint,
   including catalogue browsing, cart and guest checkout. It is harmless
   today only because it is per process. After counters become shared,
   normal shoppers and customers behind a shared mobile-carrier IP would
   receive HTTP 429.
4. OTP requests are limited per IP only. An attacker that rotates IP
   addresses can send unlimited OTP SMS messages to one mobile number.
   This costs SMS money and harasses the number owner.
5. `ResendOTPView` has no OTP throttle. It uses only the global `anon`
   throttle, although every successful resend sends an SMS. Its
   `resend_count` check also runs without a row lock, so two concurrent
   resends can both pass it.

## 3. Scope

In scope:

- Redis as the default Django cache, used by DRF throttles.
- Per-environment Redis configuration and failure policy.
- `NUM_PROXIES` configuration.
- Corrected throttle rates.
- Per-mobile OTP send limit, stored in PostgreSQL, applied to both OTP
  request and OTP resend.
- OTP throttle on `ResendOTPView`.
- Tests, plus a local `.claude/DECISIONS.md` entry (gitignored).

Out of scope (decided, see §11):

- Search or product response caching.
- Moving `OTPVerification` or the resend cooldown out of PostgreSQL.
- Sessions in Redis, Redis locks, Celery, background jobs.
- Changes to catalogue search.
- New throttles on cart, checkout or other endpoints.

## 4. Responsibilities

| Concern | Store |
|---|---|
| DRF throttle counters (`anon`, `user`, `otp_request`, `otp_verify`) | Redis |
| OTP hash, expiry, attempt count, resend count and cooldown, audit | PostgreSQL (`OTPVerification`, unchanged) |
| Per-mobile OTP send limit | PostgreSQL (derived from `OTPVerification` rows, see §8.3) |
| Catalogue, stock, prices, search | PostgreSQL (unchanged) |

Redis holds only short-lived counters. Losing all Redis data resets rate
limit windows and nothing else. No business data is stored in Redis.

## 5. Configuration

### 5.1 Environment variables

| Variable | Local | Staging / UAT / Production |
|---|---|---|
| `REDIS_URL` | Optional | Required |
| `NUM_PROXIES` | Optional, default `0` | Required (an explicit `0` is allowed) |

A missing required variable raises `ImproperlyConfigured` when settings
load, so the process does not start. There is no silent fallback.

`NUM_PROXIES` must match the actual proxy topology of each deployment:
the number of reverse proxies and load balancers that sit in front of
Django and append to `X-Forwarded-For`. This design does not assume a
value. The deployment owner confirms the topology and sets it per
environment. A value that is too high lets a client spoof its IP through
`X-Forwarded-For`. A value that is too low collapses all clients into the
proxy IP, so every customer shares one throttle bucket. If the topology
changes (for example, a CDN is added), the value must be updated.

### 5.2 Cache backend

Use Django's built-in `django.core.cache.backends.redis.RedisCache`
(Django 6.0). The only new dependency is the `redis` package. No
`django-redis`.

A small helper in `config/settings/base.py` builds the cache setting:

```python
def redis_cache(url):
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

The short socket timeouts stop a dead Redis from blocking requests for a
long time.

A second helper, `require_env(name)`, returns the variable or raises
`ImproperlyConfigured`.

- `local.py`: `CACHES = redis_cache(REDIS_URL)` when `REDIS_URL` is set;
  otherwise `LocMemCache`. `NUM_PROXIES` defaults to `0`.
- `staging.py`, `uat.py`, `production.py`:
  `CACHES = redis_cache(require_env("REDIS_URL"))` and
  `NUM_PROXIES = int(require_env("NUM_PROXIES"))`.

`NUM_PROXIES` is set inside `REST_FRAMEWORK` in each environment file.

## 6. Throttle Rates

These values are configurable initial defaults, not permanent business
rules. They live in settings and can be tuned per environment from
production traffic without a code change to the throttle classes.

| Limit | Current base | Initial default | Keyed by | Store |
|---|---|---|---|---|
| `anon` | 100/day | 120/min | client IP | Redis |
| `user` | 1000/day | 300/min | user id | Redis |
| `otp_request` (request and resend) | 5/hour (request only) | 20/hour | client IP | Redis |
| `otp_verify` | 10/minute | 10/minute (unchanged) | client IP | Redis |
| OTP sends per mobile (new) | none | 5/hour | mobile number (user) | PostgreSQL |

Settings names:

- `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]` for the four DRF scopes.
- `OTP_MAX_SENDS_PER_MOBILE_PER_HOUR = 5` for the per-mobile limit.

What each limit protects:

- **`anon` and `user`: general burst protection.** They stop scraping and
  request floods against any endpoint. They are per minute, not per day,
  because a daily cap blocks real shoppers once counters are shared.
- **`otp_request` per IP: infrastructure abuse.** It stops one source from
  flooding the OTP endpoints across many numbers. It is set to 20/hour,
  not 5/hour, because many customers share one carrier IP (CGNAT), which
  is common on Indian mobile networks, especially during a campaign.
- **OTP sends per mobile: the customer's number and SMS cost.** It stops
  an attacker that rotates IPs from sending repeated SMS messages to one
  number. It is the only limit that follows the number, not the source.

There is one rate per scope. No burst-plus-sustained tiers in this phase.

`local.py` keeps relaxed rates for development, in the same units.

## 7. Failure Policy

### 7.1 Local

- `REDIS_URL` unset: `LocMemCache` is used. This is acceptable for one
  developer process.
- `REDIS_URL` set: Redis is used, with the same runtime failure behaviour
  as §7.2.
- `NUM_PROXIES` unset: defaults to `0`.

### 7.2 Staging, UAT, Production

Startup:

- Missing `REDIS_URL` or `NUM_PROXIES`: settings raise
  `ImproperlyConfigured` and the process does not start.
- There is never a fallback to `LocMemCache` in these environments.

Redis unreachable at runtime:

| Endpoints | Behaviour | Reason |
|---|---|---|
| OTP request, resend, verify | **Fail closed**: HTTP 503, generic message "Login is temporarily unavailable. Please try again shortly." No OTP row is created or updated and no SMS is sent. | A missing IP limit would weaken OTP abuse protection. |
| All other endpoints (`anon` and `user` throttles) | **Fail open**: the request is allowed. | Browsing, cart and checkout keep working. Stock and payment safety do not depend on these throttles. |

In both cases:

- The error is logged at `ERROR` level with the throttle scope.
- No Redis error detail is returned to the client.
- Only `redis.exceptions.RedisError` is handled. Any other exception
  propagates as before.

The per-mobile limit uses PostgreSQL, so it keeps working when Redis is
down. It is never reached in that case, because the OTP throttle fails
closed first.

UAT uses the same policy as production, so failure behaviour can be
tested before go-live.

## 8. Components

### 8.1 `apps/core/throttling.py` (new)

- `ThrottleUnavailable(APIException)`: status 503, generic message.
- `FailClosedScopedRateThrottle(ScopedRateThrottle)`: calls the parent
  `allow_request`; on `redis.exceptions.RedisError` it logs and raises
  `ThrottleUnavailable`.
- `FailOpenAnonRateThrottle(AnonRateThrottle)` and
  `FailOpenUserRateThrottle(UserRateThrottle)`: call the parent
  `allow_request`; on `redis.exceptions.RedisError` they log and return
  `True`.

### 8.2 Settings and views

- `DEFAULT_THROTTLE_CLASSES` points to the two fail-open classes.
- `RequestOTPView` and `VerifyOTPView` use `FailClosedScopedRateThrottle`
  with their existing scopes.
- `ResendOTPView` uses `FailClosedScopedRateThrottle` with scope
  `otp_request`. Request and resend share one per-IP counter, because
  both send an SMS.

### 8.3 Per-mobile OTP send limit

**Current behaviour (verified in `apps/accounts/services/otp.py`):**

- `create_otp_record` (OTP request) always creates a **new**
  `OTPVerification` row and sends one SMS.
- `resend_otp` does **not** create a row. It **updates the existing row
  in place**: new hash, new expiry, `resend_count += 1`,
  `last_resend_at = now`, and sends one SMS. Up to
  `MAX_RESENDS = 3` resends per row.
- So one row can represent up to 4 SMS sends (1 initial + 3 resends).

**Conclusion:** counting rows does not count actual sends. With a limit of
5 rows per hour, one number could receive up to 20 SMS per hour. Plain row
counting is rejected.

**Solution (no new model, no migration):** count sends from the existing
fields. For one user and `cutoff = now - 1 hour`:

```
sends = count(rows where created_at >= cutoff)
      + sum(resend_count of rows where last_resend_at >= cutoff)
```

Why this is safe:

- Every send inside the window is counted. An initial send inside the
  window means `created_at >= cutoff`. A resend inside the window means
  `last_resend_at >= cutoff`, and that row's `resend_count` includes it.
- It can overcount, never undercount. A row resent inside the window may
  also have older resends from before the window; those are included.
  The error is at most 2 extra sends per row, and it only makes the limit
  stricter.
- The existing per-row limits (`MAX_RESENDS = 3`, 30-second cooldown) stay
  unchanged.

**Enforcement:**

- A new helper, `_check_mobile_send_limit(user)`, runs before any SMS is
  sent. It raises `OTPRateLimitError` when `sends` has reached
  `OTP_MAX_SENDS_PER_MOBILE_PER_HOUR`.
- `create_otp_record` and `resend_otp` both run in a transaction and lock
  the user row with `select_for_update()` before the check. This makes
  concurrent requests and resends for one number count correctly. In
  `resend_otp` it also closes the existing race on the `resend_count`
  check.
- `RequestOTPView` and `ResendOTPView` map `OTPRateLimitError` to HTTP 429
  with a generic message: "Too many OTP requests for this number. Please
  try again later." The message does not reveal whether the account
  exists; a new number can never reach the limit on its first request.

**Fallback if exact counting is later required:** a small append-only
`OTPSendEvent(user, created_at)` table with one row per SMS send, counted
by `created_at`. This needs a model and a migration, so it is not used
now.

## 9. Data Flow — OTP Request and Resend

1. Request arrives. DRF reads the client IP using `NUM_PROXIES`.
2. `FailClosedScopedRateThrottle` checks the `otp_request` counter in
   Redis. Redis down: 503. Over limit: 429.
3. Serializer validates the input.
4. Request: the user is fetched or created. Resend: the session row is
   found.
5. The service locks the user row and checks the per-mobile send limit in
   PostgreSQL. Over limit: 429.
6. The service creates (request) or updates (resend) the
   `OTPVerification` row and sends the SMS.

## 10. Testing

- Throttles, with the cache mocked to raise `RedisError`:
  - OTP request, resend and verify return 503; no `OTPVerification` row is
    created or changed.
  - A catalogue list request returns 200 (fail open).
  - Both cases log at `ERROR`.
- Resend and request share the `otp_request` per-IP counter.
- Per-mobile send limit:
  - Five sends within an hour are allowed; the sixth returns 429.
  - Resends count: 2 requests + 3 resends reach the limit, and the next
    request or resend returns 429.
  - A different number is not affected.
  - Rows and resends older than one hour are not counted.
- `NUM_PROXIES`: with `NUM_PROXIES=1`, two requests with different
  `X-Forwarded-For` addresses use separate throttle buckets.
- Settings helpers: `require_env` raises `ImproperlyConfigured` when the
  variable is missing and returns the value when present; `redis_cache`
  returns the expected structure.
- Existing accounts tests pass unchanged, apart from updated rate values.
- Validation before merge: canonical full test suite, `manage.py check`,
  `makemigrations --check --dry-run` (no migrations expected).

Tests do not need a running Redis server.

## 11. Decisions Recorded

- No search or product response caching. A measurement on 2026-09-30
  (5,025 products, `?search=teriaq`) showed 7 queries per request, no
  N+1, and about 35 ms of database time. That load fits the campaign
  scenario (1,000 searches in 10 minutes) with large headroom.
- Deferred technical debt: the `pg_trgm` GIN indexes are not used by the
  current search. DRF `icontains` produces `UPPER(name) LIKE`, which does
  not match the `gin_trgm_ops` indexes on `name`, and the OR across three
  joined tables forces sequential scans. Revisit when the catalogue grows
  or search time rises. Catalogue search is not changed in this phase.
- `OTPVerification` stays in PostgreSQL.

## 12. Files

- `config/settings/base.py`
- `config/settings/local.py`
- `config/settings/staging.py`
- `config/settings/uat.py`
- `config/settings/production.py`
- `requirements.txt` (add `redis`; the file is UTF-16 encoded)
- `apps/core/throttling.py` (new)
- `apps/core/tests.py`
- `apps/accounts/services/otp.py`
- `apps/accounts/views.py`
- `apps/accounts/tests.py`

Local only (gitignored, never committed): `.claude/DECISIONS.md` gets the
decision entry and `.claude/CURRENT_STATE.md` gets the state update.

The deployment owner adds `REDIS_URL` and `NUM_PROXIES` to the
environment files for staging, UAT and production. These files are not
committed.

## 13. Rollout Notes

- Provision Redis for staging, UAT and production before deploying this
  branch; otherwise the process will not start.
- Confirm the proxy count for each environment before setting
  `NUM_PROXIES`.
- No database migration. Rolling back the code restores per-process
  throttling.
