# Booked Incoming Checkout (P2B) — Implementation Plan

Spec: `docs/superpowers/specs/2026-09-26-booked-incoming-inventory-design.md` §5.3, §5.4, §6, §8, §10 (P2B), §11.
Branch: `feat/purchases-booked-incoming-checkout` (from `dev` `0bda842`).
Status: APPROVED (with the corrections folded in below).

`BOOKED_INCOMING_SALES_ENABLED` stays `False`. P2B creates active incoming allocations only
when the flag is on. Tests enable it with `override_settings`. No GRN conversion, no
reallocation, no PO close/cancel guards (all P2C).

## Affected files

- `apps/inventory/services/reservation.py` — new `reserve_order_items`, release change, shared helpers.
- `apps/orders/views.py` — checkout wiring.
- `apps/orders/services.py` — pack gate.
- `apps/orders/admin.py` — "Awaiting incoming" column/filter, per-item allocation breakdown.
- `apps/catalog/views.py`, `apps/catalog/serializers.py` — `is_available` through the selector.
- Tests in the existing modules `apps.inventory.tests`, `apps.orders.tests`, `apps.catalog.tests`.
- No model changes, no migrations.

## Locking order (checkout, `reserve_order_items`)

0. Cart row (existing, view-level).
1. Phase 1 (only when `allow_incoming`): `PurchaseOrderLine` `select_for_update(of=("self",))`
   ordered by id. Candidates: direct-sale variants of the order, PO warehouse = sale warehouse,
   PO status in `BOOKABLE_STATUSES`, `confirmed_booked_quantity > 0`.
2. Re-read parent PO status after the lock (plain SELECT); drop lines whose PO is no longer bookable.
3. Phase 2: retail `InventoryStock` for direct variants and decant source variants, locked
   in `(variant_id, stock_type)` order (`get_or_create` per key).
4. `PartialBottleLot` for decant sources, by `(variant_id, opened_at)`.
5. Allocate from locked in-memory objects (shared per stock row, so a direct item and a
   decant item on the same source share one counter). Items keep order-item order.

Release does not lock the PO line: it only flips allocation rows under the reservation /
allocation locks. A later-committing release can only increase availability (conservative).
Existing debt, out of scope: `cancel_order` / `pack_order` still lock stock per allocation, item by item.

## Allocation per direct item

Physical first (`retail_unit`, `min(available, needed)`), then incoming from candidate lines by
`expected_date` asc nulls last, PO id, line id. One reservation per item; one incoming
allocation per line used. Whole incoming units enforced at the `reserve_order_items` service
boundary (no DB constraint in P2B). In-memory remainder per line so two items in one order
cannot take the same unit. Shortfall raises `InsufficientStockError` with today's message
format, where the available figure is physical + incoming (never split). Decant items are
physical-only regardless of the flag.

## Feature-flag boundaries

- Read at call time: checkout view (`allow_incoming=settings.BOOKED_INCOMING_SALES_ENABLED`),
  storefront `is_available`, `sellable_quantities` (already).
- Never read the flag: `reserve_for_order_item` (in-store, physical-only), decant allocation,
  release, consume, pack gate, admin indicators (they must work on existing rows even if the
  flag is later switched off).
- Flag OFF: no `purchases_*` queries in checkout or storefront; same allocations and error
  text as today. Differences: sorted stock locks, batched `DecantSource` lookup.

## Storefront warehouse

Flag ON: `is_available = sellable_quantities(variant_ids, default warehouse) > 0`, the same
online-sale warehouse checkout uses (`Warehouse.is_default`). Physical and incoming both come
from that one warehouse. Flag OFF: the existing prefetch path is unchanged. No multi-warehouse
expansion.

## Release / consume / pack

- `release_reservation`: physical as today; active incoming rows → `released` +
  `incoming_resolved_at`; no stock change; historical rows untouched.
- `consume_reservation`: unchanged (refuses active incoming; skips non-active incoming).
- `pack_order`: after order lock and status check, one `Exists` query; any active incoming
  allocation raises `OrderPackingError`; order stays `confirmed`, nothing consumed.
- COD verify / `confirm_reservation`: unchanged.

## Query-count risks

- Checkout flag ON: +4 constant queries (line lock, PO status re-read, received aggregate,
  allocated aggregate) plus inserts per extra allocation.
- Checkout flag OFF: batched `DecantSource` (1 instead of N).
- Storefront flag ON: +constant queries per request; map computed once per page/object and
  passed through serializer context — never per variant. Flag OFF: unchanged.
- Admin changelist: `Exists` annotation. Admin detail: prefetch with `select_related`.
- Release / pack: +1 query each.

## Tasks (test-first, one commit each)

1. Release incoming (release marks incoming released; consume still refuses active).
2. Pack gate (`OrderPackingError`, order stays confirmed; admin warning, not 500; COD verify unchanged).
3. `reserve_order_items` physical path (sorted lock SQL order, shared direct/decant counter,
   rollback, unchanged error text, no `purchases_` SQL).
4. Incoming allocation (incoming-only, mixed, multi-line, priority, exclusions, PO status
   re-read, two items competing, whole units, decant physical-only).
5. Checkout view wiring (flag ON incoming-only success; response keys equal to flag-off;
   default flag ignores booked lines; in-store physical-only with flag ON).
6. Storefront `is_available` (flag ON incoming-only True; flag OFF False; constant query
   count; flag-OFF query count unchanged; shape unchanged; default-warehouse only).
7. Admin indicators (column, filter, constant changelist queries, detail breakdown).
8. Validation and docs: canonical full suite (currently 13 modules / 641 tests), `manage.py
   check`, `makemigrations --check --dry-run`; update `CURRENT_STATE.md`, `DECISIONS.md`.

Canonical full suite:

```
python manage.py test apps.accounts.tests apps.cart.tests apps.catalog.tests apps.catalog.tests_parfumly apps.core.tests apps.inventory.tests apps.notifications.tests apps.offers.tests apps.orders.tests apps.payments.tests apps.purchases.tests apps.reviews.tests apps.shipping.tests
```

## Decided

- Deferred item 6 (`inventory.0008` rollback fails once incoming rows exist): document only in
  `DECISIONS.md` / `CURRENT_STATE.md`. No edit to 0008, no guard migration.
- Deferred item 9 (whole incoming units): enforced at the service boundary; schema-level
  integrity stays separate debt.
