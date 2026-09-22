# Change Decisions Log

This file records decisions made **after** the Product Bible was established. It exists to capture the gap between the original requirements document and the current implementation direction, so that the reasoning behind each deviation or clarification is traceable.

**Scope:** Only decisions that are clearly supported by information from the current development context are recorded here. Proposals that have not been approved are not treated as decisions. Where a decision's date is not known with certainty, it is noted as *Not recorded*.

---

## DEC-001 — Inventory System Architecture: Django-managed Operational Inventory, Zoho for Accounting Only

### Original Product Bible Requirement
Section 6 of the Product Bible states that Smerfume should *evaluate* whether inventory should continue to be managed directly through Zoho Books or whether Smerfume should maintain its own operational inventory system, with Zoho Books integrated primarily for billing and accounting purposes. The evaluation was explicitly deferred to the Technical Design and Integration Design phase.

### Updated Decision
Django manages all operational inventory. Zoho Books is used exclusively for accounting and billing. Decanting is explicitly understood as a production/assembly operation (transferring perfume from a source bottle into atomizers) — it has no equivalent concept in Zoho Books and will be handled entirely within the Django inventory system.

### Reason
Zoho API usage is subject to monthly limits and additional cost at higher volumes. Making customer-facing operations dependent on Zoho API availability introduced availability and cost risk that was not acceptable for Version 2.0. Keeping the operational inventory in Django keeps the critical path entirely within Smerfume's own infrastructure.

### Impact
- `apps/inventory/` — Django-managed operational inventory, stock movements, stock types, and decanting logic.
- Zoho integration — accounting sync only; non-blocking and non-critical path for customer operations.

### Status
**APPROVED** — Confirmed explicitly in architecture decisions and implemented as the design direction.

### Date
Not recorded.

### Related Documentation
- `docs/ARCHITECTURE.md`
- `docs/DATABASE_DESIGN.md`
- Memory file: `project_zoho_inventory.md`

---

## DEC-002 — SMS Provider: MSG91 Selected

### Original Product Bible Requirement
Section 10 of the Product Bible states: *"The SMS provider has not yet been finalized."* The selection criteria listed were: cost, reliability, delivery speed, OTP support, transactional SMS support, API reliability, compliance requirements in India, scalability, and ease of integration. The Product Bible also required that the SMS integration be implemented through an internal notification layer so that the provider can be changed without modifying core business logic.

### Updated Decision
MSG91 has been selected as the production SMS provider, using their v5 API. A console backend has also been created for development and testing environments. The internal provider abstraction (base class + swappable backend) has been implemented, consistent with the Product Bible's requirement for a replaceable notification layer.

### Reason
Not recorded.

### Impact
- `apps/accounts/providers/msg91.py` — MSG91 v5 API implementation.
- `apps/accounts/providers/console.py` — Console/dev backend.
- `apps/accounts/providers/base.py` — Abstract base class defining the provider interface.
- The active backend is controlled by the `SMS_BACKEND` environment variable and the `SEND_REAL_OTP` toggle.

### Status
**APPROVED** — Implemented.

### Date
Not recorded.

### Related Documentation
- `docs/ARCHITECTURE.md`

---

## DEC-003 — Authentication: OTP-only Passwordless Login via Mobile Number

### Original Product Bible Requirement
Section 4 (Customer Journey, step 7) states that OTP verification *may be used where required* to verify the customer's contact details. Section 5 describes both Guest Checkout and Registered Customer Checkout. The Product Bible does not specify the authentication mechanism in detail and does not mandate or exclude password-based login.

### Updated Decision
Passwordless OTP-based login via mobile number is the sole customer authentication mechanism. No password-based login exists. On successful OTP verification, JWT access and refresh tokens are issued. The refresh token allows the customer to maintain their session without re-entering an OTP.

### Reason
Not recorded. Consistent with the mobile-first nature of the platform and the existing OTP verification requirement for COD and customer trust.

### Impact
- `apps/accounts/views.py` — OTP request, resend, and verify endpoints.
- `apps/accounts/authentication.py` — JWT-based authentication.
- `apps/accounts/services/` — OTP generation and verification logic.

### Status
**APPROVED** — Implemented.

### Date
Not recorded.

### Related Documentation
- `docs/API_DESIGN.md`

---

## DEC-004 — Guest Checkout: UUID Cart Token + Silent User Auto-Creation

### Original Product Bible Requirement
Section 4 (step 7) and Section 5 state that Smerfume should support both Guest Checkout and Registered Customer Checkout, with OTP verification used where required. The Product Bible does not specify how guest sessions are tracked or whether guest orders require a user record.

### Updated Decision
Guest carts are identified by a UUID `cart_token` returned in the API response. At guest checkout, a User account is silently created from the mobile number provided in the shipping address. The guest can subsequently log in via OTP to access their order history. No truly anonymous or userless orders exist in the database — every order is linked to a User record.

### Reason
Ensures all orders are traceable to a customer record, supporting order history, repeat purchase identification, COD eligibility checks, and RTO/cancellation tracking — all of which are required by the Product Bible. Silent account creation avoids forcing the guest through an explicit registration flow while still satisfying the data requirements.

### Impact
- `apps/cart/` — UUID cart token management.
- `apps/orders/` — Silent user creation at checkout.

### Status
**APPROVED** — Implemented.

### Date
Not recorded.

### Related Documentation
- `docs/API_DESIGN.md`

---

## DEC-005 — Technology Stack

### Original Product Bible Requirement
Section 8 (Technical Architecture) specifies PostgreSQL, Redis, Celery, Celery Beat, and AWS S3. It does not specify the backend application framework or language.

### Updated Decision
The backend is built with:
- Python
- Django 6.0.2
- Django REST Framework 3.16.1
- PostgreSQL
- Django Unfold (admin interface)
- djangorestframework-simplejwt 5.5.0 (JWT authentication)
- drf-spectacular 0.30.0 (OpenAPI / Swagger documentation)

### Reason
Not recorded.

### Impact
Entire backend codebase.

### Status
**APPROVED** — Implemented.

### Date
Not recorded.

### Related Documentation
- `requirements.txt`
- `docs/ARCHITECTURE.md`

---

## DEC-006 — Payment Gateway: Not Yet Selected

### Original Product Bible Requirement
Section 5 lists four options under consideration: Cashfree, Razorpay, Shiprocket Payment Gateway, Zoho Payment Gateway. Section 5 also states that the backend must be built so that any third-party payment service can be toggled, with only one gateway active at a time.

### Updated Decision
No payment gateway has been selected. The backend is being built with a toggleable provider abstraction layer so that any of the candidate gateways can be integrated when a selection is made. The `apps/payments/` app exists as a stub with the abstraction in place.

### Reason
Commercials and provider evaluation are still in progress.

### Impact
- `apps/payments/` — Stub, not yet implemented beyond the abstraction structure.

### Status
**NEEDS CLARIFICATION** — Provider selection pending commercial evaluation.

### Date
Not recorded.

### Related Documentation
- `docs/ARCHITECTURE.md`

---

## DEC-007 — Zoho Inventory Sync: ZOHO_SYNC_ENABLED Toggle Pattern

### Original Product Bible Requirement
Section 6 requires that Zoho synchronization happen asynchronously wherever real-time synchronization is not required. The system must maintain synchronization status and error logs so that failed Zoho synchronization can be identified and retried. Most critically: *"Zoho should not become a dependency for the customer to complete a purchase."*

### Updated Decision
Zoho sync will be controlled by a `ZOHO_SYNC_ENABLED` environment variable flag, following the same toggleable pattern established for `SEND_REAL_OTP` and `SMS_BACKEND`. Zoho sync failure must never block order creation. Sync is a non-critical path operation.

### Reason
Consistent with the Product Bible's requirement that Zoho must not block customer-facing operations. The env-var toggle pattern is already established in the project for SMS providers, making this a natural extension of the existing configuration approach.

### Impact
- Future Zoho integration module (not yet implemented).
- Configuration pattern defined; will apply to the Zoho sync service when built.

### Status
**APPROVED as an architectural decision** — not yet implemented.

### Date
Not recorded.

### Related Documentation
- `docs/ARCHITECTURE.md`
- Memory file: `project_zoho_inventory.md`

---

## DEC-008 — In-store (Walk-in) Sales Channel

### Original Product Bible Requirement
The Product Bible describes online ordering only. It does not cover sales made in person at
Smerfume's shop or warehouse, and `docs/ARCHITECTURE.md` notes the business also sells through
Instagram and WhatsApp.

### Updated Decision
Staff can record a walk-in sale as a normal `Order` with `channel=in_store`:
- Recorded through **both** the Django admin (Orders → New in-store sale) and a staff-only API
  (`POST /api/orders/in-store/`), which share one service, `create_in_store_order`.
- **Customer mobile is required.** The customer is found or silently created from it, as in
  guest checkout (DEC-004), so every order still links to a User and the customer can later
  log in via OTP to see the purchase.
- Payment methods at the counter: **cash, UPI, card, netbanking**, with an optional manual
  reference (UTR or card slip). No gateway is involved (DEC-006).
- Unit price is always the variant's `selling_price`. Staff may apply one **order-level
  discount**, spread across lines with the existing `allocate_discount()`. Per-line price
  overrides are not supported.
- Stock is reserved and consumed immediately through the existing inventory reservation
  service (including decants), and the order is marked `delivered` at the moment of sale.
  No shipping address, shipping surcharge or fees.
- **Returns for in-store orders are handled at the counter only.** They are blocked from the
  online returns flow.

### Reason
Counter sales otherwise leave stock and revenue unrecorded, or need a manual stock adjustment
with no order behind it. Reusing `Order` and the reservation ledger keeps one stock and sales
history for all channels.

### Impact
- `apps/orders/models.py` — `Order.channel`, `Order.created_by`, `Order.payment_reference`;
  new payment choices `cash`, `upi`, `card`, `netbanking` (migration `0010`).
- `apps/orders/services.py` — `create_in_store_order`; shared `generate_order_number` and
  `resolve_customer_by_mobile`; `create_return` rejects in-store orders.
- `apps/orders/views.py`, `serializers.py`, `urls.py` — staff endpoint; `channel` and
  `payment_method` exposed on order responses.
- `apps/orders/admin.py` — "New in-store sale" page; generic "Add order" form disabled.
- Out of scope: GST invoice/Zoho sync, receipts, split payments, counter returns workflow.

### Future Requirement (recorded, not implemented)
Staff will need an admin/staff workflow to record an in-store return/refund against an
in-store order. It must reuse the existing `Return`/`ReturnItem`/`Refund` models and the
inventory ledger (return dispositions and `StockMovement`), exactly as online returns do.
**Do not build a separate refund system for in-store sales.** The customer-facing online
return endpoint stays blocked for in-store orders.

### Status
**APPROVED** — implemented (in-store returns workflow pending, see above).

### Date
2026-09-23

### Related Documentation
- `docs/API_DESIGN.md` — `POST /api/orders/in-store/`
- `docs/DATABASE_DESIGN.md` — `Order` fields

---

## Self-Consistency Check

The following issues were reviewed after drafting this file:

1. **DEC-001 vs DEC-007:** DEC-001 confirms the architecture decision (Django handles inventory, Zoho for accounting only). DEC-007 describes the sync toggle pattern. These are complementary and consistent — no conflict.

2. **DEC-002 SMS provider vs Product Bible notification layer requirement:** The Product Bible requires an internal notification layer so the provider can be swapped. The implementation uses a base class + env-var toggle, which satisfies this requirement. No conflict.

3. **DEC-003 vs DEC-004 (Guest Checkout):** The Product Bible says OTP verification *may be used where required* — it does not mandate OTP for all customers. DEC-003 makes OTP the sole login mechanism for registered customers. DEC-004 describes silent account creation for guests. The guest flow does not force OTP at cart time, only at login. These are consistent with each other and the Product Bible's intent.

4. **DEC-006 status:** Marked as NEEDS CLARIFICATION. The `apps/payments/` stub exists, but no gateway is implemented. This accurately reflects the current state. No inconsistency.

5. **DEC-007 implementation status:** Marked as approved architecturally but not yet implemented. `ZOHO_SYNC_ENABLED` does not yet appear in `config/settings/base.py`. The decision log correctly reflects this as a forward-looking architectural decision rather than a completed implementation.

6. **Dates:** No specific implementation dates were available from the git log or memory files at the level of individual decisions. All dates are recorded as *Not recorded* rather than guessing from commit history.

No inconsistencies found between decisions. No cross-decision conflicts identified.
