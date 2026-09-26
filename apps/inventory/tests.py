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

import datetime
import re
import threading
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
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
from apps.purchases.models import (
    GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine,
)

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

    def test_stock_movement_source_return_item_is_readonly(self):
        admin_instance = StockMovementAdmin(StockMovement, django_admin.site)
        self.assertIn("source_return_item", admin_instance.readonly_fields)
        self.assertIn("quantity_delta", admin_instance.readonly_fields)

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


class VendorMasterTests(TestCase):
    """Vendor Master (P1A): inventory.Supplier shown to staff as Vendor."""

    def _vendor(self, **kwargs):
        kwargs.setdefault("name", "Test Vendor")
        return Supplier.objects.create(**kwargs)

    # --- vendor_code ---

    def test_vendor_code_generated_on_create(self):
        vendor = self._vendor()
        self.assertEqual(vendor.vendor_code, f"VEN-{vendor.pk:05d}")

    def test_vendor_code_stable_across_saves(self):
        vendor = self._vendor()
        code = vendor.vendor_code
        vendor.name = "Renamed Vendor"
        vendor.save()
        vendor.refresh_from_db()
        self.assertEqual(vendor.vendor_code, code)

    def test_vendor_codes_are_unique(self):
        first, second = self._vendor(), self._vendor(name="Other")
        self.assertNotEqual(first.vendor_code, second.vendor_code)

    def test_vendor_code_derived_for_rows_inserted_outside_django(self):
        # Rows that existed before the migration, or are inserted by raw
        # SQL, get their code from the database, the same way the backfill
        # works when the generated column is added.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO inventory_supplier (id, public_id, is_active, created_at, updated_at,"
                " name, contact_person, phone, email, address, legal_name, address_line1,"
                " address_line2, city, state, state_code, pincode, country, gstin, pan,"
                " gst_treatment, payment_terms_days, notes, external_accounting_id)"
                " VALUES (1234567, gen_random_uuid(), true, now(), now(), 'Legacy Vendor',"
                " '', '', '', 'Old street 1', '', '', '', '', '', '', '', 'IN', '', '', '',"
                " 0, '', '')"
            )
        vendor = Supplier.objects.get(pk=1234567)
        self.assertEqual(vendor.vendor_code, "VEN-1234567")

    def test_vendor_code_not_editable_in_admin(self):
        from apps.inventory.admin import SupplierAdmin

        admin_instance = SupplierAdmin(Supplier, django_admin.site)
        self.assertIn("vendor_code", admin_instance.get_readonly_fields(None))

    # --- compatibility ---

    def test_minimal_vendor_and_stock_movement_link(self):
        vendor = self._vendor()
        self.assertEqual(vendor.country, "IN")
        self.assertEqual(vendor.payment_terms_days, 0)
        movement = StockMovement.objects.create(
            variant=_make_variant("SKU-VENDOR-COMPAT"),
            warehouse=Warehouse.objects.get(is_default=True),
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            movement_type=StockMovement.MOVEMENT_PURCHASE_IN,
            quantity_delta=Decimal("1"),
            reason=StockMovement.REASON_PURCHASE,
            supplier=vendor,
        )
        self.assertEqual(movement.supplier, vendor)
        self.assertIn(movement, vendor.stock_movements.all())

    def test_legacy_address_preserved(self):
        vendor = self._vendor(address="12 Crawford Market, Mumbai")
        vendor.address_line1 = "12 Crawford Market"
        vendor.city = "Mumbai"
        vendor.save()
        vendor.refresh_from_db()
        self.assertEqual(vendor.address, "12 Crawford Market, Mumbai")

    # --- GSTIN / PAN ---

    def test_gstin_and_pan_normalized_before_validation(self):
        vendor = Supplier(name="Norm", gstin="  27aapfu0939f1zv ", pan=" aapfu0939f ")
        vendor.full_clean()
        self.assertEqual(vendor.gstin, "27AAPFU0939F1ZV")
        self.assertEqual(vendor.pan, "AAPFU0939F")

    def test_gstin_normalized_on_save(self):
        vendor = self._vendor(gstin=" 27aapfu0939f1zv")
        vendor.refresh_from_db()
        self.assertEqual(vendor.gstin, "27AAPFU0939F1ZV")

    def test_invalid_gstin_fails_validation(self):
        vendor = Supplier(name="Bad", gstin="27AAPFU0939")
        with self.assertRaises(ValidationError) as ctx:
            vendor.full_clean()
        self.assertIn("gstin", ctx.exception.message_dict)

    def test_invalid_pan_fails_validation(self):
        vendor = Supplier(name="Bad", pan="1234567890")
        with self.assertRaises(ValidationError) as ctx:
            vendor.full_clean()
        self.assertIn("pan", ctx.exception.message_dict)

    def test_multiple_blank_gstins_allowed(self):
        self._vendor(name="A")
        self._vendor(name="B")
        self.assertEqual(Supplier.objects.filter(gstin="").count(), 2)

    def test_duplicate_normalized_gstin_rejected(self):
        self._vendor(name="A", gstin="27AAPFU0939F1ZV")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._vendor(name="B", gstin=" 27aapfu0939f1zv ")

    def test_duplicate_gstin_fails_model_validation(self):
        self._vendor(name="A", gstin="27AAPFU0939F1ZV")
        with self.assertRaises(ValidationError):
            Supplier(name="B", gstin="27aapfu0939f1zv").full_clean()

    # --- payment terms ---

    def test_negative_payment_terms_fails_validation(self):
        with self.assertRaises(ValidationError):
            Supplier(name="Neg", payment_terms_days=-1).full_clean()

    def test_negative_payment_terms_rejected_by_database(self):
        vendor = self._vendor()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Supplier.objects.filter(pk=vendor.pk).update(payment_terms_days=-5)

    # --- admin / navigation ---

    def test_admin_registered_as_vendor_and_delete_denied(self):
        from apps.inventory.admin import SupplierAdmin

        self.assertIsInstance(django_admin.site._registry[Supplier], SupplierAdmin)
        self.assertEqual(str(Supplier._meta.verbose_name_plural), "vendors")
        staff = User.objects.create_superuser(
            username="vendoradmin", email="vendoradmin@example.com", password="testpass123",
        )
        request = RequestFactory().get("/admin/")
        request.user = staff
        vendor = self._vendor()
        self.assertFalse(
            django_admin.site._registry[Supplier].has_delete_permission(request, vendor)
        )

    def test_vendors_in_purchases_navigation(self):
        from django.conf import settings
        from django.urls import reverse

        groups = {
            group.get("title"): group["items"]
            for group in settings.UNFOLD["SIDEBAR"]["navigation"]
        }
        items = {item["title"]: item for item in groups["Purchases"]}
        self.assertEqual(
            str(items["Vendors"]["link"]), reverse("admin:inventory_supplier_changelist")
        )
        staff = User.objects.create_user(
            username="novendorperm", email="novendorperm@example.com", password="testpass123",
            is_staff=True,
        )
        request = RequestFactory().get("/admin/")
        request.user = staff
        self.assertFalse(items["Vendors"]["permission"](request))

    def test_admin_add_form_validates_and_saves(self):
        staff = User.objects.create_superuser(
            username="vendorform", email="vendorform@example.com", password="testpass123",
        )
        request = RequestFactory().get("/admin/")
        request.user = staff
        form_class = django_admin.site._registry[Supplier].get_form(request)
        form = form_class(data={
            "name": "Form Vendor", "gstin": "27aapfu0939f1zv", "country": "IN",
            "payment_terms_days": 15, "is_active": True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        vendor = form.save()
        self.assertEqual(vendor.gstin, "27AAPFU0939F1ZV")
        self.assertTrue(vendor.vendor_code.startswith("VEN-"))


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
    """Release frees active incoming rows (P2B); consume still refuses them
    until they are converted or reallocated (P2C)."""

    def test_release_marks_active_incoming_row_released(self):
        row = self._incoming()
        reservation_service.release_reservation(self.reservation)
        row.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(row.incoming_status, StockReservationAllocation.INCOMING_RELEASED)
        self.assertIsNotNone(row.incoming_resolved_at)
        self.assertEqual(self.reservation.status, StockReservation.STATUS_RELEASED)
        stock = InventoryStock.objects.get(pk=self.stock.pk)
        self.assertEqual((stock.quantity, stock.quantity_reserved), (5, 0))

    def test_release_returns_units_to_incoming_sellable(self):
        from apps.purchases import selectors as purchase_selectors
        PurchaseOrderLine.objects.filter(pk=self.po_line.pk).update(confirmed_booked_quantity=10)
        PurchaseOrder.objects.filter(pk=self.po_line.purchase_order_id).update(
            status=PurchaseOrder.STATUS_ISSUED
        )
        self._incoming(units=Decimal("4"))
        line = PurchaseOrderLine.objects.get(pk=self.po_line.pk)
        self.assertEqual(purchase_selectors.incoming_sellable(line), 6)
        reservation_service.release_reservation(self.reservation)
        self.assertEqual(purchase_selectors.incoming_sellable(line), 10)

    def test_release_mixed_physical_and_incoming(self):
        self._physical(units=Decimal("1"))
        InventoryStock.objects.filter(pk=self.stock.pk).update(quantity_reserved=1)
        row = self._incoming(units=Decimal("1"))
        reservation_service.release_reservation(self.reservation)
        row.refresh_from_db()
        self.assertEqual(row.incoming_status, StockReservationAllocation.INCOMING_RELEASED)
        self.assertEqual(InventoryStock.objects.get(pk=self.stock.pk).quantity_reserved, 0)

    def test_release_leaves_historical_incoming_rows_untouched(self):
        resolved_at = timezone.now() - timezone.timedelta(days=1)
        old = self._incoming(incoming_status="released", incoming_resolved_at=resolved_at)
        self._incoming(units=Decimal("1"))
        reservation_service.release_reservation(self.reservation)
        old.refresh_from_db()
        self.assertEqual(old.incoming_resolved_at, resolved_at)

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

    def _resolved_with_replacement(self, status):
        """A converted/reallocated incoming row whose replacement is the
        physical row that now holds the units."""
        replacement = self._physical(units=Decimal("2"))
        InventoryStock.objects.filter(pk=self.stock.pk).update(quantity_reserved=2)
        extra = {}
        if status == StockReservationAllocation.INCOMING_CONVERTED:
            po = self.po_line.purchase_order
            receipt = GoodsReceipt.objects.create(
                warehouse=self.warehouse, receipt_type="standard",
                purchase_order=po, supplier=po.supplier,
            )
            extra["converted_by_receipt_line"] = GoodsReceiptLine.objects.create(
                receipt=receipt, po_line=self.po_line, quantity=2,
            )
        self._incoming(
            incoming_status=status, replacement=replacement,
            incoming_resolved_at=timezone.now(), **extra,
        )

    def test_converted_and_reallocated_rows_consume_replacement_once(self):
        for status in (
            StockReservationAllocation.INCOMING_CONVERTED,
            StockReservationAllocation.INCOMING_REALLOCATED,
        ):
            with self.subTest(status=status), transaction.atomic():
                self._resolved_with_replacement(status)
                reservation_service.consume_reservation(self.reservation)
                stock = InventoryStock.objects.get(pk=self.stock.pk)
                self.assertEqual((stock.quantity, stock.quantity_reserved), (3, 0))
                self.assertEqual(
                    StockMovement.objects.filter(source_order_item=self.reservation.order_item)
                    .count(), 1,
                )
                transaction.set_rollback(True)

    def test_converted_and_reallocated_rows_release_replacement_once(self):
        for status in (
            StockReservationAllocation.INCOMING_CONVERTED,
            StockReservationAllocation.INCOMING_REALLOCATED,
        ):
            with self.subTest(status=status), transaction.atomic():
                self._resolved_with_replacement(status)
                reservation_service.release_reservation(self.reservation)
                stock = InventoryStock.objects.get(pk=self.stock.pk)
                self.assertEqual((stock.quantity, stock.quantity_reserved), (5, 0))
                transaction.set_rollback(True)

    def test_historical_incoming_row_skipped_for_decant_reservation(self):
        StockReservation.objects.filter(pk=self.reservation.pk).update(
            purpose=StockReservation.PURPOSE_DECANT_FULFILLMENT
        )
        self._incoming(incoming_status="released", incoming_resolved_at=timezone.now())
        reservation_service.consume_reservation(self.reservation)
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, StockReservation.STATUS_CONSUMED)
        self.assertFalse(
            StockMovement.objects.filter(source_order_item=self.reservation.order_item).exists()
        )


# --- P2B: whole-order checkout reservation ---


class _ReserveOrderItemsBase(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.buyer = User.objects.create_user(username="p2b-inv-buyer", password="x")
        self.supplier = Supplier.objects.create(name="P2B Inventory Vendor")

    def _variant(self, sku, stock=None, size_ml=100):
        variant = _make_variant(sku, size_ml=size_ml)
        if stock is not None:
            InventoryStock.objects.create(
                variant=variant, warehouse=self.warehouse,
                stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal(stock),
            )
        return variant

    def _items(self, *pairs):
        """One order with an OrderItem per (variant, quantity) pair."""
        first = _make_order_item(pairs[0][0], pairs[0][1])
        items = [first]
        for variant, quantity in pairs[1:]:
            items.append(OrderItem.objects.create(
                order=first.order, variant=variant, quantity=quantity,
                unit_price=Decimal(variant.selling_price),
            ))
        return items

    def _book(self, variant, confirmed, status=PurchaseOrder.STATUS_ISSUED,
              expected_date=None, warehouse=None):
        po = PurchaseOrder.objects.create(
            supplier=self.supplier, warehouse=warehouse or self.warehouse,
            created_by=self.buyer, status=status, expected_date=expected_date,
        )
        return PurchaseOrderLine.objects.create(
            purchase_order=po, variant=variant, quantity_ordered=max(confirmed, 1),
            unit_price=Decimal("100.00"), confirmed_booked_quantity=confirmed,
        )

    def _stock(self, variant):
        return InventoryStock.objects.get(
            variant=variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
        )

    def _incoming_rows(self, **filters):
        return StockReservationAllocation.objects.filter(
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE, **filters,
        )


class ReserveOrderItemsPhysicalTests(_ReserveOrderItemsBase):
    """reserve_order_items with allow_incoming=False: today's physical
    behaviour, with whole-order sorted stock locks."""

    def test_reserves_every_item_physically(self):
        a, b = self._variant("P2B-PHY-A", stock=5), self._variant("P2B-PHY-B", stock=5)
        reservations = reservation_service.reserve_order_items(
            self._items((a, 2), (b, 3)), self.warehouse,
        )
        self.assertEqual([r.quantity for r in reservations], [2, 3])
        self.assertEqual(self._stock(a).quantity_reserved, 2)
        self.assertEqual(self._stock(b).quantity_reserved, 3)
        for reservation in reservations:
            allocation = reservation.allocations.get()
            self.assertEqual(
                allocation.allocation_type, StockReservationAllocation.ALLOCATION_RETAIL_UNIT
            )
            self.assertEqual(allocation.units, reservation.quantity)

    def test_stock_rows_locked_in_variant_order(self):
        variants = [self._variant(f"P2B-LOCK-{n}", stock=5) for n in range(3)]
        items = self._items((variants[2], 1), (variants[0], 1), (variants[1], 1))
        with CaptureQueriesContext(connection) as ctx:
            reservation_service.reserve_order_items(items, self.warehouse)
        locked = []
        for query in ctx.captured_queries:
            sql = query["sql"]
            if "FOR UPDATE" in sql and "inventory_inventorystock" in sql:
                match = re.search(r'"variant_id" = (\d+)', sql)
                if match:
                    locked.append(int(match.group(1)))
        self.assertEqual(locked, sorted(v.pk for v in variants))

    def test_direct_and_decant_items_share_the_source_counter(self):
        source = self._variant("P2B-SRC", stock=1)
        decant = _make_variant("P2B-DEC", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=decant, source_variant=source, decant_volume_ml=Decimal("10"),
        )
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_order_items(
                self._items((source, 1), (decant, 1)), self.warehouse,
            )
        self.assertEqual(self._stock(source).quantity_reserved, 0)
        self.assertFalse(StockReservation.objects.exists())

    def test_shortfall_rolls_back_every_item(self):
        a, b = self._variant("P2B-RB-A", stock=5), self._variant("P2B-RB-B", stock=1)
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_order_items(self._items((a, 2), (b, 2)), self.warehouse)
        self.assertEqual(self._stock(a).quantity_reserved, 0)
        self.assertFalse(StockReservation.objects.exists())

    def test_shortfall_message_unchanged(self):
        variant = self._variant("P2B-MSG", stock=1)
        with self.assertRaises(reservation_service.InsufficientStockError) as ctx:
            reservation_service.reserve_order_items(self._items((variant, 2)), self.warehouse)
        self.assertEqual(
            str(ctx.exception),
            f"Only {self._stock(variant).available} unit(s) of {variant} available at "
            f"{self.warehouse}, needed 2.",
        )

    def test_booked_lines_ignored_and_never_queried(self):
        variant = self._variant("P2B-OFF", stock=0)
        self._book(variant, 5)
        with CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(reservation_service.InsufficientStockError):
                reservation_service.reserve_order_items(self._items((variant, 1)), self.warehouse)
        self.assertFalse([q for q in ctx.captured_queries if "purchases_" in q["sql"]])
        self.assertFalse(self._incoming_rows().exists())

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_reserve_for_order_item_stays_physical_only(self):
        """In-store path: physical only, even with the flag on."""
        variant = self._variant("P2B-INSTORE", stock=0)
        self._book(variant, 5)
        with self.assertRaises(reservation_service.InsufficientStockError):
            reservation_service.reserve_for_order_item(_make_order_item(variant, 1), self.warehouse)
        self.assertFalse(self._incoming_rows().exists())


class ReserveOrderItemsIncomingTests(_ReserveOrderItemsBase):
    """reserve_order_items with allow_incoming=True: physical first, then
    booked incoming PO lines (spec §5.3)."""

    def _reserve(self, *pairs):
        return reservation_service.reserve_order_items(
            self._items(*pairs), self.warehouse, allow_incoming=True,
        )

    def test_incoming_only(self):
        variant = self._variant("P2B-IN-ONLY", stock=0)
        line = self._book(variant, 5)
        [reservation] = self._reserve((variant, 3))
        allocation = reservation.allocations.get()
        self.assertEqual(
            allocation.allocation_type, StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE
        )
        self.assertEqual(allocation.purchase_order_line, line)
        self.assertEqual(allocation.incoming_status, StockReservationAllocation.INCOMING_ACTIVE)
        self.assertEqual(allocation.units, 3)
        self.assertEqual(reservation.quantity, 3)
        self.assertEqual(self._stock(variant).quantity_reserved, 0)

    def test_physical_first_then_incoming(self):
        variant = self._variant("P2B-MIX", stock=2)
        self._book(variant, 5)
        [reservation] = self._reserve((variant, 4))
        rows = {a.allocation_type: a.units for a in reservation.allocations.all()}
        self.assertEqual(rows, {"retail_unit": 2, "incoming_po_line": 2})
        self.assertEqual(self._stock(variant).quantity_reserved, 2)

    def test_physical_covers_everything_when_enough(self):
        variant = self._variant("P2B-PHYS-ENOUGH", stock=5)
        self._book(variant, 5)
        self._reserve((variant, 3))
        self.assertFalse(self._incoming_rows().exists())

    def test_priority_expected_date_nulls_last(self):
        variant = self._variant("P2B-PRIO", stock=0)
        undated = self._book(variant, 2)
        later = self._book(variant, 2, expected_date=datetime.date(2026, 12, 1))
        sooner = self._book(variant, 2, expected_date=datetime.date(2026, 11, 1))
        self._reserve((variant, 5))
        units = {row.purchase_order_line_id: row.units for row in self._incoming_rows()}
        self.assertEqual(units, {sooner.pk: 2, later.pk: 2, undated.pk: 1})

    def test_priority_ties_break_on_po_then_line_id(self):
        variant = self._variant("P2B-TIE", stock=0)
        first = self._book(variant, 2)
        self._book(variant, 2)
        self._reserve((variant, 2))
        self.assertEqual(
            list(self._incoming_rows().values_list("purchase_order_line_id", flat=True)),
            [first.pk],
        )

    def test_non_contributing_lines_ignored(self):
        variant = self._variant("P2B-EXCL", stock=0)
        other_warehouse = Warehouse.objects.create(name="P2B Other Warehouse")
        for status in (
            PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_CLOSED,
            PurchaseOrder.STATUS_CANCELLED, PurchaseOrder.STATUS_RECEIVED,
        ):
            self._book(variant, 5, status=status)
        self._book(variant, 5, warehouse=other_warehouse)
        self._book(variant, 0)
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reserve((variant, 1))
        self.assertFalse(self._incoming_rows().exists())

    def test_existing_active_allocations_reduce_incoming(self):
        variant = self._variant("P2B-TAKEN", stock=0)
        self._book(variant, 3)
        self._reserve((variant, 2))
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reserve((variant, 2))
        self._reserve((variant, 1))

    def test_line_dropped_when_po_no_longer_bookable_after_lock(self):
        """The PO status is re-read after the line lock (close/cancel race)."""
        variant = self._variant("P2B-RACE", stock=0)
        line = self._book(variant, 5)
        PurchaseOrder.objects.filter(pk=line.purchase_order_id).update(
            status=PurchaseOrder.STATUS_CLOSED
        )
        with patch.object(reservation_service, "_lock_candidate_lines", return_value=[line]):
            with self.assertRaises(reservation_service.InsufficientStockError):
                self._reserve((variant, 1))
        self.assertFalse(self._incoming_rows().exists())

    def test_items_in_one_order_cannot_share_the_same_unit(self):
        variant = self._variant("P2B-SHARE", stock=0)
        line = self._book(variant, 3)
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reserve((variant, 2), (variant, 2))
        self._reserve((variant, 2), (variant, 1))
        self.assertEqual(
            sum(r.units for r in self._incoming_rows(purchase_order_line=line)), 3
        )

    def test_incoming_units_are_whole(self):
        variant = self._variant("P2B-WHOLE", stock="1.5")
        self._book(variant, 5)
        [reservation] = self._reserve((variant, 2))
        rows = {a.allocation_type: a.units for a in reservation.allocations.all()}
        self.assertEqual(rows, {"retail_unit": 1, "incoming_po_line": 1})

    def test_fractional_incoming_quantity_rejected(self):
        variant = self._variant("P2B-FRACTION", stock=0)
        self._book(variant, 5)
        [item] = self._items((variant, 1))
        item.quantity = Decimal("1.5")
        with self.assertRaises(ValueError):
            reservation_service.reserve_order_items([item], self.warehouse, allow_incoming=True)
        self.assertFalse(self._incoming_rows().exists())

    def test_decant_items_never_use_incoming(self):
        source = self._variant("P2B-DSRC", stock=0)
        self._book(source, 5)
        decant = _make_variant("P2B-DDEC", size_ml=10, is_decant=True)
        DecantSource.objects.create(
            decant_variant=decant, source_variant=source, decant_volume_ml=Decimal("10"),
        )
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reserve((decant, 1))
        self.assertFalse(self._incoming_rows().exists())

    def test_shortfall_message_uses_combined_figure(self):
        variant = self._variant("P2B-IN-MSG", stock=1)
        self._book(variant, 2)
        with self.assertRaises(reservation_service.InsufficientStockError) as ctx:
            self._reserve((variant, 5))
        self.assertEqual(
            str(ctx.exception),
            f"Only 3.00 unit(s) of {variant} available at {self.warehouse}, needed 5.",
        )
        self.assertNotIn("incoming", str(ctx.exception).lower())

    def test_purchases_query_count_is_independent_of_item_count(self):
        def purchases_queries(count):
            pairs = []
            for n in range(count):
                variant = self._variant(f"P2B-QC-{count}-{n}", stock=0)
                self._book(variant, 5)
                pairs.append((variant, 1))
            with CaptureQueriesContext(connection) as ctx:
                self._reserve(*pairs)
            return len([q for q in ctx.captured_queries if "purchases_" in q["sql"]])

        self.assertEqual(purchases_queries(1), purchases_queries(3))


class ReallocateIncomingTests(_ReserveOrderItemsBase):
    """reallocate_incoming_allocation (spec §5.6): physical first, then
    other confirmed incoming lines; all or nothing; never cancels orders."""

    def _allocate(self, variant, quantity):
        [reservation] = reservation_service.reserve_order_items(
            self._items((variant, quantity)), self.warehouse, allow_incoming=True,
        )
        return reservation.allocations.get(incoming_status="active")

    def _set_stock(self, variant, quantity):
        InventoryStock.objects.filter(
            variant=variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
        ).update(quantity=Decimal(quantity))

    def _reallocate(self, allocation):
        return reservation_service.reallocate_incoming_allocation(allocation, self.buyer)

    def test_reallocates_to_free_physical_stock(self):
        variant = self._variant("P2C-RA-PHY", stock=0)
        line = self._book(variant, 3)
        allocation = self._allocate(variant, 2)
        self._set_stock(variant, 5)
        self._reallocate(allocation)
        allocation.refresh_from_db()
        self.assertEqual(allocation.incoming_status, "reallocated")
        self.assertIsNotNone(allocation.incoming_resolved_at)
        self.assertIsNone(allocation.converted_by_receipt_line)
        self.assertEqual(allocation.replacement.allocation_type, "retail_unit")
        self.assertEqual(allocation.replacement.units, Decimal("2"))
        self.assertEqual(self._stock(variant).quantity_reserved, Decimal("2"))
        self.assertFalse(self._incoming_rows(incoming_status="active").exists())
        self.assertFalse(self._incoming_rows(purchase_order_line=line, incoming_status="active").exists())
        reservation_service.consume_reservation(allocation.reservation)
        self.assertEqual(self._stock(variant).quantity, Decimal("3"))

    def test_reallocates_to_other_po_line(self):
        variant = self._variant("P2C-RA-PO", stock=0)
        self._book(variant, 3)
        allocation = self._allocate(variant, 2)
        other = self._book(variant, 2)
        self._reallocate(allocation)
        allocation.refresh_from_db()
        replacement = allocation.replacement
        self.assertEqual(allocation.incoming_status, "reallocated")
        self.assertEqual(
            (replacement.purchase_order_line, replacement.units, replacement.incoming_status),
            (other, Decimal("2"), "active"),
        )
        self.assertIsNone(replacement.split_from)

    def test_mixed_physical_then_incoming_uses_split_from(self):
        variant = self._variant("P2C-RA-MIX", stock=0)
        self._book(variant, 3)
        allocation = self._allocate(variant, 3)
        self._set_stock(variant, 1)
        other = self._book(variant, 5)
        self._reallocate(allocation)
        allocation.refresh_from_db()
        self.assertEqual(allocation.replacement.allocation_type, "retail_unit")
        self.assertEqual(allocation.replacement.units, Decimal("1"))
        [piece] = allocation.split_remainders.all()
        self.assertEqual(
            (piece.purchase_order_line, piece.units, piece.incoming_status),
            (other, Decimal("2"), "active"),
        )
        self.assertEqual(self._stock(variant).quantity_reserved, Decimal("1"))

    def test_multiple_incoming_sources_follow_priority(self):
        variant = self._variant("P2C-RA-PRI", stock=0)
        self._book(variant, 2, expected_date=datetime.date(2026, 10, 1))
        allocation = self._allocate(variant, 2)
        late = self._book(variant, 1, expected_date=datetime.date(2026, 12, 1))
        early = self._book(variant, 1, expected_date=datetime.date(2026, 11, 1))
        self._reallocate(allocation)
        allocation.refresh_from_db()
        self.assertEqual(allocation.replacement.purchase_order_line, early)
        [piece] = allocation.split_remainders.all()
        self.assertEqual((piece.purchase_order_line, piece.split_from), (late, allocation))

    def test_impossible_reallocation_changes_nothing(self):
        variant = self._variant("P2C-RA-NONE", stock=0)
        self._book(variant, 5)
        allocation = self._allocate(variant, 2)
        self._set_stock(variant, 1)
        before = StockReservationAllocation.objects.count()
        with self.assertRaises(reservation_service.InsufficientStockError) as ctx:
            self._reallocate(allocation)
        self.assertIn("1 of 2", str(ctx.exception))
        allocation.refresh_from_db()
        self.assertEqual(allocation.incoming_status, "active")
        self.assertEqual(StockReservationAllocation.objects.count(), before)
        self.assertEqual(self._stock(variant).quantity_reserved, Decimal("0"))
        allocation.reservation.refresh_from_db()
        self.assertEqual(allocation.reservation.status, StockReservation.STATUS_HELD)

    def test_current_line_is_never_a_source(self):
        variant = self._variant("P2C-RA-SELF", stock=0)
        self._book(variant, 10)
        allocation = self._allocate(variant, 2)
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reallocate(allocation)

    def test_closed_or_other_warehouse_po_is_not_a_source(self):
        variant = self._variant("P2C-RA-CLOSED", stock=0)
        self._book(variant, 2)
        allocation = self._allocate(variant, 2)
        self._book(variant, 5, status=PurchaseOrder.STATUS_CLOSED)
        self._book(variant, 5, warehouse=Warehouse.objects.create(name="P2C Other"))
        with self.assertRaises(reservation_service.InsufficientStockError):
            self._reallocate(allocation)

    def test_only_active_incoming_rows_can_be_reallocated(self):
        variant = self._variant("P2C-RA-STATE", stock=5)
        self._book(variant, 3)
        [reservation] = reservation_service.reserve_order_items(
            self._items((variant, 1)), self.warehouse, allow_incoming=True,
        )
        physical = reservation.allocations.get()
        with self.assertRaises(reservation_service.InvalidReservationStateError):
            self._reallocate(physical)

        other = self._variant("P2C-RA-STATE-B", stock=0)
        self._book(other, 3)
        incoming = self._allocate(other, 1)
        reservation_service.release_reservation(incoming.reservation)
        with self.assertRaises(reservation_service.InvalidReservationStateError):
            self._reallocate(incoming)


class ReallocateIncomingAdminTests(_ReserveOrderItemsBase):
    """Staff confirmation page for reallocate_incoming_allocation. GET never
    changes state; POST needs inventory.reallocate_incoming_allocation."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser(username="p2c-admin", email="p2c-admin@example.com", password="x")
        self.variant = self._variant("P2C-RA-ADMIN", stock=0)
        self._book(self.variant, 3)
        [reservation] = reservation_service.reserve_order_items(
            self._items((self.variant, 2)), self.warehouse, allow_incoming=True,
        )
        self.allocation = reservation.allocations.get()
        self.url = reverse(
            "admin:inventory_stockreservationallocation_reallocate", args=[self.allocation.pk]
        )

    def _status(self):
        self.allocation.refresh_from_db()
        return self.allocation.incoming_status

    def test_permission_exists(self):
        self.assertTrue(
            Permission.objects.filter(
                codename="reallocate_incoming_allocation", content_type__app_label="inventory",
            ).exists()
        )

    def test_get_shows_page_without_change(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reallocate")
        self.assertEqual(self._status(), "active")

    def test_post_reallocates_and_returns_to_order(self):
        InventoryStock.objects.filter(variant=self.variant).update(quantity=Decimal("5"))
        self.client.force_login(self.staff)
        response = self.client.post(self.url)
        order = self.allocation.reservation.order_item.order
        self.assertRedirects(
            response, reverse("admin:orders_order_change", args=[order.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self._status(), "reallocated")

    def test_failure_shows_uncovered_quantity_and_changes_nothing(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "2 of 2 unit(s)")
        self.assertEqual(self._status(), "active")

    def test_staff_without_permission_is_refused(self):
        clerk = User.objects.create_user(
            username="p2c-clerk", email="p2c-clerk@example.com", password="x", is_staff=True,
        )
        clerk.user_permissions.add(
            Permission.objects.get(codename="view_stockreservationallocation")
        )
        InventoryStock.objects.filter(variant=self.variant).update(quantity=Decimal("5"))
        self.client.force_login(clerk)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(self._status(), "active")

    def test_order_breakdown_links_active_incoming_rows(self):
        order = self.allocation.reservation.order_item.order
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin:orders_order_change", args=[order.pk]))
        self.assertContains(response, self.url)
