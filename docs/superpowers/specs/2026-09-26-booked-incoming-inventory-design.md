# Booked Incoming Inventory — Design

- Date: 2026-09-26
- Status: APPROVED
- Business decision: `docs/CHANGE_DECISIONS.md` DEC-009 (APPROVED)
- Baseline: `dev` at `fa62a84` (procurement P1A–P1E merged)
- Delivery: three branches, P2A → P2B → P2C, one spec

## 1. Purpose

Smerfume sometimes books stock from a vendor before it arrives. When the vendor has
explicitly confirmed that stock for Smerfume, the website may sell it before it
physically arrives. Customers see normal availability and are never told whether a unit
is physical or booked incoming.

Success means:

- Vendor-confirmed booked quantity can be sold online without inflating physical stock.
- Physical stock (`InventoryStock`, `StockMovement`) changes only when a GRN posts.
- Every customer unit is traceable to its backing: physical stock or a specific PO line.
- When booked stock arrives, the customer orders that were waiting for it are served
  first, automatically and atomically.
- Nothing is oversold, and no booked commitment silently disappears.

## 2. Scope

In scope:

- Direct-sale **retail** variants sold through **online checkout** (authenticated and
  guest).
- Booked quantity recorded per `PurchaseOrderLine` on issued / partially received POs.

Out of scope (stays physical-only):

- In-store sales (DEC-008). They consume stock immediately.
- Decant fulfilment. A bottle that has not arrived cannot be opened.
- Tester, damaged and promotional stock. Retail-only is a **service rule**; the schema
  does not block other stock types later.
- Customer-facing "incoming", "pre-order" or ETA fields.
- Soft or general vendor availability. Only an explicit confirmed quantity counts.

## 3. Core concepts and formulas

`PurchaseOrderLine.confirmed_booked_quantity` is the only stored booking value. Every other
quantity is derived.

`confirmed_booked_quantity` is the **cumulative** quantity the vendor has confirmed for the
whole PO line, including units already received. It is **not** the remaining incoming
quantity.

Example: ordered 10, net retail received 4, vendor confirms the remaining 6. Staff set
`confirmed_booked_quantity = 10` (4 received + 6 still to come). The formula below then
exposes `10 - 4 - 0 = 6` incoming units. Setting it to 6 would expose only 2.

For one PO line:

```
net_retail_received = posted retail GRN line quantity
                      - posted retail reversal line quantity        (P1E)

active_incoming_allocated = sum(units) of StockReservationAllocation rows
                            with allocation_type = incoming_po_line,
                                 purchase_order_line = this line,
                                 incoming_status = active

incoming_sellable = max(confirmed_booked_quantity
                        - net_retail_received
                        - active_incoming_allocated, 0)
```

A PO line contributes `incoming_sellable` only if:

- its PO status is `issued` or `partially_received`, and
- its PO warehouse matches the warehouse being sold from.

For one variant at one warehouse:

```
physical_available = InventoryStock(retail).quantity - quantity_reserved
sellable_quantity  = physical_available + sum(incoming_sellable over contributing lines)
```

Rules:

- Converted, reallocated and released incoming allocations contribute **zero**. A
  converted allocation is already covered by physical `quantity_reserved` through its
  replacement allocation. Counting it again would double-count.
- `expected_date` is allocation priority only. It is not proof of availability.
- Damaged receipt quantity never counts towards `net_retail_received`.
- A P1E reversal of surplus (unreserved) units lowers `net_retail_received`, so those
  units become incoming sellable again while the PO stays open. Staff lower
  `confirmed_booked_quantity` if the vendor will not redeliver.
- A short or partial GRN does not cancel the remaining booked quantity. Remaining
  allocations stay incoming-backed until staff explicitly lower the commitment (§7).

## 4. Data model

### 4.1 `PurchaseOrderLine` (P2A)

- `confirmed_booked_quantity` — decimal, default 0, `CHECK >= 0`.
  - Cumulative vendor-confirmed total for the line (§3), not remaining quantity.
  - `verbose_name` "Vendor-confirmed total (cumulative)"; `help_text` states that it
    includes units already received and gives the ordered 10 / received 4 / confirm 10
    example.
  - Validated in the service: `<= ordered quantity`.
  - Changed only by `set_confirmed_booked_quantity`. Not editable in admin forms.

### 4.2 `BookedQuantityChange` (P2A, new, append-only)

| Field | Notes |
|---|---|
| `purchase_order_line` | FK, PROTECT |
| `old_quantity` | decimal |
| `new_quantity` | decimal |
| `note` | text; general vendor confirmation / change note |
| `performed_by` | FK user, PROTECT |
| `created_at` | from `BaseModel` |

- One row per successful change. Never updated or deleted.
- Read-only in admin (no add, change or delete permission in the admin UI).

### 4.3 `StockReservationAllocation` extension (P2A)

New `allocation_type` value: `incoming_po_line`.

New fields:

| Field | Type | Used by |
|---|---|---|
| `purchase_order_line` | FK `purchases.PurchaseOrderLine`, PROTECT, null | incoming rows |
| `incoming_status` | char, null; `active` / `converted` / `reallocated` / `released` | incoming rows |
| `converted_by_receipt_line` | FK `purchases.GoodsReceiptLine`, PROTECT, null | `converted` |
| `replacement` | FK self, PROTECT, null | `converted`, `reallocated` |
| `split_from` | FK self, PROTECT, null | remainder rows created by a partial split |
| `incoming_resolved_at` | datetime, null | non-active incoming rows |

`units` holds the incoming quantity in pcs, as for `retail_unit`.

DB check constraint (replaces `allocation_fields_match_allocation_type`):

- `retail_unit`: `inventory_stock`, `units` set; `partial_lot`, `ml_amount`,
  `purchase_order_line`, `incoming_status` null. (Existing rule, plus the incoming fields
  must be null.)
- `partial_lot`: existing rule, plus the incoming fields must be null.
- `incoming_po_line`: `purchase_order_line`, `units`, `incoming_status` set;
  `inventory_stock`, `partial_lot`, `ml_amount`, `claimed_ml` null.

Incoming lifecycle check constraints:

- `incoming_status = active` ⇒ `converted_by_receipt_line`, `replacement`,
  `incoming_resolved_at` null.
- `incoming_status = converted` ⇒ `converted_by_receipt_line`, `replacement`,
  `incoming_resolved_at` set.
- `incoming_status = reallocated` ⇒ `replacement`, `incoming_resolved_at` set;
  `converted_by_receipt_line` null.
- `incoming_status = released` ⇒ `incoming_resolved_at` set; `replacement`,
  `converted_by_receipt_line` null.
- `units > 0` for incoming rows.
- `split_from` set ⇒ `allocation_type = incoming_po_line`. Physical rows never carry it.
- `split_from` is lineage only. It never affects current sums.

Index: partial index on `(purchase_order_line)` where
`allocation_type = 'incoming_po_line' AND incoming_status = 'active'`. It serves the
availability selector and the awaiting-incoming filter.

Existing physical rows need no data migration. Their new fields are null.

Rows are never deleted. Lifecycle changes only set status and links.

### 4.4 Settings

`BOOKED_INCOMING_SALES_ENABLED` in `config/settings/base.py`, read from env, default
`False`. While `False`, storefront availability and checkout ignore incoming stock
completely. Added in P2A (unused), read from P2B.

## 5. Services and selectors

All services are atomic and follow the global lock order (§8).

### 5.1 Selectors (P2A) — `apps/purchases/selectors.py`

- `incoming_sellable_by_line(variant_ids, warehouse)` — `{line_id: Decimal}` for
  contributing lines, using the §3 formula in one aggregated query.
- `sellable_quantity(variant, warehouse)` and a bulk form for listings — physical
  available plus incoming sellable. When the flag is off, incoming is zero.
- `active_incoming_allocated(line)` — sum of active incoming units.

These are the only authoritative availability calculations. Storefront, cart and checkout
use them from P2B on. P2A adds them with tests but no callers outside tests and admin.

### 5.2 `set_confirmed_booked_quantity(line, quantity, performed_by, note)` (P2A)

- Lock PO, then the line.
- Allowed only when PO status is `issued` or `partially_received`.
- `0 <= quantity <= line ordered quantity`.
- Lowering guard: reject when
  `max(quantity - net_retail_received, 0) < active_incoming_allocated`.
  Staff must first reallocate or release the affected allocations (§7).
- No-op when the value does not change (no history row).
- On success: update the line and append one `BookedQuantityChange`.

Admin: "Confirm booked quantity" action on PO lines (or on the PO, per line), confirmation
page with quantity and note. GET never changes state. New permission
`purchases.confirm_booked_quantity`.

### 5.3 Checkout allocation (P2B) — `apps/inventory/services/reservation.py`

New entry point used by online checkout only, for example
`reserve_order_items(order_items, warehouse, allow_incoming)`:

1. Split items: direct-sale vs decant. Decant items keep today's path, physical only.
2. **Phase 1 lock:** lock candidate `PurchaseOrderLine` rows for all direct-sale variants
   in the order, ordered by id. Candidate = matching warehouse, PO `issued` or
   `partially_received`, `confirmed_booked_quantity > 0`. Skipped when
   `allow_incoming` is false.
3. **Phase 2 lock:** lock every required `InventoryStock` row ordered by
   `(variant_id, stock_type)`. This includes decant source stock and partial lots, in
   the existing order after stock rows.
4. Recalculate availability from locked rows. For each locked PO line, re-read its parent
   PO status after the lock is held and drop the line if the PO is no longer `issued` or
   `partially_received`. This closes the race with PO close/cancel (§5.7).
5. Per direct-sale item: allocate physical first (`retail_unit`), then incoming from
   candidate lines ordered by `expected_date` ascending nulls last, then PO id, then
   line id. One item may split across several allocations under one reservation.
6. Any shortfall raises `InsufficientStockError`; the whole checkout rolls back.

`allow_incoming = settings.BOOKED_INCOMING_SALES_ENABLED`. In-store sales keep calling
`reserve_for_order_item` (physical only). The whole-order sorted stock lock also fixes the
known unsorted checkout stock-lock debt.

### 5.4 Release, confirm, consume (P2B)

- `release_reservation`: physical allocations behave as today. Active incoming
  allocations become `released` with `incoming_resolved_at`. No stock change.
- `confirm_reservation` (COD verify): unchanged.
- `consume_reservation`: raises `InvalidReservationStateError` if any active incoming
  allocation remains. Consume skips non-active incoming rows. It consumes only physical
  allocations, including physical replacements created by conversion.
- `pack_order`: before consuming, refuses with a clear error while any item has an active
  incoming allocation. The order stays `confirmed`.

### 5.5 GRN conversion (P2C) — inside `post_goods_receipt`

Within the existing atomic, idempotent posting transaction, after stock is received:

For each posted line with stock type `retail` and a PO line:

1. Select active incoming allocations for that PO line, locked, ordered by reservation
   `created_at`, then reservation id, then allocation id.
2. Convert up to the line's received quantity. For each allocation (or part of it):
   - Create a `retail_unit` allocation on the same reservation, against the
     just-received `InventoryStock`, and add its units to `quantity_reserved`.
   - Mark the incoming row `converted`, set `converted_by_receipt_line`, `replacement`
     and `incoming_resolved_at`.
   - If only part of an allocation's units can convert, apply the split in §5.5.1.
3. Surplus retail quantity stays free physical stock.
4. Damaged quantity never converts.

If any invariant fails (e.g. `quantity_reserved` would exceed `quantity`, a constraint
fails), posting raises and the **whole** GRN posting rolls back. GRN posting and conversion
are all-or-nothing.

#### 5.5.1 Partial conversion record

To keep one row per lifecycle and unambiguous sums:

- Rows are never rewritten; `units` on the original row stays `N`.
- Original incoming row with `units = N`, converting `k < N`:
  - Original row → `converted`, `replacement` = new physical row with `units = k`.
  - New incoming row, `active`, same PO line and reservation, `units = N - k`,
    `split_from` = original row.
  - The original row records the converted quantity through its replacement's `units`.
- Audit: reservation's allocations show original (converted, N), physical (k), and
  remainder incoming (active, N - k, `split_from` = original). Current backing = current
  physical allocations plus active incoming rows only: physical `k` + incoming `N - k` =
  `N`. No double count, because the converted row is excluded from all current sums.

Example:

```
A  incoming 4  → converted, replacement = P
P  physical 3  (retail_unit, created by conversion)
B  incoming 1  → active, split_from = A
```

Repeated splits chain: if `B` is later partially reallocated, its remainder `C` has
`split_from = B`. Walking `split_from` from any row reaches the original checkout
allocation.

The same split pattern applies to partial reallocation (§5.6).

### 5.6 `reallocate_incoming_allocation(allocation, performed_by)` (P2C)

For staff to move an active incoming allocation off a PO line that will not deliver.

1. Lock order per §8: candidate PO lines (including the current one), reservation,
   allocations, stock.
2. Cover the allocation's units: free physical stock first, then other contributing PO
   lines by the checkout priority.
3. Create replacement allocation(s). Mark the original `reallocated` with `replacement`.
   If covered by more than one source, the original's `replacement` points at the first,
   and further remainder rows follow the §5.5.1 split pattern.
4. If nothing covers the full quantity, raise a clear error and change nothing. The item
   stays surfaced for staff (§6). Staff may cancel the order through the existing
   `cancel_order` flow, which releases all allocations. Never cancelled automatically.

### 5.7 PO close / cancel guards (P2C)

- `close_purchase_order` and `cancel_purchase_order` reject while any active incoming
  allocation points at a line of that PO.
- Order of work, inside one transaction:
  1. Lock the `PurchaseOrder`.
  2. Lock all its `PurchaseOrderLine` rows, ordered by id.
  3. Check for active incoming allocations on those lines.
  4. Change status.
- Why this is safe: checkout must hold the PO line lock to create an incoming allocation,
  and re-checks PO status after locking (§5.3 step 4). Either checkout commits first and
  close/cancel then sees the allocation and rejects, or close/cancel commits first and
  checkout then sees the closed/cancelled PO and skips the line.
- Invariant: an active incoming allocation is never backed by a closed or cancelled PO.
- The P2A lowering guard in `set_confirmed_booked_quantity` covers commitment reductions.

### 5.8 GRN reversal (P1E, unchanged behaviour)

`reverse_goods_receipt` keeps removing only `quantity - quantity_reserved`. It never
touches converted reservations and never "unconverts" an allocation.

## 6. Admin visibility

- P2A: read-only `BookedQuantityChange` history; PO line shows, side by side and labelled
  exactly: "Ordered", "Vendor-confirmed total (cumulative)", "Received (net retail)",
  "Allocated to customer orders (awaiting arrival)" and "Incoming available to sell"
  (all derived except the confirmed total). The "Confirm booked quantity" page shows the
  current values, states that the entry is the cumulative total including received
  units, and shows the resulting "Incoming available to sell" in its confirmation text.
- P2B: orders list gains a read-only "Awaiting incoming" column and list filter; order
  detail shows per-item physical/incoming allocation breakdown (source, PO number, units,
  status).
- P2C: "Reallocate" action on active incoming allocations (confirmation page). A failed
  reallocation shows which item and quantity could not be covered. Staff find affected
  orders through the "Awaiting incoming" filter plus a per-PO admin view listing the
  active incoming allocations that depend on that PO. No separate "at risk" state is
  stored.

All admin-only. Customer APIs are unchanged in shape and content.

## 7. Shortfall handling summary

| Event | Behaviour |
|---|---|
| Partial / short GRN | Remaining allocations stay incoming. No automatic change. |
| Staff lowers confirmed booked qty | Blocked below active allocated; reallocate first. |
| PO close / cancel | Blocked while active incoming allocations exist. |
| Reallocation impossible | Error, no change, item stays surfaced. Staff decide. |
| Order cancelled | `release_reservation` releases incoming rows. |

## 8. Locking

Global lock order, used by every service:

```
PurchaseOrder
  → GoodsReceipt
  → ReceiptDiscrepancy
  → PurchaseOrderLine (by id)
  → StockReservation (by id)
  → StockReservationAllocation (by id)
  → InventoryStock (by variant_id, stock_type)
  → PartialBottleLot (existing order)
```

- Use `select_for_update(of=("self",))` when joining related rows.
- Checkout never locks `PurchaseOrder`; it locks PO lines, then stock. The relative order
  matches GRN posting, so no new cycle.
- `set_confirmed_booked_quantity`: PO → line.
- `close_purchase_order` / `cancel_purchase_order` (P2C): PO → all its lines by id, then
  check allocations (§5.7).
- `post_goods_receipt` (P2C): PO → GRN → discrepancies → PO lines → reservations →
  allocations → stock.
- `reallocate_incoming_allocation`: candidate PO lines → reservation → allocations →
  stock. It locks POs only if it needs PO status, and then first.

## 9. Error handling

- Business rule failures raise domain errors (`ValidationError`, `InsufficientStockError`,
  `InvalidReservationStateError`). Admin shows them as messages.
- Checkout keeps its existing public error response. It never reveals whether stock was
  physical or incoming.
- Unexpected errors propagate and are logged by the existing handler. Nothing is
  swallowed.

## 10. Phases and dependencies

**Safety rule:** incoming stock must not become sellable until P2C is merged, migrated and
validated end to end. `BOOKED_INCOMING_SALES_ENABLED` stays `False` through P2A and P2B
and in every environment until then. Enabling it is a separate, explicit decision.

### P2A — `feat/purchases-booked-incoming-foundation`

- `confirmed_booked_quantity`, `BookedQuantityChange`, migrations.
- `StockReservationAllocation` incoming fields, constraints, partial index, migration.
- `set_confirmed_booked_quantity` with lowering guard; admin action, permission, history.
- Selectors (§5.1); setting added, default `False`.
- No storefront, cart or checkout behaviour change. No code path creates incoming
  allocations outside tests.

### P2B — `feat/purchases-booked-incoming-checkout` (depends on P2A)

- Two-phase whole-order checkout lock and physical-then-incoming allocation.
- Storefront `is_available` and cart/checkout checks through the selector.
- Release, consume and pack gate changes; admin indicators.
- With the flag off, behaviour equals today except the sorted lock order. Tests run with
  the flag on via `override_settings`.

### P2C — `feat/purchases-booked-incoming-conversion` (depends on P2B)

- Conversion inside `post_goods_receipt`.
- `reallocate_incoming_allocation`; PO close/cancel guards; uncoverable surfacing.
- Full concurrency coverage (§11).
- After merge, migration and end-to-end validation: separate decision to enable the flag.

## 11. Testing

Unit and service tests per phase, in the existing explicit test modules
(`apps.purchases.tests`, `apps.inventory.tests`, `apps.orders.tests`).

- P2A: field and constraint validity (each allocation type and incoming status);
  `set_confirmed_booked_quantity` permissions, status rules, bounds, lowering guard, no-op,
  history row; selector formula including reversals, damaged lines, PO status and
  warehouse filters, flag off.
- P2B: physical-only, incoming-only and split allocation; priority order with null
  `expected_date`; flag off ignores incoming; in-store and decant stay physical;
  release marks incoming released; pack refuses while incoming active; COD verify
  unchanged; storefront `is_available` with incoming.
- P2C: full, partial and surplus conversion; damaged lines never convert; FIFO order;
  invariant failure rolls back the whole GRN; reversal after conversion respects
  `quantity_reserved`; reallocation to physical, to other PO, impossible case; PO
  close/cancel guards; no double count after conversion.
- Concurrency (P2C, existing concurrency test style): checkout vs GRN posting on the same
  PO line; two checkouts competing for the last incoming unit; checkout vs
  `set_confirmed_booked_quantity`; multi-item checkout vs multi-line GRN (no deadlock);
  checkout creating an incoming allocation vs `close_purchase_order` and vs
  `cancel_purchase_order` on the same PO. Invariant asserted after both finish: no active
  incoming allocation points at a line of a closed or cancelled PO.
- P2A also tests the cumulative semantics (ordered 10, received 4, confirmed 10 → 6
  incoming) and `split_from` constraints; P2C tests `split_from` chains across repeated
  partial conversion and reallocation.

Validation before each merge: full explicit test-module suite, `manage.py check`,
`makemigrations --check --dry-run`.

## 12. Risks

- **Selling stock that never ships.** Mitigated by the flag and the P2C dependency, the
  lowering and close/cancel guards, and staff surfacing.
- **Double counting after conversion.** Mitigated by counting only `active` incoming rows
  and by constraint-enforced lifecycle fields.
- **Deadlocks.** Mitigated by the single global lock order and sorted checkout locks.
- **Customer delay.** A booked unit can arrive late. The storefront does not warn the
  customer, by business decision.
