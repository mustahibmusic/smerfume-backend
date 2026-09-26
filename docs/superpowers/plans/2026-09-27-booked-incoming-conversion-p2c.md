# Booked Incoming Conversion (P2C) — Implementation Plan

Spec: `docs/superpowers/specs/2026-09-26-booked-incoming-inventory-design.md` §3, §5.5–§5.8, §6, §7, §8, §10 (P2C), §11.
Branch: `feat/purchases-booked-incoming-conversion` (from `dev` `a25d37b`).
Status: APPROVED (open points resolved below).

`BOOKED_INCOMING_SALES_ENABLED` stays `False`. Tests enable it with `override_settings`.
Enabling the flag is a separate, explicit decision after P2C is merged and validated end to end.

## Resolved decisions

1. Dedicated permission `inventory.reallocate_incoming_allocation` on
   `StockReservationAllocation`. Accepted: one permissions-only migration `inventory.0009`.
   `purchases.confirm_booked_quantity` is not reused.
2. Multi-source reallocation: `replacement` is the first/direct replacement. Additional
   incoming replacement pieces carry `split_from = original` as lineage, even when they point
   to a different PO line.
3. Conversion and reallocation logic live in `apps/inventory/services/reservation.py`.
   `post_goods_receipt` only orchestrates the lock phase and the convert phase.

## Affected files

- `apps/inventory/services/reservation.py` — `lock_active_incoming_for_lines`,
  `convert_incoming_allocations`, `reallocate_incoming_allocation`, shared split helper.
- `apps/purchases/services.py` — conversion orchestration in `post_goods_receipt`;
  guards in `close_purchase_order` / `cancel_purchase_order`.
- `apps/purchases/selectors.py` — active incoming allocations for a PO.
- `apps/orders/admin.py` — "Reallocate" confirmation page on active incoming allocations
  (P2B breakdown). GET never changes state.
- `apps/purchases/admin.py` — read-only list of active incoming allocations depending on the PO.
- `apps/inventory/models.py` — `Meta.permissions`; migration `inventory.0009` (permissions only).
- Tests: `apps.purchases.tests`, `apps.inventory.tests`, `apps.orders.tests`.
- `.claude/CURRENT_STATE.md`, `.claude/DECISIONS.md` (DEC-011) after validation.

## Lock order

Global: PO → GRN → discrepancies → PO lines (by id) → StockReservation (by id) →
StockReservationAllocation (by id) → InventoryStock (by variant_id, stock_type) →
PartialBottleLot. A service takes only the stages it needs, always in this relative order.

- `post_goods_receipt`: PO → GRN → discrepancies → PO lines (existing) → reservations with
  active incoming rows on those lines (by id) → those allocations (by id) → stock (existing
  `receive_purchased_stock`, sorted). The reservation/allocation lock phase runs **before**
  `receive_purchased_stock`; the convert phase runs after it.
- `close_purchase_order` / `cancel_purchase_order`: PO → all its lines by id → check active
  incoming allocations → status change.
- `reallocate_incoming_allocation`: unlocked read of line/reservation ids → candidate PO
  lines by id (same variant and warehouse, bookable, `confirmed > 0`, plus the current line)
  → unlocked re-read of PO status (as checkout) → reservation → allocation (re-verify active,
  same line) → retail stock. PO rows are not locked.
- Checkout (lines → stock) and `release_reservation` (reservation → allocations → stock) are
  unchanged and consistent with the order.
- `reverse_goods_receipt` (P1E) locks PO → GRN → PO lines → stock only. It takes **no**
  reservation or allocation locks, because it never reads for update or mutates those rows:
  it only removes `quantity - quantity_reserved` from stock.

## Conversion (inside the `post_goods_receipt` atomic block, after validation)

1. Retail receipt lines only. Damaged lines never convert.
2. Lock phase: reservations owning active incoming rows on the receipt's PO lines, by pk;
   then those allocation rows (`incoming_po_line`, `active`), by pk. Rows released
   concurrently drop out after the lock.
3. `receive_purchased_stock` unchanged.
4. Convert phase, per retail receipt line in pk order, budget = line quantity. Walk that PO
   line's allocations FIFO (reservation `created_at`, reservation id, allocation id):
   - `k = min(units, budget)`; create `retail_unit` row (`units = k`) on the same reservation
     against the retail stock; `quantity_reserved += k`.
   - Original → `converted`, `converted_by_receipt_line`, `replacement`, `incoming_resolved_at`.
   - `k < units` → remainder row: `active`, same line and reservation, `units - k`,
     `split_from = original`. It can be converted by a later retail line of the same receipt.
5. Leftover budget is surplus: free physical stock, nothing written.
6. Invariants: reservation `held`/`confirmed`; `quantity_reserved <= quantity` (no DB
   constraint exists, so the service checks). Failure raises and rolls back the whole GRN.

Partial conversion follows spec §5.5.1: rows are never rewritten; current backing counts only
active physical and active incoming rows; `split_from` chains A → B → C.

## Reallocation (`reallocate_incoming_allocation(allocation, performed_by)`)

1. Locks as above. Reject unless the allocation is active incoming and its reservation is
   `held`/`confirmed`.
2. Cover `N` units: physical first (`min(floor(available), N)`), then other contributing
   lines by checkout priority (expected_date nulls last, PO id, line id), excluding the
   current line.
3. Cover < `N` → `InsufficientStockError` naming item and uncovered quantity; no change; no
   automatic order cancellation.
4. First source = `replacement` (physical adds to `quantity_reserved`); further sources are new
   active incoming rows with `split_from = original`. Original → `reallocated`,
   `incoming_resolved_at`.
5. Whole units only.

## Close / cancel guards

After the PO lock and the line locks, raise `PurchaseOrderError` while any active incoming
allocation points at a line of the PO. Message: count, reallocate or cancel affected orders
first. Checkout re-reads PO status after its line lock (P2B), which closes the race.
Invariant: no active incoming allocation is backed by a closed or cancelled PO.

## Failure / rollback

All inside the existing `transaction.atomic`. Conversion failure rolls back the whole GRN
(status, stock, movements, discrepancies, PO status). Double posting is refused before
conversion. Failed reallocation / close / cancel change nothing; admin shows the domain error.
Unexpected errors propagate; nothing is swallowed.

## Shortfall

Short/partial GRN leaves remaining allocations active incoming. Staff use the "Awaiting
incoming" filter and the per-PO list, then reallocate or cancel the order (existing release).
No automatic customer-order cancellation.

## Reversal interaction (P1E unchanged)

- Reversal never touches allocations; converted rows stay converted; the
  `converted_by_receipt_line` link survives a later reversal.
- Removal is per stock row, not per receipt line: reversing a converted line succeeds only
  if enough free stock of the variant exists (existing behaviour, documented).
- Reversal lowers `net_retail_received`, so incoming sellable rises while the PO is open (§3).

## Tests

Service: full / partial / surplus conversion; damaged never converts; mixed retail + damaged;
FIFO across reservations; two retail receipt lines on one PO line; `split_from` chains across
repeated partial GRNs and reallocation; no double count in selectors; invariant failure rolls
back the GRN; pack after full conversion, refused after partial; reallocation to physical /
other PO / mixed / impossible / current line excluded; close and cancel guards; reversal after
conversion (exceeding free stock fails, surplus reversal succeeds, allocations unchanged);
admin GET safe and permission enforced.

Concurrency (`TransactionTestCase` + threads, flag on): checkout vs GRN on the same line; two
checkouts for the last incoming unit; checkout vs `set_confirmed_booked_quantity`; multi-item
checkout vs multi-line GRN (no deadlock); checkout vs close and vs cancel (no active allocation
on a closed/cancelled PO); GRN conversion vs order release on the same reservation; GRN
conversion vs reallocation of the same allocation. Each asserts `quantity_reserved` equals the
sum of active physical allocations.

Validation: canonical full suite (report actual counts), `manage.py check`,
`makemigrations --check --dry-run`.
