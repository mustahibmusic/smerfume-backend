# Smerfume Backend — Architecture

## Project Overview

Smerfume is a multi-brand fragrance e-commerce platform targeting the Indian market. The backend is a Django REST Framework API serving a (forthcoming) frontend. The business currently sells via Instagram DMs and WhatsApp; this backend is the foundation for a structured online shopping experience.

**Domain:** smerfume.com

---

## Technology Stack (CURRENT)

| Layer | Technology | Version |
|---|---|---|
| Language | Python | 3.x |
| Web framework | Django | 6.0.2 |
| API layer | Django REST Framework | 3.16.1 |
| Database | PostgreSQL | — |
| DB driver | psycopg2-binary | 2.9.11 |
| Authentication | djangorestframework-simplejwt | 5.5.0 |
| Admin UI | django-unfold | 0.78.1 |
| CORS | django-cors-headers | 4.7.0 |
| API documentation | drf-spectacular | 0.30.0 |
| File storage | django-storages + boto3 | — |
| Env management | python-dotenv | 1.2.1 |
| ASGI server | uvicorn / asgiref | 3.11.1 |

**Not currently installed:** Celery, Redis, django-channels, any payment SDK, any shipping SDK.

---

## Django Project Structure

```
smerfume-backend/
├── config/
│   ├── settings/
│   │   ├── base.py          # Shared settings for all environments
│   │   ├── local.py         # Local dev overrides (DEBUG=True, relaxed throttle)
│   │   ├── uat.py           # UAT overrides (DEBUG=False, CORS from env)
│   │   └── production.py    # Production (HSTS, SSL redirect, tighter JWT)
│   ├── urls.py              # Root URL config (admin, api/, schema docs)
│   ├── api_urls.py          # API router (auth/, catalog/, cart/, orders/)
│   ├── wsgi.py              # WSGI entry point
│   └── asgi.py              # ASGI entry point (not async currently)
├── apps/
│   ├── core/                # Shared base models and custom permissions
│   ├── accounts/            # Auth, users, OTP sessions
│   ├── catalog/             # Products, editions, variants, brands, notes
│   ├── cart/                # Shopping cart
│   ├── orders/              # Checkout and order management
│   ├── inventory/           # STUB — no models yet
│   ├── payments/            # STUB — no models yet
│   ├── shipping/            # STUB — no models yet
│   ├── offers/              # STUB — no models yet
│   ├── reviews/             # STUB — no models yet
│   └── notifications/       # STUB — no models yet
├── manage.py                # Loads .env.{DJANGO_ENV}, sets DJANGO_SETTINGS_MODULE
├── requirements.txt         # UTF-16-LE encoded
└── .env.{local|uat|prod}    # Git-ignored; DJANGO_ENV set at OS level
```

### Environment Switching

`DJANGO_ENV` (OS environment variable, default `local`) controls both the `.env` file loaded and the settings module imported. Supported values: `local`, `uat`, `prod`.

---

## Django Applications

### `apps.core`
Foundation layer. Does not expose any API endpoints.

- **`BaseModel`** — abstract model providing `id` (BigAutoField PK), `public_id` (UUID, unique), `is_active` (bool), `created_at`, `updated_at`. All domain models extend this.
- **`TimeStampedModel`** — abstract model with only timestamps (BaseModel extends this).
- **Custom permissions** (`permissions.py`) — `IsCustomer`, `IsStaffMember`, `IsAdminUser` extend DRF's `IsAuthenticated` and additionally check `request.user.role`.

### `apps.accounts`
Authentication and user management.

- Custom `User` model (extends `AbstractUser`) with `mobile_number` as the primary login field and `email` as `USERNAME_FIELD`.
- OTP-based passwordless login via `OTPVerification` model and service layer.
- JWT token issuance via `djangorestframework-simplejwt`.
- `CustomerProfile` — OneToOne extension of User for recipient/gift contact details.
- SMS provider abstraction: toggleable via `SEND_REAL_OTP` + `SMS_BACKEND` env vars.
- Admin registered via Django Unfold.

### `apps.catalog`
Read-only public product catalogue. All endpoints are `AllowAny`.

- Hierarchy: `Brand` → `Product` → `ProductEdition` → `ProductVariant`
- Perfume notes managed via `PerfumeNote` + `EditionNote` (through-table with position: top/heart/base).
- Full filtering, search, and ordering on the product list endpoint.
- Soft-delete pattern: `is_active` flag on all models; inactive records excluded from API responses.

### `apps.cart`
Shopping cart management. All endpoints require authentication (JWT).

- `Cart` supports both authenticated users (FK to User) and anonymous sessions (session_key). Currently, only authenticated carts are used by the API; anonymous/guest cart merge is not implemented.
- `CartItem` links a Cart to a `ProductVariant` with a quantity.
- Quantity capped at 99 per line item.

### `apps.orders`
Checkout and order history. All endpoints require authentication.

- `CheckoutView` converts the authenticated user's cart into an `Order` atomically.
- `Order.user` is a **required** FK — guest checkout is not currently supported.
- `ShippingAddress` stored per-order (one-to-one); no reusable address book.
- Order number format: `SMR-YYYYMMDD-XXXXXX` (date + 6-char hex suffix).
- Discount/offers integration: subtotal == total (no discount applied yet; placeholder comment in code).

### `apps.inventory` — NOT YET IMPLEMENTED
App registered in `INSTALLED_APPS` but `models.py` is empty. No migrations, no APIs.

**PLANNED:** Stock tracking for retail bottles, testers, partial bottles, decants, damaged stock, promotional/gift stock, and returns. Zoho Books integration planned but not started.

### `apps.payments` — NOT YET IMPLEMENTED
App registered. No models, no APIs. Currently payments are taken via Google Pay on a personal UPI ID outside the system.

### `apps.shipping` — NOT YET IMPLEMENTED
App registered. No models, no APIs. No shipping provider integrated.

### `apps.offers` — NOT YET IMPLEMENTED
App registered. No models, no APIs. Discount logic is a placeholder in `CheckoutView` (`total = subtotal`).

### `apps.reviews` — NOT YET IMPLEMENTED
App registered. No models, no APIs.

### `apps.notifications` — NOT YET IMPLEMENTED
App registered. No models, no APIs.

---

## Application Dependency Graph

```
core
 └─ accounts (imports BaseModel)
 └─ catalog  (imports BaseModel)
 └─ cart     (imports BaseModel, catalog.ProductVariant)
 └─ orders   (imports BaseModel, catalog.ProductVariant, cart.Cart)
```

All apps import from `core`. `cart` and `orders` import from `catalog`. `orders` imports from `cart` (reads cart at checkout; does not import models permanently). No circular dependencies.

---

## Request / Response Flow

```
Client
  │
  ▼
Django URL Router (config/urls.py)
  │
  ├─ /admin/           → Django Admin (Unfold theme)
  ├─ /api/schema/      → drf-spectacular raw OpenAPI schema
  ├─ /api/docs/        → Swagger UI
  ├─ /api/redoc/       → ReDoc
  └─ /api/             → config/api_urls.py
                            ├─ auth/     → apps.accounts.urls
                            ├─ catalog/  → apps.catalog.urls (DefaultRouter)
                            ├─ cart/     → apps.cart.urls
                            └─ orders/   → apps.orders.urls
```

**Middleware stack (in order):**
1. `SecurityMiddleware`
2. `CorsMiddleware` (django-cors-headers)
3. `SessionMiddleware`
4. `CommonMiddleware`
5. `CsrfViewMiddleware`
6. `AuthenticationMiddleware`
7. `MessageMiddleware`
8. `XFrameOptionsMiddleware`

DRF's `JWTAuthentication` and `SessionAuthentication` run as DRF authentication classes (not Django middleware).

---

## Authentication Architecture (CURRENT)

Passwordless OTP-based login, aligned with Indian e-commerce norms (Flipkart/Myntra/Nykaa pattern).

### Flow

```
1. POST /api/auth/otp/request/
   - Accepts Indian mobile number (10-digit, 12-digit, or E.164 +91...)
   - Creates User if not exists (username = user_{last4}_{hex6})
   - Generates 6-digit OTP via secrets.randbelow (cryptographically secure)
   - Hashes OTP with PBKDF2 (Django's make_password) — plaintext discarded
   - Sends via SMS backend (console in dev, MSG91 in prod)
   - Returns otp_session_token (UUID) — plaintext OTP never returned to client

2. POST /api/auth/otp/verify/
   - Accepts otp_session_token + 6-digit OTP
   - Checks: active session, not verified, not expired (5 min), not locked (3 attempts)
   - On success: marks OTPVerification.is_verified=True, issues JWT pair
   - Returns: access token (60 min), refresh token (7 days), is_new_user flag, user object

3. POST /api/auth/token/refresh/
   - Exchanges refresh token for new access token
   - ROTATE_REFRESH_TOKENS=True: issues new refresh token, blacklists old one

4. POST /api/auth/logout/
   - Blacklists the refresh token (rest_framework_simplejwt.token_blacklist)
   - Access token expires naturally (no server-side revocation for access tokens)
```

### Security Measures

| Measure | Implementation |
|---|---|
| Account enumeration prevention | `otp/request/` always returns HTTP 200 |
| OTP brute force protection | Session locks after 3 failed attempts (`MAX_ATTEMPTS=3`) |
| OTP expiry | 5 minutes (`OTP_EXPIRY_MINUTES=5`) |
| Resend limiting | Max 3 resends (`MAX_RESENDS=3`), 30-second cooldown |
| OTP storage | PBKDF2 hash only; plaintext never persisted |
| Session isolation | `otp_session_token` UUID — client references session without exposing identity |
| Token rotation | Refresh token rotated and blacklisted on each refresh |
| Public identifier | `public_id` UUID exposed in API responses; integer PK never exposed |

### JWT Configuration (local/UAT)
- Access token lifetime: 60 minutes
- Refresh token lifetime: 7 days
- Auth header: `Authorization: Bearer <access_token>`

### JWT Configuration (production)
- Access token lifetime: 15 minutes
- Refresh token lifetime: 7 days

### Throttling

| Scope | Rate (local) | Rate (prod/UAT) |
|---|---|---|
| `otp_request` | 100/hour | 5/hour |
| `otp_verify` | 1000/minute | 10/minute |
| `anon` | 1000/day | 100/day |
| `user` | 10000/day | 1000/day |

---

## Product / Catalogue Architecture (CURRENT)

### Hierarchy

```
Brand
  └─ Product (FK → Brand, FK → Category)
       └─ ProductEdition (FK → Product; gender, concentration, image, notes)
            ├─ ProductVariant (FK → Edition; size_ml, is_decant, mrp, selling_price)
            └─ EditionNote (M2M through-table; FK → Edition, FK → PerfumeNote, position)
```

- **Product** is the top-level catalogue item (e.g. "Baccarat Rouge 540").
- **ProductEdition** represents a specific fragrance version (e.g. "EDP Men's"). Multiple editions per product are supported.
- **ProductVariant** represents a purchasable SKU (e.g. "50ml", "10ml Decant").
- **EditionNote** links notes to editions with a position (top/heart/base).

All catalogue endpoints are read-only (`ReadOnlyModelViewSet`). No write APIs for catalogue management — catalogue is managed via Django Admin.

### Filtering (Product List)
Products can be filtered by brand slug, category slug, gender, concentration, perfume notes (AND logic, max 5), `is_best_seller`, `is_new_arrival`. Edition-level filters are implemented as subqueries to prevent cross-edition contamination.

---

## Catalogue SEO, Search & SEM Architecture (CURRENT)

### Canonical page

**`ProductEdition` is the canonical, indexable customer-facing product page** — not `Product` (too broad; groups unrelated scents like Hawas Ice/Fire/Viper/For Him under one brand+name) and not `ProductVariant` (too narrow; a size/decant option is a near-duplicate of its sibling sizes, not distinct content). Notes, gender, and concentration — the content that actually differentiates one fragrance from another for search intent — live on Edition. See DEC-008.

Recommended URL shape (not yet built by any frontend in this repo): `/perfumes/<product.slug>/<edition.slug>/`, with size/decant selected on that same page. `ProductEdition.slug` is unique **per product** (`unique_edition_slug_per_product`), not globally, so the URL must be product-scoped.

### SEO metadata

`ProductEdition` carries optional overrides, exposed via `ProductEditionDetailSerializer`'s `seo` block (detail endpoint only — kept off the list/search payload):

| Field | Fallback when blank |
|---|---|
| `seo_title` | `edition.display_name()` (always includes brand) |
| `meta_description` | none — left `null` (no generated-copy engine exists) |
| `og_title` | `seo_title` → `display_name()` |
| `og_description` | `meta_description` → `null` |
| `is_indexable` | defaults `True`; lets staff noindex a thin/duplicate edition without deactivating it |

### Images

`ProductVariantImage` (FK → `ProductVariant`, max 10 per variant, enforced in `clean()` and via `TabularInline(max_num=10, validate_max=True)`):
- `role`: `primary` (canonical — product page, structured data, OG, Google Images, feeds), `secondary` (product-card hover only, never treated as canonical), `gallery` (unlimited within the 10-image cap).
- At most one `primary` and one `secondary` per variant — enforced by conditional `UniqueConstraint`s, so it holds even outside the admin.
- Deleting the primary/secondary never auto-promotes another image — a human chooses the replacement.
- `alt_text`, deterministic `sort_order`.
- The legacy single `ProductVariant.image`/`ProductEdition.image` fields are unchanged and still served; new work should use `ProductVariantImage`.

### Search

DRF `SearchFilter` (`icontains`) across `Product.name`, `Brand.name`, `ProductEdition.name` — unchanged behavior. Backed by PostgreSQL trigram GIN indexes (`pg_trgm`, via `django.contrib.postgres`) on `Product.name`, `Brand.name`, `ProductEdition.name`, `PerfumeNote.name`, so `ILIKE '%term%'` queries use an index instead of a sequential scan at scale. See DEC-009. No dedicated search engine (Elasticsearch/Algolia/etc.) — not justified at current scale, and the catalogue models carry no provider-specific coupling if one is added later.

### Availability

`ProductVariantSerializer.is_available` — `True` when any single retail-type `InventoryStock` row has positive available stock (`quantity - quantity_reserved`). Mirrors what `apps.inventory.services.reservation` actually reserves against (retail stock; decants open retail bottles on demand). It is a simple "sellable right now" signal, not a simulation of full decant-fulfillment logic — the authoritative check remains at reservation time during checkout.

### SEM / product-feed readiness

Currently available per variant via the catalogue API: title (edition display name / seo_title), brand, SKU, canonical slug pair (product+edition), primary/gallery images, price (`mrp`)/sale price (`selling_price`), `is_available`. **Gaps** (not fabricated — see the SEO/SEM foundation report for full detail): no long-form product description field, no explicit currency field (system is implicitly INR-only), no GTIN/EAN/UPC/MPN (not applicable — not invented).

### CDN / storage

`ProductVariantImage.image` uses the same storage backend as every other `ImageField` (see [File / Media Storage](#file--media-storage-current) below) — S3 + `AWS_S3_CUSTOM_DOMAIN` in production, which can point at a CDN distribution without any catalogue/media model change.

---

## Order / Checkout Architecture (CURRENT)

Checkout is a single atomic transaction:
1. Validate shipping address
2. Read cart items (fail if empty)
3. Create `Order` record
4. Bulk-create `OrderItem` records (price snapshotted at checkout time)
5. Create `ShippingAddress` record
6. Clear cart items

No payment processing occurs at checkout. Orders are created in `pending` status. Payment integration is planned separately.

### Order Number Format
`SMR-YYYYMMDD-XXXXXX` where `XXXXXX` is 6 uppercase hex characters from `secrets.token_hex(3)`.

---

## Integration Architecture

### SMS (CURRENT)

Two-setting toggleable pattern:

```
SEND_REAL_OTP=False  →  ConsoleSMSBackend (prints to stdout)
SEND_REAL_OTP=True   →  class at SMS_BACKEND env var (default: MSG91SMSBackend)
```

Switching SMS vendors requires only changing `SMS_BACKEND` in the env file. No code changes needed. All backends extend `apps.accounts.providers.base.BaseSMSBackend`.

**Current providers:**
- `apps.accounts.providers.console.ConsoleSMSBackend` — dev/testing
- `apps.accounts.providers.msg91.MSG91SMSBackend` — production (MSG91 v5 API, 5-second timeout)

### Payment Gateway — NOT YET IMPLEMENTED

Currently: Google Pay on a personal UPI ID, handled offline. No payment models or APIs exist. A toggleable provider abstraction (same pattern as SMS) is planned.

**PLANNED:** Razorpay or similar Indian payment gateway. Architecture decision deferred.

### Zoho Books — NOT YET IMPLEMENTED

**PLANNED — accounting only, not inventory.**

Zoho Books will be used as the GST-compliant accounting ledger and invoice generator. It will **not** manage inventory — Smerfume's stock types (partial bottles, decants, testers, damaged, promotional) are too domain-specific for Zoho Books' standard inventory module. Decanting in particular is a production/assembly operation that Zoho Books has no concept of.

**Confirmed architecture:**

```
Django (apps.inventory)       Zoho Books
──────────────────────────    ─────────────────────────────
Stock levels and movements →  Accounting and GST compliance
Decanting operations          Revenue, P&L, invoices
Damaged write-offs            Tax records and filings
Tester / promo management     
Returns and restocking        
Stock check at checkout       
```

When an order is placed and payment is confirmed, an async background task will push the invoice to Zoho Books via their API. Zoho sync failure must never block order creation — the sync is non-critical path.

Integration will be toggleable via a `ZOHO_SYNC_ENABLED` env flag (same pattern as `SEND_REAL_OTP`), allowing development and testing without syncing to Zoho, and safe fallback if the Zoho API is unavailable.

### Shipping — NOT YET IMPLEMENTED

No shipping provider integrated. Shipping address is captured at checkout and stored, but no label generation, tracking, or courier partner API exists.

### Email — NOT YET IMPLEMENTED

Django's email backend is not configured beyond defaults. No transactional emails are sent.

---

## File / Media Storage (CURRENT)

Toggleable via `USE_S3` env var:

| `USE_S3` | Storage |
|---|---|
| `False` | Local disk (`/media/`) |
| `True` | Amazon S3 (`django-storages` + `S3Boto3Storage`) |

S3 bucket, region, and credentials are configured via env vars. Static files always use `StaticFilesStorage` regardless of `USE_S3`.

---

## Background Processing — NOT YET IMPLEMENTED

No Celery, no Redis, no Django-Q, no background workers of any kind. All operations are synchronous within the HTTP request cycle. This includes SMS dispatch (blocks for up to 5 seconds if MSG91 is slow).

---

## Caching — NOT YET IMPLEMENTED

No caching layer. No Redis, Memcached, or Django cache framework configuration beyond defaults.

---

## Admin Interface (CURRENT)

Django Admin is configured with the **Django Unfold** theme. `User` model is registered with a customised `UserAdmin`. Other registered models are managed via standard `ModelAdmin` registration in each app's `admin.py`.

Admin available at `/admin/`. Protected by Django's standard session authentication.

---

## API Documentation (CURRENT)

`drf-spectacular` generates an OpenAPI 3.0 schema automatically from view decorators.

| URL | Purpose |
|---|---|
| `/api/schema/` | Raw OpenAPI 3.0 JSON/YAML |
| `/api/docs/` | Swagger UI (with `persistAuthorization=True`) |
| `/api/redoc/` | ReDoc |

---

## Security Architecture (CURRENT)

| Concern | Implementation |
|---|---|
| Auth | JWT Bearer tokens via simplejwt |
| Token revocation | Blacklist via `token_blacklist` app |
| OTP security | PBKDF2 hash, 3-attempt lockout, 5-min expiry |
| CORS | `django-cors-headers`; `CORS_ALLOW_ALL_ORIGINS=True` in local only |
| Rate limiting | DRF throttling scopes on OTP endpoints |
| Public IDs | UUID `public_id` exposed in all API responses; integer PKs never returned |
| Role system | `customer` / `staff` / `admin` roles with custom DRF permission classes |
| HTTPS | `SECURE_SSL_REDIRECT`, `HSTS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` configured in `production.py` |
| Secret management | All secrets in git-ignored `.env.{env}` files; no secrets in code or git history |
| `.claude/` | Git-ignored — AI session context not tracked |

---

## Deployment / Infrastructure Assumptions (CURRENT)

- WSGI deployment (Gunicorn assumed for production; `config/wsgi.py` is the entry point).
- `config/asgi.py` exists but the project does not use async views or WebSockets.
- `DJANGO_ENV` set as an OS/server environment variable to select the settings module and load the corresponding `.env.{env}` file.
- PostgreSQL running on `DB_HOST` (localhost by default, managed service in UAT/prod).
- Static files served via `collectstatic` + a web server (Nginx assumed).
- Media files on local disk (dev) or S3 (UAT/prod, via `USE_S3=True`).

---

## Known Architectural Limitations

1. **No guest checkout** — `Order.user` is a required FK. Anonymous users cannot place orders.
2. **Single shipping address per order** — No reusable address book; address is captured fresh at each checkout.
3. **`CustomerProfile` is OneToOne** — Supports only one profile per user. Multi-recipient gift support would require migration to ForeignKey.
4. **SMS dispatch is synchronous** — Blocks the HTTP response cycle for up to 5 seconds on MSG91 timeout.
5. **No inventory check at checkout** — Orders are created regardless of stock availability (inventory app is empty).
6. **No payment at checkout** — Orders are created in `pending` status with no payment captured.
7. **No order cancellation API** — Status management is admin-only via Django Admin.
8. **Cart merge (guest → authenticated) not implemented** — `Cart.session_key` field exists in the model but no merge endpoint exists.
9. **No background task queue** — All SMS dispatch, and future email/payment webhooks, run synchronously.

---

## Planned Architectural Improvements

- **Inventory app** — stock models, Zoho Books sync, stock check at checkout
- **Payments app** — toggleable payment gateway (Razorpay or similar), webhook handling
- **Shipping app** — courier partner integration, label generation, order tracking
- **Offers app** — coupon/discount engine, integration into checkout total calculation
- **Guest checkout** — nullable `Order.user` + cart merge endpoint
- **Saved address book** — `Address` model linked to `User`
- **Background task queue** — Celery + Redis for async SMS, emails, Zoho sync, payment webhooks
- **Notifications app** — transactional email and/or WhatsApp notifications
- **Reviews app** — product reviews and ratings
