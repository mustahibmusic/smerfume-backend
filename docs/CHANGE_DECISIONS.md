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

### Amendment (DEC-010, 2026-10-20)
Django may keep, for procurement operations, each purchase order's purchase tax treatment and an operational supplier payment ledger (payments, vendor refunds and reversals). These are not accounting records. Zoho Books remains authoritative for vendor bills, accounting, GST compliance, input-tax treatment, credit notes and the final accounts-payable balance.

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

## DEC-009 — Booked Incoming Inventory

### Original Product Bible Requirement
The Product Bible lists "Incoming Stock" among inventory data points but does not define it,
and does not allow stock to be sold before it physically arrives.

### Updated Decision
Stock that a vendor has **explicitly confirmed** for Smerfume may be sold online before it
arrives:
- The confirmed quantity is recorded on the purchase order line
  (`PurchaseOrderLine.confirmed_booked_quantity`) by staff, through one service that keeps an
  append-only change history. It is the **cumulative** total the vendor has confirmed for the
  line, including units already received, not the remaining quantity (ordered 10, received 4,
  vendor confirms the other 6: record 10). Issuing a PO does not make it sellable. General or
  soft vendor availability does not count.
- Only issued / partially received POs contribute, only for the PO's warehouse.
- Sellable quantity = physical available + confirmed booked quantity not yet received and not
  yet allocated. Incoming quantities are derived, not stored.
- Customers see normal availability. They are never told whether a unit is physical or booked
  incoming.
- Booked stock never increases physical inventory and never creates `purchase_in` movements.
  It becomes physical only when the goods receipt (GRN) posts.
- Online checkout (authenticated and guest) reserves physical stock first, then confirmed
  incoming stock (earliest expected date first). One order item may be split between both.
  Every reservation allocation records its source.
- When the GRN posts, retail receipt quantity automatically serves the waiting orders first
  (oldest reservation first), in the same all-or-nothing transaction. Damaged units never
  serve customer orders.
- An order cannot be packed while any of its units still wait for incoming stock.
- A short delivery does not cancel the remaining booking. Staff cannot
  lower a confirmed quantity, close or cancel a PO below quantities that
  active customer allocations still depend on. Those allocations must
  first be explicitly resolved — preferably reallocated to physical or
  other confirmed incoming stock; if that is impossible, staff may
  decide to cancel the affected customer order through the normal
  cancellation flow. Orders are never cancelled automatically. A
  customer order can never remain backed by a closed or cancelled PO.
- Partial conversions and reallocations keep a full audit chain: the remaining part links back
  to the allocation it was split from.
- Scope: direct-sale retail bottles through online checkout only. In-store sales, decants,
  testers, damaged and promotional stock stay physical-only.
- The feature is off (`BOOKED_INCOMING_SALES_ENABLED=False`) until all three delivery phases
  are merged and validated. Enabling it is a separate decision.

### Reason
Booking stock from vendors before arrival lets Smerfume sell confirmed supply earlier without
misstating physical stock or losing the audit trail between customer orders and purchase
orders.

### Impact
- `apps/purchases` — `confirmed_booked_quantity`, `BookedQuantityChange`, booking service,
  selectors, GRN posting conversion, reallocation, PO close/cancel guards.
- `apps/inventory` — `StockReservationAllocation` gains an incoming source type and lifecycle.
- `apps/orders` / checkout — physical-then-incoming allocation, pack gate, admin indicators.
- Design: `docs/superpowers/specs/2026-09-26-booked-incoming-inventory-design.md`.

### Status
**APPROVED** — design approved; implementation pending (P2A → P2B → P2C).

### Date
2026-09-26

### Related Documentation
- `docs/superpowers/specs/2026-09-26-booked-incoming-inventory-design.md`
- `docs/INVENTORY_MANAGEMENT.md`

---

## DEC-010 — Purchase Tax Treatment and Operational Supplier Payment Ledger on Purchase Orders

### Original Product Bible Requirement
Section 6 and DEC-001 place billing and accounting in Zoho Books. Django procurement (purchase orders, goods receipts) was operational only, assumed GST pricing through optional per-line tax snapshots, and recorded no supplier payments.

### Updated Decision
1. **Purchase tax treatment per PO.** Each purchase order records `purchase_tax_treatment`: `gst_invoice` or `no_gst_invoice`. Staff choose it explicitly while the PO is a draft (no default); it is locked once the PO is issued. It is never inferred from the supplier, the payment method, or cash/UPI use.
2. **Issue validation.** `gst_invoice`: every line requires a tax rate, whether or not prices include tax. `no_gst_invoice`: prices must exclude tax (`amounts_include_tax` false), lines carry no tax rate, and no GST is added. HSN is optional for both and allowed on `no_gst_invoice` lines.
3. **Ordered gross total.** `ordered_gross_total` is the operational ordered amount: including GST for `gst_invoice`, the agreed amount for `no_gst_invoice`. It is not necessarily the final vendor-bill liability.
4. **Operational supplier payment ledger.** One append-only ledger per PO with entry types:
   - `payment`: money paid to the supplier.
   - `vendor_refund`: money the supplier actually returned (a real business event).
   - `reversal`: a Django entry was recorded incorrectly. Linked to the original `payment` or `vendor_refund`, full amount only, reason required, at most one per entry, and a reversal cannot itself be reversed.
   Each entry records amount, date, method, reference/UTR, notes, who recorded it and when. References are required for bank transfer, UPI, cheque and card; optional for cash; "other" needs a reference or meaningful notes.
5. **Derived values.** `gross_payments`, `vendor_refunds`, `reversals`, `net_paid`, `remaining_against_po` and `payment_status` (unpaid / partially paid / paid). These describe operational PO-payment coverage only; `remaining_against_po` is not the accounting balance due. Net paid can never exceed `ordered_gross_total` or fall below zero.
6. **Allowed by PO status.** New payments and vendor refunds while the PO is issued, partially received, received or closed; never while draft or cancelled. Closing a PO ends procurement, not supplier settlement. A PO cannot be cancelled while net paid is not zero.
7. **Independence.** Payment state and the PO goods lifecycle (draft / issued / partially received / received / closed / cancelled) never change each other. Inventory, goods receipt and booked-incoming logic are unchanged.
8. **Existing POs.** Purchase orders created before this decision may have no tax treatment. They are shown as unclassified and accept no payments until a Manager classifies them once (validated against their lines). No automatic backfill.
9. **Deferred.** Vendor bills, invoice tracking (invoice received / number / date) and advance payments on draft POs.
10. **Boundary.** Django records operational facts: the tax treatment selected by staff, PO commercial totals, payments, vendor refunds and reversals. Zoho Books remains authoritative for vendor bills, accounting, GST compliance, input-tax treatment, credit notes and the final accounts-payable balance.

### Reason
Some suppliers sell with a GST invoice and some without (lower price, cash or UPI), sometimes the same supplier on different purchases. Staff need correct PO totals and visibility of supplier payments without depending on Zoho for day-to-day procurement.

### Impact
- `apps/purchases`: `PurchaseOrder.purchase_tax_treatment` (nullable for existing POs only), a new append-only `PurchaseOrderPayment` ledger, payment services, admin workflows, and issue-validation changes.
- New permissions for recording payments/refunds, reversing entries and classifying existing POs.
- No change to inventory, goods receipts or booked incoming stock.
- DEC-001 amended (see above).

### Status
**APPROVED** — not yet implemented.

### Date
2026-10-20

### Related Documentation
- DEC-001 (amended)
- DEC-007 (future Zoho reconciliation)

---

## DEC-011 — Vendor Bill Reference, Payment Due Date and Overdue Status on Purchase Orders

### Original Product Bible Requirement
DEC-010 (point 9) deferred vendor bills and invoice tracking. Zoho Books is authoritative for vendor bills and the accounts-payable balance (DEC-001, DEC-010).

### Updated Decision
The purchase order list must show, per row: date, bill number, reference number, vendor, payment status (Paid / Unpaid / Partially Paid / Overdue), due date, amount and balance due.
1. **One vendor bill per PO.** `PurchaseOrder` records `bill_number`, `bill_date` and `payment_due_date`. They are editable from issue onwards (issued, partially received, received, closed), never in draft or on a cancelled PO.
2. **Due date default.** Left blank, `payment_due_date` is set to `bill_date` plus the vendor's `payment_terms_days`. Staff may enter the vendor's stated due date instead; it cannot be before the bill date, and a due date needs a bill date.
3. **Overdue is derived, never stored.** A PO is Overdue when it is not fully paid, its payment due date is before today, and it is in a payable status. Paid wins over Overdue. Status and balance come from the DEC-010 payment ledger only.
4. **Balance due** is `ordered_gross_total - net_paid` (the DEC-010 `remaining_against_po`); unknown while the PO is unclassified.
5. **Visibility.** Balance due and payment status stay behind the ledger view permission, like the other DEC-010 payment columns.
6. **Boundary unchanged.** These are operational payables figures. Zoho Books remains authoritative for vendor bills and the final accounts-payable balance.

### Reason
Staff need a day-to-day payables view (what is due, what is late) on the purchase order list without opening Zoho Books.

### Impact
- `apps/purchases`: three new nullable/blank `PurchaseOrder` fields (migration 0007), an `overdue` payment status, a `balance_due_db` annotation, new list columns, bill-number search, and a "Vendor bill" section on the PO form. The existing goods "overdue" filter (past expected date) is relabelled "delivery overdue".
- No change to inventory, goods receipts, the payment ledger or Zoho integration.
- Amends DEC-010 point 9 for bill number, bill date and due date only. Multiple bills per PO, bill amounts that differ from the PO and bill documents remain deferred.

### Status
**APPROVED** — Implemented.

### Date
2026-10-31

### Related Documentation
- DEC-001, DEC-010

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
