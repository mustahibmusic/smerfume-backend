"""
Tests for apps.inventory.

Test groups (Phase 1 — foundation models):
    WarehouseTests           — default-warehouse uniqueness
    InventoryStockTests      — creation, availability, constraints
    PartialBottleLotTests    — FIFO ordering field, constraints
    DecantSourceTests        — one source per decant SKU
    StockMovementTests       — ledger creation and StockTransaction grouping

Test groups (Phase 2 — reservation service):
    DirectReservationTests          — retail/tester reservation, release, consume
    DecantReservationTests          — FIFO allocation across partial lots + bottle opening
    ReservationReleaseConsumeTests  — allocation-based release/consume correctness
    ReservationConcurrencyTests     — select_for_update prevents overselling

Test groups (stabilization pass — admin integrity audit):
    InventoryAdjustmentServiceTests — adjust_inventory_stock / adjust_partial_lot
    InventoryAdminIntegrityTests    — protected fields cannot be bypassed via admin
"""

import threading
from decimal import Decimal

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.inventory.admin import (
    InventoryStockAdmin,
    PartialBottleLotAdmin,
    StockMovementAdmin,
    StockReservationAdmin,
    StockReservationAllocationAdmin,
    StockTransactionAdmin,
)
from apps.inventory.models import (
    DecantSource,
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockReservationAllocation,
    StockTransaction,
    Supplier,
    Warehouse,
)
from apps.inventory.services import reservation as reservation_service
from apps.inventory.services import adjustment as adjustment_service
from apps.orders.models import Order, OrderItem

User = get_user_model()


def _make_variant(sku, size_ml=100, is_decant=False, selling_price="4500.00", mrp="5000.00"):
    brand = Brand.objects.get_or_create(name="InvTestBrand", slug="invtestbrand")[0]
    category = Category.objects.get_or_create(name="InvTestCat", slug="invtestcat")[0]
    product = Product.objects.get_or_create(
        name="InvTestProduct", slug="invtestproduct",
        defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="invtestproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=size_ml, is_decant=is_decant,
        selling_price=selling_price, mrp=mrp, sku=sku,
    )


_order_counter = 0


def _make_order_item(variant, quantity=1):
    """Create a minimal User + Order + OrderItem for reservation testing."""
    global _order_counter
    _order_counter += 1
    user = User.objects.create(
        username=f"invtestuser{_order_counter}",
        email=f"invtest{_order_counter}@example.com",
        mobile_number=f"90000{_order_counter:05d}",
    )
    order = Order.objects.create(
        user=user,
        order_number=f"SMR-TEST-{_order_counter:06d}",
        subtotal=Decimal("0.00"),
        total=Decimal("0.00"),
    )
    return OrderItem.objects.create(
        order=order, variant=variant, quantity=quantity,
        unit_price=Decimal(variant.selling_price),
    )


class WarehouseTests(TestCase):
    def test_only_one_default_warehouse_allowed(self):
        Warehouse.objects.get(is_default=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Warehouse.objects.create(name="Second Warehouse", is_default=True)

    def test_multiple_non_default_warehouses_allowed(self):
        Warehouse.objects.create(name="WH A", is_default=False)
        Warehouse.objects.create(name="WH B", is_default=False)
        # +1 for the seeded "Default Warehouse" every environment starts with.
        self.assertEqual(Warehouse.objects.count(), 3)


class InventoryStockTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-RETAIL-100ML")

    def test_available_property(self):
        stock = InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            quantity=10, quantity_reserved=3,
        )
        self.assertEqual(stock.available, 7)

    def test_negative_quantity_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventoryStock.objects.create(
                    variant=self.variant, warehouse=self.warehouse,
                    stock_type=InventoryStock.STOCK_TYPE_RETAIL,
                    quantity=-1,
                )

    def test_negative_reserved_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventoryStock.objects.create(
                    variant=self.variant, warehouse=self.warehouse,
                    stock_type=InventoryStock.STOCK_TYPE_RETAIL,
                    quantity=5, quantity_reserved=-1,
                )

    def test_unique_variant_warehouse_stock_type(self):
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=10,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventoryStock.objects.create(
                    variant=self.variant, warehouse=self.warehouse,
                    stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=5,
                )

    def test_same_variant_different_stock_type_allowed(self):
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=10,
        )
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_DAMAGED, quantity=1,
        )
        self.assertEqual(InventoryStock.objects.filter(variant=self.variant).count(), 2)


class PartialBottleLotTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-PARTIAL-100ML")
        self.txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )

    def test_available_ml_property(self):
        lot = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            remaining_ml=Decimal("90.00"), reserved_ml=Decimal("10.00"),
            opened_at=timezone.now(), source_transaction=self.txn,
        )
        self.assertEqual(lot.available_ml, Decimal("80.00"))

    def test_reserved_cannot_exceed_remaining(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PartialBottleLot.objects.create(
                    variant=self.variant, warehouse=self.warehouse,
                    remaining_ml=Decimal("50.00"), reserved_ml=Decimal("60.00"),
                    opened_at=timezone.now(), source_transaction=self.txn,
                )

    def test_negative_remaining_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PartialBottleLot.objects.create(
                    variant=self.variant, warehouse=self.warehouse,
                    remaining_ml=Decimal("-1.00"),
                    opened_at=timezone.now(), source_transaction=self.txn,
                )

    def test_depleted_lots_are_not_deleted(self):
        lot = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            remaining_ml=Decimal("0.00"), opened_at=timezone.now(),
            source_transaction=self.txn, is_depleted=True,
        )
        self.assertTrue(PartialBottleLot.objects.filter(pk=lot.pk).exists())

    def test_fifo_ordering_by_opened_at(self):
        now = timezone.now()
        lot_newer = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            remaining_ml=Decimal("25.00"), opened_at=now,
            source_transaction=self.txn,
        )
        lot_older = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            remaining_ml=Decimal("40.00"), opened_at=now - timezone.timedelta(days=1),
            source_transaction=self.txn,
        )
        ordered = list(
            PartialBottleLot.objects.filter(variant=self.variant).order_by("opened_at")
        )
        self.assertEqual(ordered, [lot_older, lot_newer])


class DecantSourceTests(TestCase):
    def setUp(self):
        self.source_variant = _make_variant("SKU-SOURCE-100ML", size_ml=100)
        self.decant_variant = _make_variant(
            "SKU-DECANT-10ML", size_ml=10, is_decant=True
        )

    def test_create_and_str(self):
        source = DecantSource.objects.create(
            decant_variant=self.decant_variant,
            source_variant=self.source_variant,
            decant_volume_ml=Decimal("10.00"),
        )
        self.assertIn("10.00ml", str(source))

    def test_one_source_per_decant_variant(self):
        DecantSource.objects.create(
            decant_variant=self.decant_variant,
            source_variant=self.source_variant,
            decant_volume_ml=Decimal("10.00"),
        )
        another_source_variant = _make_variant("SKU-SOURCE2-100ML")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DecantSource.objects.create(
                    decant_variant=self.decant_variant,
                    source_variant=another_source_variant,
                    decant_volume_ml=Decimal("10.00"),
                )

    def test_zero_volume_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DecantSource.objects.create(
                    decant_variant=self.decant_variant,
                    source_variant=self.source_variant,
                    decant_volume_ml=Decimal("0.00"),
                )


class StockMovementTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-MOVEMENT-100ML")
        self.supplier = Supplier.objects.create(name="Test Distributor")

    def test_purchase_in_movement(self):
        movement = StockMovement.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            movement_type=StockMovement.MOVEMENT_PURCHASE_IN,
            quantity_delta=Decimal("10.00"),
            reason=StockMovement.REASON_PURCHASE,
            supplier=self.supplier,
        )
        self.assertEqual(movement.quantity_delta, Decimal("10.00"))
        self.assertEqual(movement.supplier, self.supplier)

    def test_bottle_opening_grouped_under_one_transaction(self):
        """Opening a bottle produces two movements sharing one StockTransaction —
        the retail decrement and the partial-lot creation."""
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        StockMovement.objects.create(
            transaction_group=txn, variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            movement_type=StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT,
            quantity_delta=Decimal("-1.00"), reason=StockMovement.REASON_DECANT_PREPARATION,
        )
        StockMovement.objects.create(
            transaction_group=txn, variant=self.variant, warehouse=self.warehouse,
            stock_type="partial",
            movement_type=StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN,
            quantity_delta=Decimal("100.00"), reason=StockMovement.REASON_DECANT_PREPARATION,
        )
        self.assertEqual(txn.movements.count(), 2)


# ── Phase 2 — reservation service ─────────────────────────────────────────────

class DirectReservationTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-DIRECT-100ML")
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_reserve_direct_creates_allocation_and_increments_reserved(self):
        order_item = _make_order_item(self.variant, quantity=3)
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)

        self.assertEqual(res.purpose, StockReservation.PURPOSE_DIRECT_SALE)
        self.assertEqual(res.status, StockReservation.STATUS_HELD)
        self.assertEqual(res.quantity, Decimal("3"))

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity, Decimal("10"))
        self.assertEqual(stock.quantity_reserved, Decimal("3"))
        self.assertEqual(stock.available, Decimal("7"))

        allocation = res.allocations.get()
        self.assertEqual(allocation.allocation_type, StockReservationAllocation.ALLOCATION_RETAIL_UNIT)
        self.assertEqual(allocation.units, Decimal("3"))

    def test_insufficient_stock_raises_and_reserves_nothing(self):
        order_item = _make_order_item(self.variant, quantity=11)
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_for_order_item(order_item, self.warehouse)

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("0"))

    def test_release_restores_availability(self):
        order_item = _make_order_item(self.variant, quantity=4)
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.release_reservation(res)

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("0"))
        res.refresh_from_db()
        self.assertEqual(res.status, StockReservation.STATUS_RELEASED)
        self.assertIsNotNone(res.resolved_at)

    def test_consume_decrements_quantity_and_logs_movement(self):
        order_item = _make_order_item(self.variant, quantity=2)
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.consume_reservation(res)

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity, Decimal("8"))
        self.assertEqual(stock.quantity_reserved, Decimal("0"))

        movement = StockMovement.objects.get(source_order_item=order_item)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_SALE_OUT)
        self.assertEqual(movement.quantity_delta, Decimal("-2"))

        res.refresh_from_db()
        self.assertEqual(res.status, StockReservation.STATUS_CONSUMED)

    def test_cannot_release_already_consumed_reservation(self):
        order_item = _make_order_item(self.variant, quantity=1)
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.consume_reservation(res)
        with self.assertRaises(reservation_service.InvalidReservationStateError):
            reservation_service.release_reservation(res)

    def test_confirm_then_consume(self):
        order_item = _make_order_item(self.variant, quantity=1)
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.confirm_reservation(res)
        res.refresh_from_db()
        self.assertEqual(res.status, StockReservation.STATUS_CONFIRMED)
        reservation_service.consume_reservation(res)
        res.refresh_from_db()
        self.assertEqual(res.status, StockReservation.STATUS_CONSUMED)


class DecantReservationTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.source = _make_variant("SKU-SOURCE-100ML", size_ml=100)
        self.decant = _make_variant("SKU-DECANT-10ML", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=self.decant, source_variant=self.source,
            decant_volume_ml=Decimal("10.00"),
        )
        InventoryStock.objects.create(
            variant=self.source, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_decant_reservation_opens_bottle_when_no_partial_stock(self):
        order_item = _make_order_item(self.decant, quantity=3)  # 30ml needed
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)

        self.assertEqual(res.purpose, StockReservation.PURPOSE_DECANT_FULFILLMENT)
        self.assertEqual(res.variant, self.source)
        self.assertEqual(res.quantity, Decimal("30.00"))

        allocation = res.allocations.get()
        self.assertEqual(allocation.allocation_type, StockReservationAllocation.ALLOCATION_RETAIL_UNIT)
        self.assertEqual(allocation.units, Decimal("1"))
        self.assertEqual(allocation.claimed_ml, Decimal("30.00"))

        stock = InventoryStock.objects.get(
            variant=self.source, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("1"))

    def test_fifo_across_partial_lots_matches_approved_design_example(self):
        """P1=40ml, P2=70ml, P3=25ml; 50ml demand consumes P1(40) + P2(10)."""
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        now = timezone.now()
        p1 = PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("40.00"),
            opened_at=now - timezone.timedelta(hours=3), source_transaction=txn,
        )
        p2 = PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("70.00"),
            opened_at=now - timezone.timedelta(hours=2), source_transaction=txn,
        )
        p3 = PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("25.00"),
            opened_at=now - timezone.timedelta(hours=1), source_transaction=txn,
        )

        order_item = _make_order_item(self.decant, quantity=5)  # 50ml needed
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)

        allocations = list(res.allocations.order_by("id"))
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0].partial_lot_id, p1.pk)
        self.assertEqual(allocations[0].ml_amount, Decimal("40.00"))
        self.assertEqual(allocations[1].partial_lot_id, p2.pk)
        self.assertEqual(allocations[1].ml_amount, Decimal("10.00"))

        p1.refresh_from_db(); p2.refresh_from_db(); p3.refresh_from_db()
        self.assertEqual(p1.reserved_ml, Decimal("40.00"))
        self.assertEqual(p2.reserved_ml, Decimal("10.00"))
        self.assertEqual(p3.reserved_ml, Decimal("0.00"))

    def test_insufficient_decant_capacity_raises(self):
        order_item = _make_order_item(self.decant, quantity=200)  # 2000ml, only 1000ml exists
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_for_order_item(order_item, self.warehouse)


class DoubleBookingPreventionTests(TestCase):
    """Proves the corrected design (approved after Correction 1): a bottle
    reserved for one purpose is structurally invisible to the other."""

    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.source = _make_variant("SKU-SHARED-100ML", size_ml=100)
        self.decant = _make_variant("SKU-SHARED-DECANT-10ML", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=self.decant, source_variant=self.source,
            decant_volume_ml=Decimal("10.00"),
        )
        self.retail_stock = InventoryStock.objects.create(
            variant=self.source, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_worked_example_10_bottles_70ml_partial_2_reserved_300ml_decant(self):
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("70.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )

        direct_item = _make_order_item(self.source, quantity=2)
        reservation_service.reserve_for_order_item(direct_item, self.warehouse)

        decant_item = _make_order_item(self.decant, quantity=30)  # 300ml
        reservation_service.reserve_for_order_item(decant_item, self.warehouse)

        self.retail_stock.refresh_from_db()
        self.assertEqual(self.retail_stock.quantity, Decimal("10"))
        self.assertEqual(self.retail_stock.quantity_reserved, Decimal("5"))  # 2 direct + 3 decant
        self.assertEqual(self.retail_stock.available, Decimal("5"))

        lots = PartialBottleLot.objects.filter(
            variant=self.source, warehouse=self.warehouse, is_depleted=False
        )
        available_partial_ml = sum((lot.available_ml for lot in lots), Decimal("0"))
        self.assertEqual(available_partial_ml, Decimal("0.00"))

        available_decant_capacity_ml = (
            available_partial_ml + self.retail_stock.available * Decimal(self.source.size_ml)
        )
        self.assertEqual(available_decant_capacity_ml, Decimal("500.00"))

    def test_fully_committed_retail_stock_blocks_new_decant_reservation(self):
        direct_item = _make_order_item(self.source, quantity=10)
        reservation_service.reserve_for_order_item(direct_item, self.warehouse)

        decant_item = _make_order_item(self.decant, quantity=1)
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_for_order_item(decant_item, self.warehouse)


class DecantConsumeTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.source = _make_variant("SKU-CONSUME-100ML", size_ml=100)
        self.decant = _make_variant("SKU-CONSUME-DECANT-10ML", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=self.decant, source_variant=self.source,
            decant_volume_ml=Decimal("10.00"),
        )
        InventoryStock.objects.create(
            variant=self.source, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_consume_opens_bottle_and_creates_leftover_partial_lot(self):
        order_item = _make_order_item(self.decant, quantity=1)  # 10ml needed
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.consume_reservation(res)

        stock = InventoryStock.objects.get(
            variant=self.source, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity, Decimal("9"))
        self.assertEqual(stock.quantity_reserved, Decimal("0"))

        lot = PartialBottleLot.objects.get(variant=self.source, warehouse=self.warehouse)
        self.assertEqual(lot.remaining_ml, Decimal("90.00"))
        self.assertEqual(lot.reserved_ml, Decimal("0.00"))
        self.assertFalse(lot.is_depleted)

        movements = list(
            StockMovement.objects.filter(source_order_item=order_item).order_by("id")
        )
        self.assertEqual(
            [m.movement_type for m in movements],
            [
                StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT,
                StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN,
                StockMovement.MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL,
            ],
        )
        self.assertEqual(movements[0].transaction_group_id, movements[1].transaction_group_id)
        self.assertEqual(movements[1].transaction_group_id, movements[2].transaction_group_id)

    def test_consume_from_existing_partial_lot_does_not_open_new_bottle(self):
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        lot = PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("50.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )
        order_item = _make_order_item(self.decant, quantity=1)  # 10ml
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)
        reservation_service.consume_reservation(res)

        stock = InventoryStock.objects.get(
            variant=self.source, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity, Decimal("10"))  # unchanged — no bottle opened

        lot.refresh_from_db()
        self.assertEqual(lot.remaining_ml, Decimal("40.00"))
        self.assertEqual(lot.reserved_ml, Decimal("0.00"))

    def test_release_of_decant_reservation_restores_partial_lot_and_retail(self):
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        lot = PartialBottleLot.objects.create(
            variant=self.source, warehouse=self.warehouse, remaining_ml=Decimal("15.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )
        order_item = _make_order_item(self.decant, quantity=2)  # 20ml: 15 from lot + 1 bottle for the rest
        res = reservation_service.reserve_for_order_item(order_item, self.warehouse)

        reservation_service.release_reservation(res)

        lot.refresh_from_db()
        self.assertEqual(lot.reserved_ml, Decimal("0.00"))
        stock = InventoryStock.objects.get(
            variant=self.source, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("0"))
        self.assertEqual(stock.quantity, Decimal("10"))  # nothing physically consumed by a release


class ReservationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase's flush timing relative to the migration-seeded
        # default warehouse isn't reliable across test ordering (present for
        # the first TransactionTestCase to run, gone for the rest) — clear
        # and recreate explicitly rather than depending on either state.
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.variant = _make_variant("SKU-CONCURRENT-100ML")
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_concurrent_direct_reservations_cannot_oversell(self):
        order_item_a = _make_order_item(self.variant, quantity=6)
        order_item_b = _make_order_item(self.variant, quantity=6)
        results = {}
        barrier = threading.Barrier(2)

        def _run(key, order_item):
            barrier.wait()
            try:
                reservation_service.reserve_for_order_item(order_item, self.warehouse)
                results[key] = "ok"
            except reservation_service.InsufficientStockError:
                results[key] = "insufficient"
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("a", order_item_a))
        t2 = threading.Thread(target=_run, args=("b", order_item_b))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), ["insufficient", "ok"])
        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("6"))


class ReservationConcurrencyDecantVsRetailTests(TransactionTestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.source = _make_variant("SKU-CONCURRENT-SOURCE-100ML", size_ml=100)
        self.decant = _make_variant("SKU-CONCURRENT-DECANT-10ML", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=self.decant, source_variant=self.source,
            decant_volume_ml=Decimal("10.00"),
        )
        InventoryStock.objects.create(
            variant=self.source, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_concurrent_direct_and_decant_reservation_cannot_double_book(self):
        direct_item = _make_order_item(self.source, quantity=10)     # claims all 10 bottles
        decant_item = _make_order_item(self.decant, quantity=100)    # 1000ml = all 10 bottles worth
        results = {}
        barrier = threading.Barrier(2)

        def _run(key, order_item):
            barrier.wait()
            try:
                reservation_service.reserve_for_order_item(order_item, self.warehouse)
                results[key] = "ok"
            except reservation_service.InsufficientStockError:
                results[key] = "insufficient"
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("direct", direct_item))
        t2 = threading.Thread(target=_run, args=("decant", decant_item))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), ["insufficient", "ok"])
        stock = InventoryStock.objects.get(
            variant=self.source, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("10"))


# ── Stabilization pass: adjustment service + admin integrity ─────────────────

class InventoryAdjustmentServiceTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-ADJUST-100ML")
        self.stock = InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )

    def test_positive_adjustment_increases_quantity_and_logs_movement(self):
        adjustment_service.adjust_inventory_stock(
            self.stock, quantity_delta=Decimal("5"), reason=StockMovement.REASON_PURCHASE,
        )
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("15"))
        movement = StockMovement.objects.get(variant=self.variant, movement_type=StockMovement.MOVEMENT_ADJUSTMENT)
        self.assertEqual(movement.quantity_delta, Decimal("5"))
        self.assertEqual(movement.reason, StockMovement.REASON_PURCHASE)

    def test_negative_adjustment_cannot_go_below_zero(self):
        with self.assertRaises(adjustment_service.InventoryAdjustmentError):
            adjustment_service.adjust_inventory_stock(
                self.stock, quantity_delta=Decimal("-20"), reason=StockMovement.REASON_STOCK_WRITTEN_OFF,
            )
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("10"))  # unchanged

    def test_zero_delta_rejected(self):
        with self.assertRaises(adjustment_service.InventoryAdjustmentError):
            adjustment_service.adjust_inventory_stock(
                self.stock, quantity_delta=Decimal("0"), reason=StockMovement.REASON_STOCKTAKING_RESULTS,
            )

    def test_partial_lot_adjustment_marks_depleted_at_zero(self):
        txn = StockTransaction.objects.create(transaction_type=StockTransaction.TYPE_ADJUSTMENT)
        lot = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse, remaining_ml=Decimal("10.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )
        adjustment_service.adjust_partial_lot(
            lot, ml_delta=Decimal("-10.00"), reason=StockMovement.REASON_STOCK_WRITTEN_OFF,
            notes="evaporated",
        )
        lot.refresh_from_db()
        self.assertEqual(lot.remaining_ml, Decimal("0.00"))
        self.assertTrue(lot.is_depleted)

    def test_partial_lot_adjustment_cannot_go_below_reserved(self):
        txn = StockTransaction.objects.create(transaction_type=StockTransaction.TYPE_ADJUSTMENT)
        lot = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse, remaining_ml=Decimal("10.00"),
            reserved_ml=Decimal("6.00"), opened_at=timezone.now(), source_transaction=txn,
        )
        with self.assertRaises(adjustment_service.InventoryAdjustmentError):
            adjustment_service.adjust_partial_lot(
                lot, ml_delta=Decimal("-8.00"), reason=StockMovement.REASON_STOCK_WRITTEN_OFF,
            )
        lot.refresh_from_db()
        self.assertEqual(lot.remaining_ml, Decimal("10.00"))  # unchanged


class InventoryAdminIntegrityTests(TestCase):
    """Proves the protected fields identified in the audit cannot be edited
    through the admin form, using the same get_form().base_fields technique
    established in apps.orders.tests.OrderStatusAdminLockdownTests."""

    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="invadminstaff", email="invadminstaff@example.com", password="testpass123",
        )
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _make_variant("SKU-ADMINAUDIT-100ML")
        self.stock = InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("10"),
        )
        self.txn = StockTransaction.objects.create(transaction_type=StockTransaction.TYPE_ADJUSTMENT)
        self.lot = PartialBottleLot.objects.create(
            variant=self.variant, warehouse=self.warehouse, remaining_ml=Decimal("50.00"),
            opened_at=timezone.now(), source_transaction=self.txn,
        )
        self.movement = StockMovement.objects.create(
            variant=self.variant, warehouse=self.warehouse, stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            movement_type=StockMovement.MOVEMENT_ADJUSTMENT, quantity_delta=Decimal("10"),
            reason=StockMovement.REASON_PURCHASE,
        )
        self.request = RequestFactory().get("/")
        self.request.user = self.staff

    def test_inventory_stock_quantity_is_readonly_on_existing_row(self):
        admin_instance = InventoryStockAdmin(InventoryStock, django_admin.site)
        self.assertIn("quantity", admin_instance.get_readonly_fields(self.request, self.stock))
        self.assertIn("quantity_reserved", admin_instance.get_readonly_fields(self.request, self.stock))
        form_class = admin_instance.get_form(self.request, self.stock)
        self.assertNotIn("quantity", form_class.base_fields)

    def test_inventory_stock_quantity_is_editable_on_add_form(self):
        # The one legitimate exception: bootstrapping a brand-new
        # variant/warehouse/stock_type row needs an initial value.
        admin_instance = InventoryStockAdmin(InventoryStock, django_admin.site)
        self.assertEqual(admin_instance.get_readonly_fields(self.request, None), [])
        form_class = admin_instance.get_form(self.request, None)
        self.assertIn("quantity", form_class.base_fields)

    def test_inventory_stock_admin_change_page_does_not_render_quantity_widget(self):
        self.client.login(username="invadminstaff@example.com", password="testpass123")
        url = f"/admin/inventory/inventorystock/{self.stock.pk}/change/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="quantity"')
        self.assertNotContains(resp, 'name="quantity_reserved"')

    def test_inventory_stock_admin_form_adjustment_creates_real_movement(self):
        self.client.login(username="invadminstaff@example.com", password="testpass123")
        url = f"/admin/inventory/inventorystock/{self.stock.pk}/change/"
        self.client.post(url, data={
            "reorder_level": "",
            "quantity_delta": "3",
            "reason": StockMovement.REASON_STOCKTAKING_RESULTS,
            "notes": "count correction",
            "_save": "Save",
        })
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("13"))
        self.assertTrue(
            StockMovement.objects.filter(
                variant=self.variant, movement_type=StockMovement.MOVEMENT_ADJUSTMENT,
                quantity_delta=Decimal("3"),
            ).exists()
        )

    def test_partial_bottle_lot_remaining_ml_is_readonly(self):
        admin_instance = PartialBottleLotAdmin(PartialBottleLot, django_admin.site)
        readonly = admin_instance.get_readonly_fields(self.request, self.lot)
        self.assertIn("remaining_ml", readonly)
        self.assertIn("reserved_ml", readonly)
        self.assertIn("is_depleted", readonly)
        form_class = admin_instance.get_form(self.request, self.lot)
        self.assertNotIn("remaining_ml", form_class.base_fields)

    def test_partial_bottle_lot_has_no_add_permission(self):
        admin_instance = PartialBottleLotAdmin(PartialBottleLot, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(self.request))

    def test_stock_movement_has_no_add_or_delete_permission(self):
        admin_instance = StockMovementAdmin(StockMovement, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(self.request))
        self.assertFalse(admin_instance.has_delete_permission(self.request, self.movement))

    def test_stock_transaction_has_no_add_or_delete_permission(self):
        admin_instance = StockTransactionAdmin(StockTransaction, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(self.request))
        self.assertFalse(admin_instance.has_delete_permission(self.request, self.txn))

    def test_stock_reservation_is_fully_locked(self):
        admin_instance = StockReservationAdmin(StockReservation, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(self.request))
        self.assertFalse(admin_instance.has_change_permission(self.request))
        self.assertFalse(admin_instance.has_delete_permission(self.request))

    def test_stock_reservation_allocation_is_fully_locked(self):
        admin_instance = StockReservationAllocationAdmin(StockReservationAllocation, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(self.request))
        self.assertFalse(admin_instance.has_change_permission(self.request))
        self.assertFalse(admin_instance.has_delete_permission(self.request))
