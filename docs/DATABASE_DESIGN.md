# Smerfume Backend — Database Design

**Database:** PostgreSQL  
**ORM:** Django ORM  
**Default PK:** `BigAutoField` (64-bit integer, set globally in `base.py`)

---

## Base Abstractions

All domain models (except `User`) inherit from `BaseModel`.

### `BaseModel` (abstract)

Extends `TimeStampedModel`.

| Field | Type | Notes |
|---|---|---|
| `id` | BigAutoField | Primary key |
| `public_id` | UUIDField | `default=uuid.uuid4`, unique, indexed. Exposed in all API responses instead of integer PK. |
| `is_active` | BooleanField | Default `True`. Soft-delete pattern — all querysets filter `is_active=True`. |
| `created_at` | DateTimeField | `auto_now_add=True` |
| `updated_at` | DateTimeField | `auto_now=True` |

### `TimeStampedModel` (abstract)
Only `created_at` and `updated_at`. Not used directly — `BaseModel` extends it.

---

## Accounts

### `accounts_user`

Custom user model extending Django's `AbstractUser`. `AUTH_USER_MODEL = "accounts.User"`.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | Inherited from AbstractUser |
| `public_id` | UUIDField | unique, indexed | Added in migration 0004 |
| `username` | CharField(150) | unique | Required by AbstractUser; not used for login |
| `email` | EmailField | unique, nullable | `USERNAME_FIELD`; null=True to allow mobile-only accounts |
| `mobile_number` | CharField(15) | unique, indexed, nullable | Primary login identifier |
| `role` | CharField(20) | indexed | Choices: `customer` / `staff` / `admin`. Default: `customer` |
| `first_name` | CharField(150) | blank | Inherited from AbstractUser |
| `last_name` | CharField(150) | blank | Inherited from AbstractUser |
| `password` | CharField(128) | — | Set to unusable password for OTP-only users |
| `is_staff` | BooleanField | — | Inherited; `False` for customers |
| `is_active` | BooleanField | — | Inherited; separate from `BaseModel.is_active` |
| `is_superuser` | BooleanField | — | Inherited |
| `last_login` | DateTimeField | nullable | Updated on verify (`UPDATE_LAST_LOGIN=True`) |
| `date_joined` | DateTimeField | — | Inherited |

**Note:** `User` does not inherit `BaseModel` — it has its own `public_id` added directly, and `is_active` comes from `AbstractUser`. `created_at` / `updated_at` are **not** on this model.

**Computed properties (not DB columns):**
- `full_name` → `f"{first_name} {last_name}".strip()`
- `role_label` → human-readable role string

---

### `accounts_customerprofile`

Stores optional recipient/contact details for a user. Currently OneToOne (one profile per user).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | default True | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `user` | FK → accounts_user | CASCADE, unique (OneToOne) | |
| `first_name` | CharField(100) | blank | Optional |
| `last_name` | CharField(100) | blank | |
| `mobile` | CharField(15) | blank | |
| `email` | EmailField | blank | |

**Limitation:** OneToOne means one profile per user. Multi-recipient support (gift contacts) would require migrating to ForeignKey.

---

### `accounts_otpverification`

Tracks a single OTP session. One session per OTP request; sessions are not reused.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | default True | Used to invalidate old sessions |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `user` | FK → accounts_user | CASCADE | |
| `otp_hash` | CharField(128) | | PBKDF2 hash of OTP; plaintext never stored |
| `otp_session_token` | UUIDField | unique, indexed | Client-facing session reference |
| `purpose` | CharField(20) | | Choices: `login`. Default: `login` |
| `expires_at` | DateTimeField | | Set to `now + 5 minutes` on save |
| `attempt_count` | PositiveSmallIntegerField | default 0 | Incremented on wrong OTP; session locks at 3 |
| `resend_count` | PositiveSmallIntegerField | default 0 | Incremented on resend; max 3 |
| `last_resend_at` | DateTimeField | nullable | Tracks cooldown (30 seconds between resends) |
| `is_verified` | BooleanField | default False | Set True on successful verification |
| `is_new_user` | BooleanField | default False | True if the User was created in this OTP request |

**Class constants (not DB columns):**

| Constant | Value |
|---|---|
| `MAX_ATTEMPTS` | 3 |
| `MAX_RESENDS` | 3 |
| `RESEND_COOLDOWN_SECONDS` | 30 |
| `OTP_EXPIRY_MINUTES` | 5 |

**Computed properties (not DB columns):**
- `is_expired` → `now() > expires_at`
- `is_locked` → `attempt_count >= MAX_ATTEMPTS`
- `can_resend` → cooldown elapsed since `last_resend_at`
- `resend_available_in` → seconds remaining until resend allowed

---

### JWT Token Blacklist (`token_blacklist_*`)

Managed by `rest_framework_simplejwt.token_blacklist`. Two tables created automatically:
- `token_blacklist_outstandingtoken` — all issued refresh tokens
- `token_blacklist_blacklistedtoken` — revoked tokens

These tables are not custom models; they are managed by the simplejwt app.

---

## Product Catalogue

### `catalog_brand`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `name` | CharField(100) | unique | |
| `brand_category` | CharField(100) | indexed | Choices: `designer` / `middle eastern` / `niche`. Default: `designer` |
| `slug` | SlugField | unique, indexed | |

---

### `catalog_category`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `name` | CharField(100) | unique | |
| `slug` | SlugField | unique, indexed | |

---

### `catalog_perfumenote`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `name` | CharField(100) | unique, indexed | |
| `notes_category` | CharField(100) | indexed, blank | Choices: fresh / citrus / fruity / floral / sweet / spicy / woody / ambery / musky / leathery / smoky / aquatic / aromatic |

---

### `catalog_product`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `name` | CharField(255) | | e.g. "Baccarat Rouge 540" |
| `slug` | SlugField | unique, indexed | |
| `brand` | FK → catalog_brand | PROTECT | Deletion blocked if products exist |
| `category` | FK → catalog_category | PROTECT | Deletion blocked if products exist |

**Computed methods (not DB columns):**
- `display_name()` → `"{brand.name} {name}"`

---

### `catalog_productedition`

Represents a specific version of a product (concentration + gender combination).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `product` | FK → catalog_product | CASCADE | |
| `name` | CharField(100) | nullable, blank | Optional edition name (e.g. "Intense") |
| `slug` | SlugField | nullable, blank, indexed | |
| `gender` | CharField(10) | indexed | Choices: `men` / `women` / `unisex`. Default: `unisex` |
| `concentration` | CharField(20) | indexed | Choices: `edc` / `edt` / `edp` / `extrait`. Default: `edp` |
| `image` | ImageField | nullable | Upload path: `catalog/editions/` |
| `is_best_seller` | BooleanField | default False | |
| `is_new_arrival` | BooleanField | default False | |
| `notes` | M2M → catalog_perfumenote | through `EditionNote` | |

---

### `catalog_productvariant`

A purchasable SKU (specific size or decant).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `edition` | FK → catalog_productedition | CASCADE | |
| `image` | ImageField | nullable | Upload path: `catalog/variants/` |
| `size_ml` | PositiveIntegerField | | Volume in millilitres |
| `is_decant` | BooleanField | default False | True for decant/split |
| `mrp` | DecimalField(10,2) | | Maximum retail price |
| `selling_price` | DecimalField(10,2) | | Price used at checkout |

**Note:** `mrp` is stored in the DB but is **not exposed in any current API serializer**. It is present in the model for admin use only.

---

### `catalog_editionnote`

Through-table for the M2M relationship between `ProductEdition` and `PerfumeNote`.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `edition` | FK → catalog_productedition | CASCADE | |
| `note` | FK → catalog_perfumenote | CASCADE | |
| `position` | CharField(10) | | Choices: `top` / `heart` / `base` |

**Unique constraint:** `(edition, note)` — a note can only appear once per edition.

---

## Cart

### `cart_cart`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `user` | FK → accounts_user | CASCADE, nullable | OneToOne behaviour enforced by constraint |
| `session_key` | CharField(40) | nullable, indexed | For anonymous carts (not used by current API) |

**Unique constraint:** `unique_anonymous_cart_session` — `session_key` is unique when `user IS NULL`.

**Business rule:** One active cart per authenticated user (`user` is effectively unique when set). Anonymous cart support is modelled but not exposed via API.

---

### `cart_cartitem`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | Exposed in API for item-level operations |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `cart` | FK → cart_cart | CASCADE | |
| `variant` | FK → catalog_productvariant | CASCADE | |
| `quantity` | PositiveSmallIntegerField | default 1 | Validated: 1–99 |

**Unique constraint:** `(cart, variant)` — one line per variant per cart.

**Computed property:**
- `line_total` → `variant.selling_price × quantity`

---

## Orders

### `orders_order`

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `user` | FK → accounts_user | PROTECT | **Required** — guest checkout not supported |
| `order_number` | CharField(24) | unique, indexed | Format: `SMR-YYYYMMDD-XXXXXX` |
| `status` | CharField(20) | indexed | Choices: `pending` / `confirmed` / `processing` / `shipped` / `delivered` / `cancelled` / `refunded`. Default: `pending` |
| `subtotal` | DecimalField(10,2) | | Sum of line totals at checkout time |
| `discount_amount` | DecimalField(10,2) | default 0 | Always 0 currently (offers not implemented) |
| `total` | DecimalField(10,2) | | Currently equals `subtotal` |
| `customer_notes` | TextField | blank | Optional notes from customer |

**Default ordering:** `-created_at`

---

### `orders_orderitem`

Price is snapshotted at order creation time. Not linked back to current `ProductVariant` price.

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `order` | FK → orders_order | CASCADE | |
| `variant` | FK → catalog_productvariant | PROTECT | Prevents deleting variants with orders |
| `quantity` | PositiveSmallIntegerField | | |
| `unit_price` | DecimalField(10,2) | | Snapshotted from `variant.selling_price` at order time |
| `line_total` | DecimalField(10,2) | | Computed as `unit_price × quantity` on save |

---

### `orders_shippingaddress`

One shipping address per order, stored directly (no reusable address book).

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BigAutoField | PK | |
| `public_id` | UUIDField | unique, indexed | |
| `is_active` | BooleanField | | |
| `created_at` | DateTimeField | | |
| `updated_at` | DateTimeField | | |
| `order` | FK → orders_order | CASCADE, unique (OneToOne) | |
| `full_name` | CharField(200) | | |
| `mobile` | CharField(15) | | Validated: 10–15 digits |
| `address_line1` | CharField(255) | | |
| `address_line2` | CharField(255) | blank | |
| `city` | CharField(100) | | |
| `state` | CharField(2) | | 2-letter code from full Indian states list (37 options) |
| `pincode` | CharField(6) | | Validated: exactly 6 digits |
| `country` | CharField(100) | default "India" | |

---

## Empty / Stub Apps

The following apps are registered in `INSTALLED_APPS` but have no models:

| App | Status |
|---|---|
| `apps.inventory` | `models.py` is empty — no tables |
| `apps.payments` | `models.py` is empty — no tables |
| `apps.shipping` | `models.py` is empty — no tables |
| `apps.offers` | `models.py` is empty — no tables |
| `apps.reviews` | `models.py` is empty — no tables |
| `apps.notifications` | `models.py` is empty — no tables |

---

## Entity Relationship Overview

```
accounts_user
    │ (OneToOne)
    ├─── accounts_customerprofile
    │
    │ (FK, one-to-many)
    ├─── accounts_otpverification
    │
    │ (FK, OneToOne via cart_cart)
    ├─── cart_cart
    │        └─ cart_cartitem ──── catalog_productvariant
    │
    └─── orders_order
             ├─ orders_orderitem ── catalog_productvariant
             └─ orders_shippingaddress

catalog_brand ─────── catalog_product ─── catalog_productedition ─── catalog_productvariant
catalog_category ────┘                           │
catalog_perfumenote ─────────────────── catalog_editionnote (position: top/heart/base)
```

---

## Migration History

| App | Migration | Description |
|---|---|---|
| accounts | 0001_initial | Initial User + CustomerProfile |
| accounts | 0002_otpverification | OTPVerification with plaintext `code` field (superseded) |
| accounts | 0003_alter_user_email | email unique constraint |
| accounts | 0004_auth_hardening | User.public_id; rewrote OTPVerification (hashed OTP, security fields) |
| catalog | 0001_initial | All catalog models |
| catalog | 0002_productedition_image_productvariant_image | Image fields on Edition and Variant |
| cart | 0001_initial | Cart + CartItem |
| orders | 0001_initial | Order + OrderItem + ShippingAddress |
| orders | 0002_alter_shippingaddress_state | State field adjustment (untracked — present locally, not committed) |

---

## Gap Between Requirements and Current Database

The following requirements from the project brief are **not yet represented in the database**:

| Requirement | Current State | Gap |
|---|---|---|
| Inventory tracking (retail, testers, partial, decants, damaged, promo, returns) | No inventory models | Entire `inventory` app needs to be built |
| Zoho Books sync | No integration models | No Zoho sync tables or webhook logs |
| Payment recording | No payment models | No `Payment`, `PaymentAttempt`, or webhook tables |
| Shipping / fulfilment | No shipping models | No label, tracking, or courier data |
| Coupon / discount engine | No offers models | `Order.discount_amount` exists but is always 0 |
| Product reviews | No review models | Entire `reviews` app needs to be built |
| Notifications | No notification models | Entire `notifications` app needs to be built |
| Guest checkout | `Order.user` is required FK | Schema change needed to allow null |
| Reusable address book | No `Address` model | Shipping address is captured per-order only |
| Multi-recipient profiles | `CustomerProfile` is OneToOne | FK migration needed for gift-contact support |
| Stock deduction at checkout | No inventory linkage | Checkout creates order regardless of stock |
