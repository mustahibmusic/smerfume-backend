# Session Handoff

**Date:** 2026-08-19
**Commits this session:** `24ba39c`, `4427e70`

---

## 1. What We Worked On

- Full passwordless OTP authentication system (mobile number → OTP → JWT)
- Swagger / ReDoc API documentation for all apps
- Multi-environment configuration (`local` / `uat` / `prod`)
- Secret hygiene: all credentials moved to git-ignored env files, `.env.example` removed

---

## 2. Files Modified

| File | Change |
|---|---|
| `apps/accounts/models.py` | Added `public_id` UUID to User; rewrote OTPVerification with hashed OTP, security fields, resend limits |
| `apps/accounts/serializers.py` | New file — RequestOTP, VerifyOTP, ResendOTP, UserMe, UpdateMe serializers |
| `apps/accounts/views.py` | New file — 5 auth views with `@extend_schema` |
| `apps/accounts/urls.py` | New file — 6 URL patterns including tagged TokenRefreshView |
| `apps/accounts/tests.py` | Fully rewritten — 32 auth tests |
| `apps/cart/serializers.py` | Added `@extend_schema_field` to fix schema warnings |
| `apps/cart/views.py` | Added `@extend_schema` decorators |
| `apps/catalog/serializers.py` | Added `@extend_schema_field` to `get_notes` |
| `apps/catalog/views.py` | Added `@extend_schema_view` to all 4 ViewSets |
| `apps/orders/views.py` | Added `@extend_schema` to all 3 views |
| `config/settings/base.py` | Added `drf_spectacular`, `token_blacklist`, SMS settings, SPECTACULAR_SETTINGS |
| `config/settings/local.py` | Added SIMPLE_JWT config, relaxed throttle |
| `config/settings/production.py` | Filled in — HSTS, SSL redirect, secure cookies, tighter JWT |
| `config/urls.py` | Added `/api/schema/`, `/api/docs/`, `/api/redoc/` |
| `manage.py` | Loads `.env.{DJANGO_ENV}` instead of `.env` |
| `config/wsgi.py` | Same dotenv logic as manage.py |
| `config/asgi.py` | Same dotenv logic as manage.py |
| `.gitignore` | Added `.env.*` and `.claude/`; removed granular env entries |
| `requirements.txt` | Added `drf-spectacular==0.30.0` |

---

## 3. Files Created

| File | Purpose |
|---|---|
| `apps/accounts/migrations/0004_auth_hardening.py` | Migrates User.public_id (3-step unique UUID pattern), rewrites OTPVerification schema |
| `apps/accounts/providers/__init__.py` | Package init |
| `apps/accounts/providers/base.py` | Abstract `BaseSMSBackend` — all SMS vendors extend this |
| `apps/accounts/providers/console.py` | Dev backend — prints OTP + session token to stdout |
| `apps/accounts/providers/msg91.py` | Production MSG91 backend (v5 API, 5s timeout, full error handling) |
| `apps/accounts/services/__init__.py` | Package init |
| `apps/accounts/services/otp.py` | OTP lifecycle: `create_otp_record`, `verify_otp_record`, `resend_otp` |
| `apps/accounts/services/jwt.py` | `get_tokens_for_user` — returns access + refresh JWT pair |
| `config/settings/uat.py` | UAT settings — DEBUG=False, CORS from env, standard JWT |
| `.env.local` | Local dev secrets (git-ignored) |
| `.env.uat` | UAT secrets template (git-ignored) |
| `.env.prod` | Prod secrets template (git-ignored) |
| `docs/SESSION_HANDOFF.md` | This file |

---

## 4. Database Changes

Migration `0004_auth_hardening` applied to local DB:

- `accounts_user`: Added `public_id` UUID (unique, indexed)
- `accounts_otpverification`: Replaced plaintext `code` field with `otp_hash` (PBKDF2), added `otp_session_token` UUID, `purpose`, `expires_at`, `attempt_count`, `resend_count`, `last_resend_at`, `is_new_user`
- `accounts_customerprofile`: `first_name` set to `blank=True`
- `token_blacklist` tables: Added by `rest_framework_simplejwt.token_blacklist`

Pending untracked migration: `apps/orders/migrations/0002_alter_shippingaddress_state.py` (pre-existed, not created this session).

---

## 5. APIs Created or Changed

All new. Base path: `/api/auth/`

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/otp/request/` | None | Send OTP to mobile number |
| POST | `/api/auth/otp/verify/` | None | Verify OTP → returns JWT pair |
| POST | `/api/auth/otp/resend/` | None | Resend OTP on same session |
| POST | `/api/auth/token/refresh/` | None | Refresh access token |
| POST | `/api/auth/logout/` | Bearer | Blacklist refresh token |
| GET/PATCH | `/api/auth/me/` | Bearer | Get or update own profile |

Swagger UI: `http://127.0.0.1:8000/api/docs/`
ReDoc: `http://127.0.0.1:8000/api/redoc/`

---

## 6. Tests Created or Changed

`apps/accounts/tests.py` — fully rewritten, 32 tests across 6 classes:

- `OTPServiceTests` — unit tests for OTP service layer (mock SMS backend)
- `RequestOTPViewTests` — new user, existing user, invalid mobile
- `VerifyOTPViewTests` — success, wrong OTP, expired, locked, already verified
- `ResendOTPViewTests` — success, cooldown, limit reached
- `LogoutViewTests` — valid logout, invalid token
- `MeViewTests` — GET profile, PATCH profile, unauthenticated

All 123 tests passing (32 auth + 91 pre-existing catalog/cart/orders).

---

## 7. Current Implementation Status

| Area | Status |
|---|---|
| Auth (OTP + JWT) | Complete |
| Catalog APIs | Complete |
| Cart APIs | Complete |
| Orders / Checkout | Complete (stub) |
| Swagger docs | Complete — 0 warnings |
| Multi-env config | Complete |
| Inventory | Not started |
| Payments | Not started |
| Saved addresses | Not started (deferred) |
| Guest cart merge | Not started (deferred) |
| Notifications | Not started |

---

## 8. Current Errors or Known Issues

- **`apps/orders/migrations/0002_alter_shippingaddress_state.py`** — untracked migration file present locally, origin unknown, not yet committed. Needs investigation before next orders work.
- **`Order.user` is a required FK** — guest checkout is blocked until this is made nullable. Explicitly deferred this session.
- **`CustomerProfile` is OneToOne** — deferred; will need ForeignKey migration when multi-recipient gift support is needed.
- **Old `.env` file** still exists on local disk (not tracked). Safe to delete manually.

---

## 9. Decisions Made This Session

**SMS provider abstraction pattern:**
Same as Django's `EMAIL_BACKEND` — dotted class path in `SMS_BACKEND` env var, resolved via `import_string`. Switching SMS vendors requires only changing `SMS_BACKEND` in the env file. No code changes needed.

**Two-setting SMS gate:**
- `SEND_REAL_OTP=False` → always use `ConsoleSMSBackend` regardless of `SMS_BACKEND`
- `SEND_REAL_OTP=True` → use the class at `SMS_BACKEND`
Prevents accidental SMS charges in dev/UAT.

**OTP security approach:**
OTP hashed with PBKDF2 (`make_password`) before storage — plain OTP is generated, sent, and discarded. Never stored in plaintext.

**`otp_session_token` UUID pattern:**
Decouples OTP request from verify step. Client holds the session token; server never re-exposes the OTP. Prevents session fixation and replay across requests.

**Account enumeration prevention:**
`POST /api/auth/otp/request/` always returns HTTP 200 — never reveals whether a mobile number is registered.

**Multi-environment switching:**
`DJANGO_ENV` env var (set at OS/server level, not in any `.env` file) controls which `.env.{env}` file loads and which settings module is used. Default is `local`. No `.env.example` — key names are also kept private.

**No credentials in git history:**
Verified via `git log` scan — no actual credential values were ever committed. `.env` was git-ignored from the initial commit.

---

## 10. Next Recommended Task

**Implement the Inventory app.**

The `apps/inventory` app exists but is empty. Per CLAUDE.md, inventory must support:
retail bottles, testers, partial bottles, decanting, damaged stock, promotional/gift stock, and returns.

Suggested starting point:
1. Design the `InventoryItem` model (linked to `Variant`, with `stock_type` choices)
2. Add stock tracking fields (`quantity`, `unit`, `cost_price`)
3. Admin registration via Django Unfold
4. Stock movement / audit log model
5. Expose available stock on the catalog variant API response

Before starting: inspect `apps/inventory/` current state and `apps/catalog/models.py` Variant model to understand the existing relationship.
