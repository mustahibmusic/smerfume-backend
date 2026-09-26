# Booked Incoming Inventory — P2A Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the schema, audit history, booking service, admin screens and availability selectors for vendor-confirmed booked stock, without changing any storefront, cart or checkout behaviour.

**Architecture:** `PurchaseOrderLine.confirmed_booked_quantity` stores the cumulative vendor-confirmed total and changes only through `apps.purchases.services.set_confirmed_booked_quantity`, which appends an immutable `BookedQuantityChange` row. `StockReservationAllocation` gains an `incoming_po_line` type and lifecycle fields enforced by DB check constraints. Read-only selectors in `apps/purchases/selectors.py` derive net retail received, active incoming allocated, incoming sellable and total sellable quantity. Nothing outside tests and admin calls the selectors in P2A.

**Tech Stack:** Python, Django, Django ORM, PostgreSQL, Django Unfold admin.

**Spec:** `docs/superpowers/specs/2026-09-26-booked-incoming-inventory-design.md` (APPROVED, commit `187b9f5`). Business decision: `docs/CHANGE_DECISIONS.md` DEC-009 (APPROVED).

**Branch:** `feat/purchases-booked-incoming-foundation` (already created from `dev` `fa62a84`; spec commit `187b9f5` on it).

## Global Constraints

- P2A only. No storefront, cart, checkout, order, pack, release or consume behaviour change for any existing data.
- No code path outside tests creates `incoming_po_line` allocations in P2A.
- `BOOKED_INCOMING_SALES_ENABLED` is added, read from env, default `False`. It stays `False`.
- `confirmed_booked_quantity` is the **cumulative** vendor-confirmed total for the line, including units already received. Never "remaining".
- `incoming_sellable = max(confirmed_booked_quantity - net_retail_received - active_incoming_allocated, 0)`.
- Only POs in `issued` or `partially_received` contribute, and only for the PO's own warehouse.
- Only `incoming_status = active` incoming rows count in any current sum.
- Allocation rows are never deleted or rewritten; `BookedQuantityChange` rows are never updated or deleted.
- Lock order: `PurchaseOrder` → `GoodsReceipt` → `ReceiptDiscrepancy` → `PurchaseOrderLine` (by id) → `StockReservation` (by id) → `StockReservationAllocation` (by id) → `InventoryStock` (by variant, stock type).
- Admin: state changes only through confirmation pages; GET never changes state.
- No new dependencies. PEP-8. Follow existing patterns in `apps/purchases/services.py` and `apps/purchases/admin.py`.
- Do not commit without the user's confirmation (project rule). Commit steps below are prepared commands; run them only when the user has approved committing for this branch.
- Run tests with explicit module paths. Bare `python manage.py test` finds nothing in this repo.

## Deviations from the spec text (approved by the user)

1. **Integer quantity.** The spec says "decimal" for `confirmed_booked_quantity`. `PurchaseOrderLine.quantity_ordered` and `GoodsReceiptLine.quantity` are `PositiveIntegerField`. This plan uses `PositiveIntegerField(default=0)` so booking, ordering and receiving use one unit type (whole bottles). Selectors return `Decimal`, because `InventoryStock` and allocation `units` are decimal.
2. **Note required.** The service rejects an empty `note`, like the existing close/cancel `reason`. The spec does not say whether it is required.
3. **Fail-fast guard (corrected).** `release_reservation` and `consume_reservation` raise `InvalidReservationStateError` only when the reservation has an **active** incoming allocation (`allocation_type = incoming_po_line` and `incoming_status = active`). Historical converted / reallocated / released incoming rows never trigger the refusal on their own; they are skipped. No production code path creates active incoming allocations in P2A; they exist only in tests. P2B replaces the guard with real handling.
4. **Query count.** The spec says the selector uses "one aggregated query". This plan uses three fixed queries (lines, received, allocated) regardless of line count. No per-line queries.

## Review Focus

1. **Reversal lowers received.** A P1E reversal of retail units reduces `net_retail_received`, so `incoming_sellable` rises again. Pinned in Task 3.
2. **Damaged receipts.** Damaged GRN lines must not count as received retail. Pinned in Task 3.
3. **Confirmed below received.** Confirmed 3 with 5 already received gives `incoming_sellable = 0`, never negative. Pinned in Task 3.
4. **Staff type the remaining quantity instead of the total.** Admin text and confirmation message must show the resulting "Incoming available to sell". Pinned in Task 5.
5. **Other warehouse / closed / cancelled / draft PO.** Must contribute zero. Pinned in Task 3.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `config/settings/base.py` | Modify | `BOOKED_INCOMING_SALES_ENABLED` toggle; nav item for history |
| `apps/purchases/models.py` | Modify | `confirmed_booked_quantity`, `BOOKABLE_STATUSES`, permission, `BookedQuantityChange` |
| `apps/purchases/migrations/0005_booked_incoming_foundation.py` | Create (generated) | Purchases schema |
| `apps/inventory/models.py` | Modify | Incoming allocation type, fields, constraints, partial index |
| `apps/inventory/migrations/0008_incoming_allocation.py` | Create (generated) | Allocation schema |
| `apps/inventory/services/reservation.py` | Modify | Fail-fast guard in release/consume |
| `apps/inventory/admin.py` | Modify | Show incoming columns on read-only allocation admin |
| `apps/purchases/selectors.py` | Modify | Booking and availability selectors |
| `apps/purchases/services.py` | Modify | `BookedQuantityError`, `set_confirmed_booked_quantity` |
| `apps/purchases/admin.py` | Modify | Inline booking columns, "Confirm booked quantity" action, read-only history admin |
| `apps/purchases/tests.py` | Modify | New P2A test classes at end of file |
| `apps/inventory/tests.py` | Modify | Allocation constraint and guard tests at end of file |

---

### Task 1: Booked quantity field, change history model, settings flag

**Files:**
- Modify: `apps/purchases/models.py` (`PurchaseOrder` constants, `PurchaseOrderLine`, new `BookedQuantityChange` after `PurchaseOrderLine`)
- Modify: `config/settings/base.py` (after the `SEND_REAL_OTP` block)
- Create: `apps/purchases/migrations/0005_booked_incoming_foundation.py` (generated)
- Test: `apps/purchases/tests.py`

**Interfaces:**
- Produces: `PurchaseOrderLine.confirmed_booked_quantity` (int, default 0); `PurchaseOrder.BOOKABLE_STATUSES`; permission codename `confirm_booked_quantity` on `PurchaseOrderLine` (checked as `purchases.confirm_booked_quantity`); `BookedQuantityChange(purchase_order_line, old_quantity, new_quantity, note, performed_by)`; `settings.BOOKED_INCOMING_SALES_ENABLED` (bool).

- [ ] **Step 1: Write the failing tests**

Append to `apps/purchases/tests.py`. Add `BookedQuantityChange` to the existing `from apps.purchases.models import (...)` block.

```python
# --- P2A: booked incoming foundation ---


class BookedQuantityModelTests(_POBase):
    def test_confirmed_booked_quantity_defaults_to_zero(self):
        line = self._line(self._po())
        self.assertEqual(line.confirmed_booked_quantity, 0)

    def test_db_rejects_negative_confirmed_booked_quantity(self):
        line = self._line(self._po())
        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchaseOrderLine.objects.filter(pk=line.pk).update(confirmed_booked_quantity=-1)

    def test_field_label_says_cumulative(self):
        field = PurchaseOrderLine._meta.get_field("confirmed_booked_quantity")
        self.assertEqual(field.verbose_name, "Vendor-confirmed total (cumulative)")
        self.assertIn("including units already received", field.help_text)

    def test_bookable_statuses(self):
        self.assertEqual(
            PurchaseOrder.BOOKABLE_STATUSES,
            (PurchaseOrder.STATUS_ISSUED, PurchaseOrder.STATUS_PARTIALLY_RECEIVED),
        )

    def test_permission_exists(self):
        self.assertTrue(
            Permission.objects.filter(
                codename="confirm_booked_quantity", content_type__app_label="purchases"
            ).exists()
        )

    def test_change_row_is_immutable(self):
        line = self._line(self._po())
        change = BookedQuantityChange.objects.create(
            purchase_order_line=line, old_quantity=0, new_quantity=4,
            note="Vendor mail 12 Sep", performed_by=self.user,
        )
        change.note = "edited"
        with self.assertRaises(ValidationError):
            change.save()
        with self.assertRaises(ValidationError):
            change.delete()
        self.assertEqual(BookedQuantityChange.objects.get(pk=change.pk).note, "Vendor mail 12 Sep")

    def test_flag_defaults_off(self):
        self.assertIs(settings.BOOKED_INCOMING_SALES_ENABLED, False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test apps.purchases.tests.BookedQuantityModelTests -v 2`
Expected: FAIL / ERROR (`ImportError: cannot import name 'BookedQuantityChange'`).

- [ ] **Step 3: Implement**

In `PurchaseOrder`, next to `RECEIVABLE_STATUSES`:

```python
    # POs whose lines may carry a vendor-confirmed booked quantity that
    # counts as incoming stock (DEC-009).
    BOOKABLE_STATUSES = (STATUS_ISSUED, STATUS_PARTIALLY_RECEIVED)
```

In `PurchaseOrderLine`, after `hsn_code`:

```python
    confirmed_booked_quantity = models.PositiveIntegerField(
        default=0,
        verbose_name="Vendor-confirmed total (cumulative)",
        help_text=(
            "Total units the vendor has confirmed for this line, including units already "
            "received. Not the remaining quantity: ordered 10, received 4, vendor confirms "
            "the other 6 -> enter 10. Changed only through 'Confirm booked quantity'."
        ),
    )
```

In `PurchaseOrderLine.Meta`, add to `constraints`:

```python
            models.CheckConstraint(
                condition=Q(confirmed_booked_quantity__gte=0),
                name="po_line_confirmed_booked_gte_0",
            ),
```

and add:

```python
        permissions = [("confirm_booked_quantity", "Can confirm booked quantity")]
```

After `PurchaseOrderLine`:

```python
class BookedQuantityChange(BaseModel):
    """Append-only history of PurchaseOrderLine.confirmed_booked_quantity.
    Written only by services.set_confirmed_booked_quantity; never updated
    or deleted."""

    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, related_name="booked_quantity_changes"
    )
    old_quantity = models.PositiveIntegerField()
    new_quantity = models.PositiveIntegerField()
    note = models.TextField(help_text="Vendor confirmation or reason for the change.")
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.purchase_order_line}: {self.old_quantity} -> {self.new_quantity}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Booked quantity history cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Booked quantity history cannot be deleted.")
```

In `config/settings/base.py`, after the `SEND_REAL_OTP` line:

```python
# BOOKED_INCOMING_SALES_ENABLED (bool env var, default False):
#   When True, storefront availability and online checkout may use
#   vendor-confirmed booked incoming stock (DEC-009). Must stay False until
#   P2C (GRN conversion and shortfall protection) is merged and validated.
BOOKED_INCOMING_SALES_ENABLED = os.getenv("BOOKED_INCOMING_SALES_ENABLED", "False") == "True"
```

Generate migration:

Run: `python manage.py makemigrations purchases --name booked_incoming_foundation`
Expected: creates `apps/purchases/migrations/0005_booked_incoming_foundation.py` with AddField, AddConstraint, AlterModelOptions (permissions), CreateModel. Open it and confirm there are no unrelated operations.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test apps.purchases.tests.BookedQuantityModelTests -v 2`
Expected: 7 tests PASS.

- [ ] **Step 5: Commit (only after user confirmation)**

```bash
git add apps/purchases/models.py apps/purchases/migrations/0005_booked_incoming_foundation.py config/settings/base.py apps/purchases/tests.py
git commit -m "feat(purchases): add confirmed booked quantity and change history"
```

---

### Task 2: Incoming allocation schema, constraints, fail-fast guard

**Files:**
- Modify: `apps/inventory/models.py` (`StockReservationAllocation`)
- Create: `apps/inventory/migrations/0008_incoming_allocation.py` (generated)
- Modify: `apps/inventory/services/reservation.py` (`release_reservation`, `consume_reservation`)
- Modify: `apps/inventory/admin.py` (`StockReservationAllocationAdmin`)
- Test: `apps/inventory/tests.py`

**Interfaces:**
- Consumes: `purchases.PurchaseOrderLine`, `purchases.GoodsReceiptLine` (existing).
- Produces: `StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE = "incoming_po_line"`; `INCOMING_ACTIVE/CONVERTED/REALLOCATED/RELEASED`; fields `purchase_order_line`, `incoming_status`, `converted_by_receipt_line`, `replacement`, `split_from`, `incoming_resolved_at`; index `alloc_active_incoming_line_idx`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/inventory/tests.py`. Reuse the file's existing imports; add any missing ones at the top:

```python
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.purchases.models import PurchaseOrder, PurchaseOrderLine
```

```python
# --- P2A: incoming allocation schema ---


class _IncomingAllocationBase(TestCase):
    """A direct-sale reservation and a PO line to hang incoming rows on."""

    def setUp(self):
        self.user = User.objects.create_user(username="incoming", password="x")
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _incoming_variant("SKU-INC-100")
        self.stock = InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=5,
        )
        supplier = Supplier.objects.create(name="Incoming Vendor")
        po = PurchaseOrder.objects.create(
            supplier=supplier, warehouse=self.warehouse, created_by=self.user,
        )
        self.po_line = PurchaseOrderLine.objects.create(
            purchase_order=po, variant=self.variant, quantity_ordered=10,
            unit_price=Decimal("100.00"),
        )
        order = Order.objects.create(
            user=self.user, order_number="INC-1", subtotal=Decimal("0"), total=Decimal("0"),
        )
        item = OrderItem.objects.create(
            order=order, variant=self.variant, quantity=2, unit_price=Decimal("4500"),
        )
        self.reservation = StockReservation.objects.create(
            order_item=item, variant=self.variant, warehouse=self.warehouse,
            purpose=StockReservation.PURPOSE_DIRECT_SALE, quantity=Decimal("2"),
        )

    def _incoming(self, **kwargs):
        values = {
            "reservation": self.reservation,
            "allocation_type": StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            "purchase_order_line": self.po_line,
            "units": Decimal("2"),
            "incoming_status": StockReservationAllocation.INCOMING_ACTIVE,
        }
        values.update(kwargs)
        return StockReservationAllocation.objects.create(**values)

    def _physical(self, **kwargs):
        values = {
            "reservation": self.reservation,
            "allocation_type": StockReservationAllocation.ALLOCATION_RETAIL_UNIT,
            "inventory_stock": self.stock,
            "units": Decimal("1"),
        }
        values.update(kwargs)
        return StockReservationAllocation.objects.create(**values)

    def _assert_rejected(self, factory, **kwargs):
        with self.assertRaises(IntegrityError), transaction.atomic():
            factory(**kwargs)


class IncomingAllocationSchemaTests(_IncomingAllocationBase):
    """DB constraints for incoming_po_line allocations (DEC-009)."""

    def test_active_incoming_row_is_valid(self):
        row = self._incoming()
        self.assertEqual(row.incoming_status, "active")

    def test_incoming_requires_po_line_and_status(self):
        self._assert_rejected(self._incoming, purchase_order_line=None)
        self._assert_rejected(self._incoming, incoming_status=None)

    def test_incoming_rejects_physical_source_fields(self):
        self._assert_rejected(self._incoming, inventory_stock=self.stock)

    def test_incoming_units_must_be_positive(self):
        self._assert_rejected(self._incoming, units=Decimal("0"))

    def test_physical_rows_reject_incoming_fields(self):
        self._assert_rejected(self._physical, purchase_order_line=self.po_line)
        self._assert_rejected(self._physical, incoming_status="active")
        source = self._incoming()
        self._assert_rejected(self._physical, split_from=source)

    def test_existing_physical_row_still_valid(self):
        self.assertIsNone(self._physical().incoming_status)

    def test_active_rejects_resolution_fields(self):
        physical = self._physical()
        self._assert_rejected(self._incoming, replacement=physical)
        self._assert_rejected(self._incoming, incoming_resolved_at=timezone.now())

    def test_converted_requires_receipt_line_and_replacement(self):
        physical = self._physical()
        self._assert_rejected(
            self._incoming, incoming_status="converted", replacement=physical,
            incoming_resolved_at=timezone.now(),
        )

    def test_reallocated_requires_replacement(self):
        self._assert_rejected(
            self._incoming, incoming_status="reallocated", incoming_resolved_at=timezone.now(),
        )
        physical = self._physical()
        row = self._incoming(
            incoming_status="reallocated", replacement=physical,
            incoming_resolved_at=timezone.now(),
        )
        self.assertEqual(row.replacement, physical)

    def test_released_rejects_links(self):
        physical = self._physical()
        self._assert_rejected(
            self._incoming, incoming_status="released", replacement=physical,
            incoming_resolved_at=timezone.now(),
        )
        row = self._incoming(incoming_status="released", incoming_resolved_at=timezone.now())
        self.assertIsNone(row.replacement)

    def test_split_from_links_remainder_to_source(self):
        source = self._incoming()
        remainder = self._incoming(units=Decimal("1"), split_from=source)
        self.assertEqual(remainder.split_from, source)
        self.assertEqual(list(source.split_remainders.all()), [remainder])


class IncomingAllocationGuardTests(_IncomingAllocationBase):
    """P2A has no incoming handling in release/consume; they must refuse
    clearly instead of crashing or skipping."""

    def test_release_refuses_reservation_with_incoming_row(self):
        self._incoming()
        with self.assertRaises(reservation_service.InvalidReservationStateError):
            reservation_service.release_reservation(self.reservation)

    def test_consume_refuses_reservation_with_incoming_row(self):
        self._incoming()
        with self.assertRaises(reservation_service.InvalidReservationStateError):
            reservation_service.consume_reservation(self.reservation)

    def test_historical_incoming_rows_do_not_block_release_or_consume(self):
        stock_before = InventoryStock.objects.get(pk=self.stock.pk).quantity
        self._physical(units=Decimal("2"))
        InventoryStock.objects.filter(pk=self.stock.pk).update(quantity_reserved=2)
        self._incoming(incoming_status="released", incoming_resolved_at=timezone.now())
        reservation_service.consume_reservation(self.reservation)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, StockReservation.STATUS_CONSUMED)
        self.assertEqual(
            InventoryStock.objects.get(pk=self.stock.pk).quantity, stock_before - 2
        )

    def test_historical_incoming_row_skipped_on_release(self):
        self._physical(units=Decimal("2"))
        InventoryStock.objects.filter(pk=self.stock.pk).update(quantity_reserved=2)
        self._incoming(incoming_status="released", incoming_resolved_at=timezone.now())
        reservation_service.release_reservation(self.reservation)
        self.assertEqual(InventoryStock.objects.get(pk=self.stock.pk).quantity_reserved, 0)
```

Add this module-level helper near the other helpers in `apps/inventory/tests.py` (adjust names if the file already has an equivalent `_variant` helper; use that instead):

```python
def _incoming_variant(sku):
    brand = Brand.objects.get_or_create(name="IncBrand", slug="incbrand")[0]
    category = Category.objects.get_or_create(name="IncCat", slug="inccat")[0]
    product = Product.objects.get_or_create(
        name="IncProduct", slug="incproduct", defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="incproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=100, selling_price="4500.00", mrp="5000.00", sku=sku,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test apps.inventory.tests.IncomingAllocationSchemaTests apps.inventory.tests.IncomingAllocationGuardTests -v 2`
Expected: ERROR (`AttributeError: ... has no attribute 'ALLOCATION_INCOMING_PO_LINE'`).

- [ ] **Step 3: Implement the model**

In `StockReservationAllocation`:

```python
    ALLOCATION_RETAIL_UNIT = "retail_unit"
    ALLOCATION_PARTIAL_LOT = "partial_lot"
    ALLOCATION_INCOMING_PO_LINE = "incoming_po_line"

    ALLOCATION_TYPE_CHOICES = (
        (ALLOCATION_RETAIL_UNIT, "Retail Unit"),
        (ALLOCATION_PARTIAL_LOT, "Partial Lot"),
        (ALLOCATION_INCOMING_PO_LINE, "Incoming PO Line"),
    )

    INCOMING_ACTIVE = "active"
    INCOMING_CONVERTED = "converted"
    INCOMING_REALLOCATED = "reallocated"
    INCOMING_RELEASED = "released"

    INCOMING_STATUS_CHOICES = (
        (INCOMING_ACTIVE, "Active"),
        (INCOMING_CONVERTED, "Converted"),
        (INCOMING_REALLOCATED, "Reallocated"),
        (INCOMING_RELEASED, "Released"),
    )
```

After `ml_amount`:

```python
    # Populated when allocation_type == incoming_po_line (DEC-009): units
    # promised from vendor-confirmed booked stock that has not arrived.
    # Only incoming_status == active rows count in any current total.
    purchase_order_line = models.ForeignKey(
        "purchases.PurchaseOrderLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="incoming_allocations",
    )
    incoming_status = models.CharField(
        max_length=20, choices=INCOMING_STATUS_CHOICES, null=True, blank=True
    )
    converted_by_receipt_line = models.ForeignKey(
        "purchases.GoodsReceiptLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="converted_allocations",
    )
    # The allocation that took over this row's units (converted/reallocated).
    replacement = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replaced"
    )
    # Lineage only: the incoming row this active remainder was split from.
    split_from = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="split_remainders"
    )
    incoming_resolved_at = models.DateTimeField(null=True, blank=True)
```

Replace `Meta` with:

```python
    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        allocation_type="retail_unit",
                        inventory_stock__isnull=False,
                        units__isnull=False,
                        partial_lot__isnull=True,
                        ml_amount__isnull=True,
                        **_NO_INCOMING_FIELDS,
                    )
                    | models.Q(
                        allocation_type="partial_lot",
                        partial_lot__isnull=False,
                        ml_amount__isnull=False,
                        inventory_stock__isnull=True,
                        units__isnull=True,
                        claimed_ml__isnull=True,
                        **_NO_INCOMING_FIELDS,
                    )
                    | models.Q(
                        allocation_type="incoming_po_line",
                        purchase_order_line__isnull=False,
                        units__isnull=False,
                        incoming_status__isnull=False,
                        inventory_stock__isnull=True,
                        partial_lot__isnull=True,
                        ml_amount__isnull=True,
                        claimed_ml__isnull=True,
                    )
                ),
                name="allocation_fields_match_allocation_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(incoming_status__isnull=True)
                    | models.Q(
                        incoming_status="active",
                        converted_by_receipt_line__isnull=True,
                        replacement__isnull=True,
                        incoming_resolved_at__isnull=True,
                    )
                    | models.Q(
                        incoming_status="converted",
                        converted_by_receipt_line__isnull=False,
                        replacement__isnull=False,
                        incoming_resolved_at__isnull=False,
                    )
                    | models.Q(
                        incoming_status="reallocated",
                        converted_by_receipt_line__isnull=True,
                        replacement__isnull=False,
                        incoming_resolved_at__isnull=False,
                    )
                    | models.Q(
                        incoming_status="released",
                        converted_by_receipt_line__isnull=True,
                        replacement__isnull=True,
                        incoming_resolved_at__isnull=False,
                    )
                ),
                name="allocation_incoming_lifecycle_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(allocation_type="incoming_po_line") | models.Q(units__gt=0)
                ),
                name="allocation_incoming_units_gt_0",
            ),
        ]
        indexes = [
            models.Index(
                fields=["purchase_order_line"],
                condition=models.Q(allocation_type="incoming_po_line", incoming_status="active"),
                name="alloc_active_incoming_line_idx",
            ),
        ]
```

Module-level, just above `class StockReservationAllocation`:

```python
# Physical allocations never carry incoming-source or lifecycle fields.
_NO_INCOMING_FIELDS = {
    "purchase_order_line__isnull": True,
    "incoming_status__isnull": True,
    "converted_by_receipt_line__isnull": True,
    "replacement__isnull": True,
    "split_from__isnull": True,
    "incoming_resolved_at__isnull": True,
}
```

Update `__str__`:

```python
    def __str__(self):
        if self.allocation_type == self.ALLOCATION_RETAIL_UNIT:
            return f"{self.units} retail unit(s) for reservation {self.reservation_id}"
        if self.allocation_type == self.ALLOCATION_INCOMING_PO_LINE:
            return (
                f"{self.units} incoming unit(s) from PO line {self.purchase_order_line_id} "
                f"[{self.incoming_status}] for reservation {self.reservation_id}"
            )
        return f"{self.ml_amount}ml from lot {self.partial_lot_id} for reservation {self.reservation_id}"
```

Generate migration:

Run: `python manage.py makemigrations inventory --name incoming_allocation`
Expected: `apps/inventory/migrations/0008_incoming_allocation.py` with AlterField (`allocation_type` choices), AddField ×6, RemoveConstraint + AddConstraint (`allocation_fields_match_allocation_type`), AddConstraint ×2, AddIndex. It must depend on a `purchases` migration. Confirm no unrelated operations.

- [ ] **Step 4: Implement the fail-fast guard**

In `apps/inventory/services/reservation.py`, add below `InvalidReservationStateError`:

```python
def _refuse_active_incoming_allocations(reservation):
    """Active incoming (booked) allocations are released/converted from
    P2B/P2C on. Until then, refuse clearly rather than mis-handle them.
    Historical (converted/reallocated/released) incoming rows are skipped."""
    Allocation = inv_models.StockReservationAllocation
    if reservation.allocations.filter(
        allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
        incoming_status=Allocation.INCOMING_ACTIVE,
    ).exists():
        raise InvalidReservationStateError(
            "This reservation has active incoming (booked) allocations, which are not "
            "handled yet."
        )
```

Call `_refuse_active_incoming_allocations(reservation)` in `release_reservation` and in `consume_reservation`, directly after the existing status check (after the row lock, before any allocation is touched).

Then make both allocation loops skip non-active historical incoming rows, so they never reach the physical code paths:

- `release_reservation`: iterate `reservation.allocations.exclude(allocation_type=ALLOCATION_INCOMING_PO_LINE).select_for_update()` instead of all allocations.
- `consume_reservation`: build `allocations` from the same exclude.

After the guard, any remaining incoming row is historical and contributes nothing.

- [ ] **Step 5: Show incoming columns in the read-only allocation admin**

In `apps/inventory/admin.py`, `StockReservationAllocationAdmin`:

```python
    list_display = (
        "reservation", "allocation_type", "units", "partial_lot", "ml_amount",
        "purchase_order_line", "incoming_status", "replacement", "split_from",
    )
    list_filter = ("allocation_type", "incoming_status")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python manage.py test apps.inventory.tests -v 1`
Expected: all inventory tests PASS, including 15 new ones. Existing reservation tests unchanged.

- [ ] **Step 7: Commit (only after user confirmation)**

```bash
git add apps/inventory/models.py apps/inventory/migrations/0008_incoming_allocation.py apps/inventory/services/reservation.py apps/inventory/admin.py apps/inventory/tests.py
git commit -m "feat(inventory): add incoming allocation schema and lifecycle constraints"
```

---

### Task 3: Booking and availability selectors

**Files:**
- Modify: `apps/purchases/selectors.py`
- Test: `apps/purchases/tests.py`

**Interfaces:**
- Consumes: Task 1 field and `BOOKABLE_STATUSES`; Task 2 allocation constants.
- Produces:
  - `received_quantities(po_line_ids, stock_type=None) -> dict[int, int]` (existing function, new optional filter; existing callers unchanged)
  - `active_incoming_allocated_quantities(po_line_ids) -> dict[int, Decimal]`
  - `incoming_sellable(po_line) -> Decimal`
  - `incoming_sellable_by_line(variant_ids, warehouse) -> dict[int, Decimal]` (only lines with a positive amount)
  - `sellable_quantities(variant_ids, warehouse) -> dict[int, Decimal]` (every requested variant id present)
  - `sellable_quantity(variant, warehouse) -> Decimal`

- [ ] **Step 1: Write the failing tests**

Append to `apps/purchases/tests.py`. Add `StockReservation, StockReservationAllocation` to the existing `apps.inventory.models` import, and `from django.test import override_settings`.

```python
class _BookingBase(_GRNBase):
    """Issued PO: 10 units of self.variant at the default warehouse."""

    def _book(self, quantity, line=None):
        line = line or self.po_line
        PurchaseOrderLine.objects.filter(pk=line.pk).update(confirmed_booked_quantity=quantity)
        line.refresh_from_db()
        return line

    def _incoming_allocation(self, units, line=None, status="active", number="BK-1"):
        order = Order.objects.create(
            user=self.user, order_number=number, subtotal=Decimal("0"), total=Decimal("0"),
        )
        item = OrderItem.objects.create(
            order=order, variant=self.variant, quantity=int(units), unit_price=Decimal("4500"),
        )
        reservation = StockReservation.objects.create(
            order_item=item, variant=self.variant, warehouse=self.warehouse,
            purpose=StockReservation.PURPOSE_DIRECT_SALE, quantity=Decimal(units),
        )
        extra = {}
        if status == "released":
            extra["incoming_resolved_at"] = timezone.now()
        return StockReservationAllocation.objects.create(
            reservation=reservation,
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            purchase_order_line=line or self.po_line,
            units=Decimal(units), incoming_status=status, **extra,
        )

    def _receive(self, retail=0, damaged=0):
        receipt = self._receipt()
        lines = {}
        if retail:
            lines["retail"] = self._grn_line(receipt, retail)
        if damaged:
            lines["damaged"] = self._grn_line(receipt, damaged, stock_type="damaged")
        self._post(receipt)
        receipt.refresh_from_db()
        return receipt, lines

    def _physical(self, quantity):
        InventoryStock.objects.update_or_create(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail",
            defaults={"quantity": quantity},
        )


class BookingSelectorTests(_BookingBase):
    def test_unbooked_line_has_no_incoming(self):
        self.assertEqual(selectors.incoming_sellable(self.po_line), Decimal("0"))

    def test_cumulative_semantics(self):
        # Ordered 10, received 4, vendor confirms the other 6 -> confirmed total 10.
        self._receive(retail=4)
        line = self._book(10)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("6"))

    def test_active_allocations_reduce_incoming(self):
        line = self._book(5)
        self._incoming_allocation(2)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("3"))

    def test_non_active_allocations_do_not_count(self):
        line = self._book(5)
        self._incoming_allocation(2, status="released")
        self.assertEqual(selectors.incoming_sellable(line), Decimal("5"))

    def test_damaged_receipts_do_not_count_as_received(self):
        self._receive(retail=2, damaged=3)
        line = self._book(10)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("8"))

    def test_reversal_lowers_net_retail_received(self):
        receipt, lines = self._receive(retail=4)
        line = self._book(10)
        services.reverse_goods_receipt(receipt, self.user, "Keyed wrong", {lines["retail"].pk: 1})
        self.assertEqual(selectors.incoming_sellable(line), Decimal("7"))

    def test_confirmed_below_received_is_zero_not_negative(self):
        self._receive(retail=5)
        line = self._book(3)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("0"))

    def test_only_bookable_po_statuses_contribute(self):
        line = self._book(5)
        for status in (PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_CLOSED,
                       PurchaseOrder.STATUS_CANCELLED, PurchaseOrder.STATUS_RECEIVED):
            PurchaseOrder.objects.filter(pk=self.po.pk).update(status=status)
            line.refresh_from_db()
            self.assertEqual(selectors.incoming_sellable(line), Decimal("0"), status)
            self.assertEqual(
                selectors.incoming_sellable_by_line([self.variant.pk], self.warehouse), {}, status
            )

    def test_other_warehouse_does_not_contribute(self):
        self._book(5)
        other = Warehouse.objects.create(name="Other WH", city="Pune")
        self.assertEqual(selectors.incoming_sellable_by_line([self.variant.pk], other), {})

    def test_by_line_lists_positive_lines_only(self):
        self._book(5)
        second = self._line(self.po, quantity_ordered=4, tax_rate=Decimal("18"))
        self.assertEqual(
            selectors.incoming_sellable_by_line([self.variant.pk], self.warehouse),
            {self.po_line.pk: Decimal("5")},
        )
        self._book(4, line=second)
        self.assertEqual(
            selectors.incoming_sellable_by_line([self.variant.pk], self.warehouse),
            {self.po_line.pk: Decimal("5"), second.pk: Decimal("4")},
        )

    def test_received_quantities_stock_type_filter(self):
        self._receive(retail=2, damaged=3)
        self.assertEqual(selectors.received_quantities([self.po_line.pk]), {self.po_line.pk: 5})
        self.assertEqual(
            selectors.received_quantities([self.po_line.pk], stock_type="retail"),
            {self.po_line.pk: 2},
        )


class SellableQuantitySelectorTests(_BookingBase):
    def test_flag_off_returns_physical_only(self):
        self._physical(3)
        self._book(5)
        self.assertEqual(selectors.sellable_quantity(self.variant, self.warehouse), Decimal("3"))

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_flag_on_adds_incoming(self):
        self._physical(3)
        self._book(5)
        self.assertEqual(selectors.sellable_quantity(self.variant, self.warehouse), Decimal("8"))

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_physical_reserved_is_excluded(self):
        self._physical(3)
        InventoryStock.objects.filter(variant=self.variant).update(quantity_reserved=2)
        self.assertEqual(selectors.sellable_quantity(self.variant, self.warehouse), Decimal("1"))

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_bulk_includes_every_requested_variant(self):
        other = _variant("SKU-PO-OTHER")
        self._book(2)
        self.assertEqual(
            selectors.sellable_quantities([self.variant.pk, other.pk], self.warehouse),
            {self.variant.pk: Decimal("2"), other.pk: Decimal("0")},
        )

    def test_only_retail_stock_counts_as_physical(self):
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse, stock_type="damaged", quantity=4,
        )
        self.assertEqual(selectors.sellable_quantity(self.variant, self.warehouse), Decimal("0"))
```

Also add `from django.utils import timezone` to the test imports if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test apps.purchases.tests.BookingSelectorTests apps.purchases.tests.SellableQuantitySelectorTests -v 2`
Expected: ERROR (`AttributeError: module 'apps.purchases.selectors' has no attribute 'incoming_sellable'`).

- [ ] **Step 3: Implement**

In `apps/purchases/selectors.py`, update the module docstring to add one line: "Booked incoming quantities (DEC-009) are derived the same way; only confirmed_booked_quantity is stored." Update imports:

```python
from decimal import Decimal

from django.conf import settings
from django.db.models import Case, F, IntegerField, Sum, When

from apps.inventory.models import InventoryStock, StockReservationAllocation

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptDiscrepancy,
)
```

Replace `received_quantities`:

```python
def received_quantities(po_line_ids, stock_type=None):
    """{po_line_id: net physically received units} for many PO lines in
    one query. Lines with nothing received are omitted. stock_type limits
    the count to one stock type (reversal lines copy the original's)."""
    signed = Case(
        When(receipt__receipt_type=GoodsReceipt.TYPE_REVERSAL, then=-F("quantity")),
        default=F("quantity"),
        output_field=IntegerField(),
    )
    lines = GoodsReceiptLine.objects.filter(
        receipt__status=GoodsReceipt.STATUS_POSTED,
        receipt__receipt_type__in=(GoodsReceipt.TYPE_STANDARD, GoodsReceipt.TYPE_REVERSAL),
        po_line_id__in=list(po_line_ids),
    )
    if stock_type is not None:
        lines = lines.filter(stock_type=stock_type)
    rows = lines.values("po_line_id").annotate(total=Sum(signed))
    return {row["po_line_id"]: row["total"] for row in rows if row["total"]}
```

Append:

```python
# --- Booked incoming inventory (DEC-009) ---

ZERO = Decimal("0")


def active_incoming_allocated_quantities(po_line_ids):
    """{po_line_id: units held by active incoming allocations}. Converted,
    reallocated and released rows never count: converted units are already
    covered by physical quantity_reserved."""
    rows = (
        StockReservationAllocation.objects.filter(
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            incoming_status=StockReservationAllocation.INCOMING_ACTIVE,
            purchase_order_line_id__in=list(po_line_ids),
        )
        .values("purchase_order_line_id")
        .annotate(total=Sum("units"))
    )
    return {row["purchase_order_line_id"]: row["total"] for row in rows}


def _incoming_amounts(lines):
    """{line_id: incoming sellable} for already-filtered bookable lines:
    max(confirmed_booked_quantity - net retail received - active allocated, 0)."""
    ids = [line.pk for line in lines]
    received = received_quantities(ids, stock_type="retail")
    allocated = active_incoming_allocated_quantities(ids)
    return {
        line.pk: max(
            Decimal(line.confirmed_booked_quantity)
            - received.get(line.pk, 0)
            - allocated.get(line.pk, ZERO),
            ZERO,
        )
        for line in lines
    }


def _bookable_lines(variant_ids, warehouse):
    return list(
        PurchaseOrderLine.objects.filter(
            variant_id__in=list(variant_ids),
            purchase_order__warehouse=warehouse,
            purchase_order__status__in=PurchaseOrder.BOOKABLE_STATUSES,
            confirmed_booked_quantity__gt=0,
        )
    )


def incoming_sellable(po_line):
    """Incoming units of one PO line still available to sell. Zero unless
    its PO is issued or partially received."""
    if po_line.purchase_order.status not in PurchaseOrder.BOOKABLE_STATUSES:
        return ZERO
    return _incoming_amounts([po_line])[po_line.pk]


def incoming_sellable_by_line(variant_ids, warehouse):
    """{po_line_id: incoming sellable} for bookable lines of these variants
    at this warehouse. Lines with nothing to sell are omitted."""
    amounts = _incoming_amounts(_bookable_lines(variant_ids, warehouse))
    return {line_id: amount for line_id, amount in amounts.items() if amount > 0}


def sellable_quantities(variant_ids, warehouse):
    """{variant_id: physical retail available + incoming sellable}. The one
    authoritative availability figure (DEC-009). Incoming counts only when
    settings.BOOKED_INCOMING_SALES_ENABLED is True."""
    variant_ids = list(variant_ids)
    result = {variant_id: ZERO for variant_id in variant_ids}
    for stock in InventoryStock.objects.filter(
        variant_id__in=variant_ids,
        warehouse=warehouse,
        stock_type=InventoryStock.STOCK_TYPE_RETAIL,
    ):
        result[stock.variant_id] += max(stock.available, ZERO)
    if settings.BOOKED_INCOMING_SALES_ENABLED:
        lines = _bookable_lines(variant_ids, warehouse)
        amounts = _incoming_amounts(lines)
        for line in lines:
            result[line.variant_id] += amounts[line.pk]
    return result


def sellable_quantity(variant, warehouse):
    return sellable_quantities([variant.pk], warehouse)[variant.pk]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test apps.purchases.tests.BookingSelectorTests apps.purchases.tests.SellableQuantitySelectorTests -v 2`
Expected: 16 tests PASS.

Run: `python manage.py test apps.purchases.tests -v 1`
Expected: all purchases tests PASS (existing `received_quantities` callers unaffected).

- [ ] **Step 5: Commit (only after user confirmation)**

```bash
git add apps/purchases/selectors.py apps/purchases/tests.py
git commit -m "feat(purchases): add booked incoming availability selectors"
```

---

### Task 4: `set_confirmed_booked_quantity` service with lowering guard

**Files:**
- Modify: `apps/purchases/services.py`
- Test: `apps/purchases/tests.py`

**Interfaces:**
- Consumes: Task 1 model/field/`BOOKABLE_STATUSES`; Task 3 `received_quantities(..., stock_type="retail")`, `active_incoming_allocated_quantities`.
- Produces: `class BookedQuantityError(Exception)`; `set_confirmed_booked_quantity(line, quantity, performed_by, note) -> tuple[PurchaseOrderLine, BookedQuantityChange | None]` (`None` when unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `apps/purchases/tests.py`:

```python
class SetConfirmedBookedQuantityTests(_BookingBase):
    def _set(self, quantity, note="Vendor confirmed by mail", line=None):
        return services.set_confirmed_booked_quantity(
            line or self.po_line, quantity, self.user, note
        )

    def test_sets_value_and_appends_history(self):
        line, change = self._set(6)
        self.assertEqual(line.confirmed_booked_quantity, 6)
        self.assertEqual(
            (change.old_quantity, change.new_quantity, change.note, change.performed_by),
            (0, 6, "Vendor confirmed by mail", self.user),
        )
        line, change = self._set(8, note="Two more confirmed")
        self.assertEqual((change.old_quantity, change.new_quantity), (6, 8))
        self.assertEqual(BookedQuantityChange.objects.filter(purchase_order_line=line).count(), 2)

    def test_unchanged_value_writes_no_history(self):
        self._set(6)
        line, change = self._set(6)
        self.assertIsNone(change)
        self.assertEqual(BookedQuantityChange.objects.count(), 1)

    def test_allowed_on_partially_received_po(self):
        self._receive(retail=4)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, PurchaseOrder.STATUS_PARTIALLY_RECEIVED)
        line, _ = self._set(10)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("6"))

    def test_rejected_on_non_bookable_po(self):
        for status in (PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_RECEIVED,
                       PurchaseOrder.STATUS_CLOSED, PurchaseOrder.STATUS_CANCELLED):
            PurchaseOrder.objects.filter(pk=self.po.pk).update(status=status)
            with self.assertRaises(services.BookedQuantityError, msg=status):
                self._set(3)
        self.assertEqual(BookedQuantityChange.objects.count(), 0)

    def test_bounds(self):
        with self.assertRaises(services.BookedQuantityError):
            self._set(-1)
        with self.assertRaises(services.BookedQuantityError):
            self._set(11)  # ordered 10
        line, _ = self._set(10)
        self.assertEqual(line.confirmed_booked_quantity, 10)

    def test_note_required(self):
        with self.assertRaises(services.BookedQuantityError):
            self._set(3, note="   ")

    def test_lowering_below_active_allocations_is_blocked(self):
        self._set(5)
        self._incoming_allocation(3)
        with self.assertRaises(services.BookedQuantityError):
            self._set(2)
        self.po_line.refresh_from_db()
        self.assertEqual(self.po_line.confirmed_booked_quantity, 5)
        line, _ = self._set(3)  # exactly covers the 3 allocated units
        self.assertEqual(line.confirmed_booked_quantity, 3)

    def test_lowering_guard_accounts_for_received(self):
        # Received 4, confirmed 10, 3 allocated: lowest allowed is 4 + 3 = 7.
        self._receive(retail=4)
        self._set(10)
        self._incoming_allocation(3)
        with self.assertRaises(services.BookedQuantityError):
            self._set(6)
        line, _ = self._set(7)
        self.assertEqual(selectors.incoming_sellable(line), Decimal("0"))

    def test_released_allocations_do_not_block_lowering(self):
        self._set(5)
        self._incoming_allocation(3, status="released")
        line, _ = self._set(0)
        self.assertEqual(line.confirmed_booked_quantity, 0)

    def test_does_not_touch_inventory(self):
        self._physical(2)
        before = list(InventoryStock.objects.values_list("quantity", "quantity_reserved"))
        movements = StockMovement.objects.count()
        self._set(5)
        self.assertEqual(
            list(InventoryStock.objects.values_list("quantity", "quantity_reserved")), before
        )
        self.assertEqual(StockMovement.objects.count(), movements)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test apps.purchases.tests.SetConfirmedBookedQuantityTests -v 2`
Expected: ERROR (`AttributeError: module 'apps.purchases.services' has no attribute 'set_confirmed_booked_quantity'`).

- [ ] **Step 3: Implement**

In `apps/purchases/services.py`, update the module docstring lock-order paragraph to the full order:

```
Lock order, always: PurchaseOrder -> GoodsReceipt -> ReceiptDiscrepancy
-> PurchaseOrderLine (by id) -> StockReservation (by id)
-> StockReservationAllocation (by id) -> InventoryStock (by variant, stock type).
```

Add `BookedQuantityChange` to the `.models` import. Append:

```python
# --- Booked incoming inventory (DEC-009) ---


class BookedQuantityError(Exception):
    """Raised when a booked quantity change is not allowed."""


@transaction.atomic
def set_confirmed_booked_quantity(line, quantity, performed_by, note):
    """Set the cumulative vendor-confirmed total for a PO line and append
    one BookedQuantityChange. The total includes units already received;
    it is not the remaining quantity. Returns (line, change); change is
    None when the value is unchanged.

    Lowering is refused while the remaining booking would no longer cover
    the active incoming allocations of customer orders: those must be
    reallocated or released first."""
    note = (note or "").strip()
    if not note:
        raise BookedQuantityError("A note about the vendor confirmation is required.")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise BookedQuantityError("Booked quantity must be a whole number.")

    po = _lock(line.purchase_order)
    line = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk)
    if po.status not in PurchaseOrder.BOOKABLE_STATUSES:
        raise BookedQuantityError(
            f"{po.po_number} is {po.get_status_display()}. Booked quantities can be set "
            "only on issued or partially received purchase orders."
        )
    if quantity < 0 or quantity > line.quantity_ordered:
        raise BookedQuantityError(
            f"Booked quantity must be between 0 and the ordered quantity ({line.quantity_ordered})."
        )
    if quantity == line.confirmed_booked_quantity:
        return line, None

    received = selectors.received_quantities([line.pk], stock_type="retail").get(line.pk, 0)
    allocated = selectors.active_incoming_allocated_quantities([line.pk]).get(line.pk, 0)
    if max(quantity - received, 0) < allocated:
        raise BookedQuantityError(
            f"Customer orders are waiting for {allocated} unit(s) of this line. With "
            f"{received} already received, the confirmed total cannot go below "
            f"{received + allocated}. Reallocate or cancel those orders first."
        )

    change = BookedQuantityChange.objects.create(
        purchase_order_line=line,
        old_quantity=line.confirmed_booked_quantity,
        new_quantity=quantity,
        note=note,
        performed_by=performed_by,
    )
    line.confirmed_booked_quantity = quantity
    line.save(update_fields=["confirmed_booked_quantity", "updated_at"])
    return line, change
```

Note: `received + allocated` may be a `Decimal`; the f-string prints it as e.g. `7` or `7.00`. If `7.00` appears, format with `{received + allocated:g}` — keep whichever the test output shows cleanly; the tests do not assert the message text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test apps.purchases.tests.SetConfirmedBookedQuantityTests -v 2`
Expected: 10 tests PASS.

- [ ] **Step 5: Commit (only after user confirmation)**

```bash
git add apps/purchases/services.py apps/purchases/tests.py
git commit -m "feat(purchases): add set_confirmed_booked_quantity service"
```

---

### Task 5: Admin — booking columns, confirm action, read-only history, navigation

**Files:**
- Modify: `apps/purchases/admin.py`
- Modify: `config/settings/base.py` (Purchases sidebar group)
- Test: `apps/purchases/tests.py` (new class; update `test_purchase_orders_in_navigation`)

**Interfaces:**
- Consumes: Task 3 selectors, Task 4 service and `BookedQuantityError`, Task 1 permission.
- Produces: admin URL name `admin:purchases_purchaseorder_confirm_booked`; `BookedQuantityChangeAdmin` registered; nav item "Booked Quantity History".

- [ ] **Step 1: Write the failing tests**

In `PurchaseOrderAdminTests.test_purchase_orders_in_navigation`, change the expected list to:

```python
            ["Vendors", "Purchase Orders", "Goods Receipts", "Receipt Discrepancies",
             "Booked Quantity History"],
```

Append:

```python
class BookedQuantityAdminTests(_BookingBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("admin:purchases_purchaseorder_confirm_booked", args=[self.po.pk])

    def test_get_shows_cumulative_explanation_and_changes_nothing(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cumulative")
        self.assertContains(response, "including units already received")
        self.po_line.refresh_from_db()
        self.assertEqual(self.po_line.confirmed_booked_quantity, 0)
        self.assertEqual(BookedQuantityChange.objects.count(), 0)

    def test_post_sets_quantity_and_reports_incoming_available(self):
        self._receive(retail=4)
        response = self.client.post(
            self.url,
            {"line": self.po_line.pk, "quantity": 10, "note": "Vendor confirmed rest"},
            follow=True,
        )
        self.po_line.refresh_from_db()
        self.assertEqual(self.po_line.confirmed_booked_quantity, 10)
        self.assertContains(response, "Incoming available to sell: 6")

    def test_post_error_is_shown_and_nothing_changes(self):
        response = self.client.post(
            self.url, {"line": self.po_line.pk, "quantity": 11, "note": "x"}, follow=True,
        )
        self.assertContains(response, "between 0 and the ordered quantity")
        self.assertEqual(BookedQuantityChange.objects.count(), 0)

    def test_permission_required(self):
        staff = User.objects.create_user(username="nobook", password="x", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_purchaseorder"))
        self.client.force_login(staff)
        self.client.post(self.url, {"line": self.po_line.pk, "quantity": 3, "note": "x"})
        self.po_line.refresh_from_db()
        self.assertEqual(self.po_line.confirmed_booked_quantity, 0)

    def test_action_hidden_on_draft_po(self):
        model_admin = django_admin.site._registry[PurchaseOrder]
        request = RequestFactory().get("/admin/")
        request.user = self.user
        draft = self._po()
        self.assertFalse(model_admin.has_confirm_booked_permission(request, draft.pk))
        self.assertTrue(model_admin.has_confirm_booked_permission(request, self.po.pk))

    def test_inline_shows_booking_columns(self):
        self._book(10)
        inline = PurchaseOrderLineInline(PurchaseOrder, django_admin.site)
        self.assertEqual(inline.confirmed_booked(self.po_line), 10)
        self.assertEqual(inline.incoming_available(self.po_line), Decimal("10"))
        self.assertEqual(inline.allocated_awaiting(self.po_line), Decimal("0"))
        self.assertEqual(inline.received_net_retail(self.po_line), 0)
        self.assertNotIn("confirmed_booked_quantity", inline.fields)

    def test_history_admin_is_read_only(self):
        model_admin = django_admin.site._registry[BookedQuantityChange]
        request = RequestFactory().get("/admin/")
        request.user = self.user
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(
            self.client.get(reverse("admin:purchases_bookedquantitychange_changelist")).status_code,
            200,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python manage.py test apps.purchases.tests.BookedQuantityAdminTests apps.purchases.tests.PurchaseOrderAdminTests -v 2`
Expected: ERROR (`NoReverseMatch: 'purchaseorder_confirm_booked'`) and navigation test FAIL.

- [ ] **Step 3: Implement inline columns**

In `PurchaseOrderLineInline`, extend `fields` and `readonly_fields` (the stored field is **not** added; the display method shows it read-only):

```python
    fields = (
        "variant", "quantity_ordered", "unit_price", "line_discount_amount",
        "tax_rate", "hsn_code", "gross_line_amount", "taxable_value",
        "effective_unit_cost_ex_tax", "received", "outstanding",
        "confirmed_booked", "received_net_retail", "allocated_awaiting", "incoming_available",
    )
    readonly_fields = (
        "gross_line_amount", "taxable_value", "effective_unit_cost_ex_tax",
        "received", "outstanding",
        "confirmed_booked", "received_net_retail", "allocated_awaiting", "incoming_available",
    )
```

Add display methods after `outstanding`:

```python
    @admin.display(description="Vendor-confirmed total (cumulative)")
    def confirmed_booked(self, line):
        return line.confirmed_booked_quantity if line.pk else "-"

    @admin.display(description="Received (net retail)")
    def received_net_retail(self, line):
        if not line.pk:
            return "-"
        return selectors.received_quantities([line.pk], stock_type="retail").get(line.pk, 0)

    @admin.display(description="Allocated to customer orders (awaiting arrival)")
    def allocated_awaiting(self, line):
        if not line.pk:
            return "-"
        return selectors.active_incoming_allocated_quantities([line.pk]).get(
            line.pk, selectors.ZERO
        )

    @admin.display(description="Incoming available to sell")
    def incoming_available(self, line):
        return selectors.incoming_sellable(line) if line.pk else "-"
```

- [ ] **Step 4: Implement the confirm action**

Add `BookedQuantityChange` to the `.models` import. Add the form next to the other forms:

```python
class ConfirmBookedQuantityForm(forms.Form):
    line = forms.ModelChoiceField(queryset=PurchaseOrderLine.objects.none(), label="PO line")
    quantity = forms.IntegerField(
        min_value=0,
        label="Vendor-confirmed total (cumulative)",
        help_text=(
            "Total units the vendor has confirmed for this line, including units already "
            "received. Ordered 10, received 4, vendor confirms the other 6: enter 10."
        ),
    )
    note = forms.CharField(widget=UnfoldAdminTextareaWidget, label="Vendor confirmation note")

    def __init__(self, *args, po, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["line"].queryset = po.lines.select_related("variant")
```

In `PurchaseOrderAdmin`, add `"confirm_booked"` to `actions_detail` after `"receive_goods"`:

```python
    actions_detail = ["issue_po", "receive_goods", "confirm_booked", "close_po", "cancel_po"]
```

Permission hook, next to the others:

```python
    def has_confirm_booked_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.confirm_booked_quantity") and self._po_in_status(
            object_id, list(PurchaseOrder.BOOKABLE_STATUSES)
        )
```

Action (after `receive_goods`):

```python
    @action(
        description="Confirm booked quantity", url_path="confirm-booked",
        permissions=["confirm_booked"], icon="event_available",
    )
    def confirm_booked(self, request, object_id):
        """GET shows each line's booking figures and a form; only POST
        changes the vendor-confirmed total, through the service."""
        po = get_object_or_404(PurchaseOrder, pk=object_id)
        form = ConfirmBookedQuantityForm(request.POST or None, po=po)
        if request.method == "POST" and form.is_valid():
            try:
                line, _ = po_services.set_confirmed_booked_quantity(
                    form.cleaned_data["line"], form.cleaned_data["quantity"],
                    request.user, form.cleaned_data["note"],
                )
            except po_services.BookedQuantityError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(
                    request,
                    f"{line.variant}: vendor-confirmed total is {line.confirmed_booked_quantity}. "
                    f"Incoming available to sell: {selectors.incoming_sellable(line):g}.",
                    level=messages.SUCCESS,
                )
            return redirect(self._change_url(po))
        rows = [
            f"{line.variant} — ordered {line.quantity_ordered}, vendor-confirmed total "
            f"{line.confirmed_booked_quantity}, received (net retail) "
            f"{selectors.received_quantities([line.pk], stock_type='retail').get(line.pk, 0)}, "
            f"incoming available to sell {selectors.incoming_sellable(line):g}"
            for line in po.lines.select_related("variant")
        ]
        return self._action_page(
            request, po, f"Confirm booked quantity for {po.po_number}", form,
            "Enter the cumulative total the vendor has confirmed for one line, including "
            "units already received. It is not the remaining quantity. Confirmed units "
            "not yet received can be sold before they arrive once booked selling is "
            "switched on. Physical stock is not changed.",
            "Save confirmed total", "bg-primary-600", rows=rows,
        )
```

Extend `_action_page` with an optional `rows` argument (the template already renders `rows`):

```python
    def _action_page(self, request, po, title, form, message, button_label, button_class,
                     rows=None):
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.model._meta,
            "form": form,
            "message": message,
            "rows": rows or [],
            "button_label": button_label,
            "button_class": button_class,
            "back_url": self._change_url(po),
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)
```

- [ ] **Step 5: Implement the read-only history admin**

At the end of `apps/purchases/admin.py`:

```python
@admin.register(BookedQuantityChange)
class BookedQuantityChangeAdmin(ModelAdmin):
    """Append-only history of vendor-confirmed booked totals. Rows are
    written only by services.set_confirmed_booked_quantity."""

    list_display = (
        "purchase_order", "purchase_order_line", "old_quantity", "new_quantity",
        "performed_by", "created_at",
    )
    list_filter = ("purchase_order_line__purchase_order__supplier",)
    search_fields = (
        "purchase_order_line__purchase_order__po_number", "purchase_order_line__variant__sku",
        "note",
    )
    list_select_related = (
        "purchase_order_line__purchase_order", "purchase_order_line__variant", "performed_by",
    )
    fields = (
        "purchase_order_line", "old_quantity", "new_quantity", "note", "performed_by",
        "created_at",
    )
    readonly_fields = fields

    @admin.display(description="Purchase order")
    def purchase_order(self, obj):
        return obj.purchase_order_line.purchase_order.po_number

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
```

If `BaseModel` names its timestamp differently than `created_at`, use that name (check `apps/core/models.py` `TimeStampedModel`).

- [ ] **Step 6: Add the navigation item**

In `config/settings/base.py`, Purchases group, after "Receipt Discrepancies":

```python
                    _nav_item(
                        "Booked Quantity History", "history",
                        "admin:purchases_bookedquantitychange_changelist",
                        "purchases.view_bookedquantitychange",
                    ),
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python manage.py test apps.purchases.tests.BookedQuantityAdminTests apps.purchases.tests.PurchaseOrderAdminTests -v 2`
Expected: all PASS.

- [ ] **Step 8: Commit (only after user confirmation)**

```bash
git add apps/purchases/admin.py config/settings/base.py apps/purchases/tests.py
git commit -m "feat(purchases): add booked quantity admin action and history"
```

---

### Task 6: Full validation and project state

**Files:**
- Modify: `.claude/CURRENT_STATE.md` (local, git-ignored — never force-add)
- Modify: `docs/DATABASE_DESIGN.md` only if it already documents `PurchaseOrderLine` / `StockReservationAllocation` fields (add the new fields there; do not rewrite other sections)

- [ ] **Step 1: Run the full explicit suite**

Run:
```bash
python manage.py test apps.accounts.tests apps.cart.tests apps.catalog.tests apps.catalog.tests_parfumly apps.core.tests apps.inventory.tests apps.notifications.tests apps.offers.tests apps.orders.tests apps.payments.tests apps.purchases.tests apps.reviews.tests apps.shipping.tests
```
Expected: all PASS; count = 582 (P1E baseline; CURRENT_STATE says 14 modules — verify the module list with `find apps -name "tests*.py"`, add any missing module) + about 53 new tests. Record the exact number.

- [ ] **Step 2: System check and migration drift**

Run: `python manage.py check`
Expected: `System check identified no issues` (existing `staticfiles.W004` warning is known).

Run: `python manage.py makemigrations --check --dry-run`
Expected: `No changes detected`.

- [ ] **Step 3: Confirm P2A changed no sales behaviour**

Run: `git diff fa62a84 --stat -- apps/orders apps/cart apps/catalog`
Expected: no output (orders, cart and catalogue untouched).

Run: `git grep -n "ALLOCATION_INCOMING_PO_LINE" -- apps ':!*tests*.py'`
Expected: only `apps/inventory/models.py`, `apps/inventory/services/reservation.py` (guard), `apps/purchases/selectors.py`. No creator of incoming allocations.

- [ ] **Step 4: Update CURRENT_STATE.md**

Replace the Current Focus paragraph with P2A status (branch, commits, test count, migrations `purchases.0005_booked_incoming_foundation`, `inventory.0008_incoming_allocation`, flag still False, next: P2B plan). Keep it concise; remove outdated design-stage text.

- [ ] **Step 5: Report to the user**

Report test count, check output, migration check, and list of commits. Do not merge into `dev` or push without explicit instruction.
