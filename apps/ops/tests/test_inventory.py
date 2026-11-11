"""Smerfume Panel Inventory group: Stock, Partial Bottles, Stock Movements,
Reservations and Warehouses. Writes go through apps.inventory.services."""

import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import (
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockReservationAllocation,
    StockTransaction,
    Warehouse,
)
from apps.inventory.services import adjustment as adjustment_services
from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.ops.tests.test_ops import PASSWORD, _order, _perm, _variant

User = get_user_model()

STOCK_URL = reverse("ops:stock_list")
LOTS_URL = reverse("ops:partial_lot_list")
MOVES_URL = reverse("ops:movement_list")
RES_URL = reverse("ops:reservation_list")
WH_URL = reverse("ops:warehouse_list")


def _user(username, *codes):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password=PASSWORD)
    user.user_permissions.add(*(_perm(code) for code in (OPS_ACCESS_PERMISSION, *codes)))
    return user


def _warehouse():
    return Warehouse.objects.get(is_default=True)


def _stock(quantity="10", stock_type=InventoryStock.STOCK_TYPE_RETAIL, variant=None, **extra):
    return InventoryStock.objects.create(
        variant=variant or _variant(), warehouse=_warehouse(), stock_type=stock_type,
        quantity=Decimal(quantity), **extra,
    )


def _lot(remaining="80", reserved="0", variant=None):
    opened = StockTransaction.objects.create(transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED)
    return PartialBottleLot.objects.create(
        variant=variant or _variant(), warehouse=_warehouse(), remaining_ml=Decimal(remaining),
        reserved_ml=Decimal(reserved), opened_at=timezone.now(), source_transaction=opened,
    )


class InventoryNavigationTests(TestCase):
    def test_inventory_pages_live_and_others_placeholders(self):
        root = User.objects.create_superuser(username="invroot", email="invroot@example.com", password=PASSWORD)
        self.client.force_login(root)
        response = self.client.get(STOCK_URL)
        groups = {g["key"]: g for g in response.context["ops_nav"]["groups"]}
        inventory = groups["inventory"]
        self.assertTrue(inventory["open"])
        self.assertEqual(
            [(i["key"], i["implemented"], i["url"]) for i in inventory["items"]],
            [("stock", True, STOCK_URL), ("partial_bottles", True, LOTS_URL),
             ("stock_movements", True, MOVES_URL), ("reservations", True, RES_URL),
             ("warehouses", True, WH_URL)],
        )
        html = response.content.decode()
        self.assertNotIn('data-coming-soon="stock"', html)

    def test_each_page_needs_its_view_permission(self):
        stock = _stock()
        lot = _lot()
        pages = {
            "inventory.view_inventorystock": [STOCK_URL, reverse("ops:stock_detail", args=[stock.public_id])],
            "inventory.view_partialbottlelot": [LOTS_URL, reverse("ops:partial_lot_detail", args=[lot.public_id])],
            "inventory.view_stockmovement": [MOVES_URL],
            "inventory.view_stockreservation": [RES_URL],
            "inventory.view_warehouse": [WH_URL],
        }
        self.client.force_login(_user("nothing", "orders.view_order"))
        for urls in pages.values():
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403, url)
        for index, (perm, urls) in enumerate(pages.items()):
            self.client.force_login(_user(f"viewer{index}", perm))
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200, url)


class StockPageTests(TestCase):
    def setUp(self):
        self.stock = _stock("10", reorder_level=Decimal("12"))
        self.other = _stock("50")
        self.url = reverse("ops:stock_detail", args=[self.stock.public_id])
        self.manager = _user(
            "stockmgr", "inventory.view_inventorystock", "inventory.change_inventorystock",
            "inventory.transfer_stock_type", "inventory.view_stockmovement",
        )
        self.client.force_login(self.manager)

    def test_list_filters_and_public_ids(self):
        def rows(**params):
            return {s.pk for s in self.client.get(STOCK_URL, params).context["object_list"]}

        self.assertEqual(rows(), {self.stock.pk, self.other.pk})
        self.assertEqual(rows(low_stock="on"), {self.stock.pk})
        self.assertEqual(rows(q=self.other.variant.sku), {self.other.pk})
        self.assertEqual(rows(warehouse=str(_warehouse().public_id)), {self.stock.pk, self.other.pk})
        self.assertEqual(rows(stock_type="tester"), set())
        html = self.client.get(STOCK_URL).content.decode()
        self.assertIn(f"/ops/inventory/stock/{self.stock.public_id}/", html)
        self.assertIsNone(re.search(rf'href="/ops/inventory/stock/{self.stock.pk}/', html))
        self.assertEqual(self.client.get(f"/ops/inventory/stock/{self.stock.pk}/").status_code, 404)

    def test_adjust_through_service_records_movement(self):
        with patch.object(
            adjustment_services, "adjust_inventory_stock", wraps=adjustment_services.adjust_inventory_stock,
        ) as service:
            response = self.client.post(
                reverse("ops:stock_adjust", args=[self.stock.public_id]),
                {"quantity_delta": "-3", "reason": StockMovement.REASON_DAMAGED_GOODS, "notes": "Broken"},
            )
        self.assertRedirects(response, self.url)
        service.assert_called_once()
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("7"))
        movement = StockMovement.objects.get(variant=self.stock.variant)
        self.assertEqual((movement.quantity_delta, movement.performed_by), (Decimal("-3"), self.manager))
        self.assertContains(self.client.get(self.url), "Broken")

    def test_adjust_refusals_change_nothing(self):
        adjust = reverse("ops:stock_adjust", args=[self.stock.public_id])
        for data in (
            {"quantity_delta": "-11", "reason": StockMovement.REASON_DAMAGED_GOODS},  # below zero
            {"quantity_delta": "0", "reason": StockMovement.REASON_DAMAGED_GOODS},
            {"quantity_delta": "5", "reason": StockMovement.REASON_PURCHASE},  # receipts only
            {"quantity_delta": "5", "reason": StockMovement.REASON_OPENING_STOCK},
        ):
            self.client.post(adjust, data)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("10"))
        self.assertFalse(StockMovement.objects.exists())

    def test_reorder_level_through_service(self):
        url = reverse("ops:stock_reorder_level", args=[self.stock.public_id])
        self.client.post(url, {"reorder_level": "4"})
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.reorder_level, Decimal("4"))
        self.client.post(url, {"reorder_level": ""})
        self.stock.refresh_from_db()
        self.assertIsNone(self.stock.reorder_level)
        self.client.post(url, {"reorder_level": "-1"})
        self.stock.refresh_from_db()
        self.assertIsNone(self.stock.reorder_level)
        self.assertFalse(StockMovement.objects.exists())  # configuration only

    def test_transfer_through_service(self):
        response = self.client.post(reverse("ops:stock_transfer", args=[self.stock.public_id]), {
            "to_type": InventoryStock.STOCK_TYPE_TESTER, "quantity": "2",
            "reason": StockMovement.REASON_MARKETING_USE, "notes": "",
        })
        self.assertRedirects(response, self.url)
        self.stock.refresh_from_db()
        tester = InventoryStock.objects.get(variant=self.stock.variant, stock_type=InventoryStock.STOCK_TYPE_TESTER)
        self.assertEqual((self.stock.quantity, tester.quantity), (Decimal("8"), Decimal("2")))

    def test_damaged_stock_offers_no_transfer(self):
        damaged = _stock("3", stock_type=InventoryStock.STOCK_TYPE_DAMAGED)
        page = self.client.get(reverse("ops:stock_detail", args=[damaged.public_id]))
        self.assertIsNone(page.context["transfer_form"])
        self.client.post(reverse("ops:stock_transfer", args=[damaged.public_id]), {
            "to_type": InventoryStock.STOCK_TYPE_RETAIL, "quantity": "1",
            "reason": StockMovement.REASON_MARKETING_USE,
        })
        damaged.refresh_from_db()
        self.assertEqual(damaged.quantity, Decimal("3"))

    def test_view_only_user_gets_no_forms_and_403_on_post(self):
        self.client.force_login(_user("stockview", "inventory.view_inventorystock"))
        page = self.client.get(self.url)
        for key in ("adjust_form", "reorder_form", "transfer_form"):
            self.assertIsNone(page.context[key], key)
        for name, data in (
            ("ops:stock_adjust", {"quantity_delta": "1", "reason": StockMovement.REASON_STOCKTAKING_RESULTS}),
            ("ops:stock_reorder_level", {"reorder_level": "1"}),
            ("ops:stock_transfer", {"to_type": "tester", "quantity": "1", "reason": "marketing_use"}),
        ):
            self.assertEqual(self.client.post(reverse(name, args=[self.stock.public_id]), data).status_code, 403)
        self.stock.refresh_from_db()
        self.assertEqual((self.stock.quantity, self.stock.reorder_level), (Decimal("10"), Decimal("12")))

    def test_list_query_count_is_flat(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(STOCK_URL).status_code, 200)
            return len(ctx.captured_queries)

        before = count()
        for _ in range(5):
            _stock("1")
        self.assertEqual(before, count())


class PartialLotPageTests(TestCase):
    def setUp(self):
        self.lot = _lot("80", reserved="10")
        self.depleted = _lot("0")
        PartialBottleLot.objects.filter(pk=self.depleted.pk).update(is_depleted=True)
        self.client.force_login(_user(
            "lotmgr", "inventory.view_partialbottlelot", "inventory.change_partialbottlelot",
        ))

    def test_list_state_filter(self):
        open_rows = self.client.get(LOTS_URL, {"state": "open"}).context["object_list"]
        self.assertEqual([lot.pk for lot in open_rows], [self.lot.pk])
        depleted_rows = self.client.get(LOTS_URL, {"state": "depleted"}).context["object_list"]
        self.assertEqual([lot.pk for lot in depleted_rows], [self.depleted.pk])

    def test_adjust_through_service(self):
        adjust = reverse("ops:partial_lot_adjust", args=[self.lot.public_id])
        self.client.post(adjust, {"ml_delta": "-5", "reason": StockMovement.REASON_STOCK_WRITTEN_OFF})
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.remaining_ml, Decimal("75"))
        self.assertEqual(StockMovement.objects.get().partial_lot, self.lot)
        # Below the reserved ml: refused by the service, nothing changes.
        self.client.post(adjust, {"ml_delta": "-70", "reason": StockMovement.REASON_STOCK_WRITTEN_OFF})
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.remaining_ml, Decimal("75"))
        page = self.client.get(reverse("ops:partial_lot_detail", args=[self.lot.public_id]))
        self.assertEqual(len(page.context["movements"]), 1)

    def test_adjust_needs_change_permission(self):
        self.client.force_login(_user("lotview", "inventory.view_partialbottlelot"))
        page = self.client.get(reverse("ops:partial_lot_detail", args=[self.lot.public_id]))
        self.assertIsNone(page.context["adjust_form"])
        response = self.client.post(
            reverse("ops:partial_lot_adjust", args=[self.lot.public_id]),
            {"ml_delta": "-5", "reason": StockMovement.REASON_STOCK_WRITTEN_OFF},
        )
        self.assertEqual(response.status_code, 403)


class MovementAndReservationTests(TestCase):
    def setUp(self):
        self.stock = _stock("10")
        adjustment_services.adjust_inventory_stock(
            self.stock, quantity_delta=Decimal("2"), reason=StockMovement.REASON_STOCKTAKING_RESULTS,
            notes="Count fix",
        )
        order = _order(800, variant=self.stock.variant)
        self.reservation = StockReservation.objects.create(
            order_item=order.items.get(), variant=self.stock.variant, warehouse=_warehouse(),
            purpose=StockReservation.PURPOSE_DIRECT_SALE, quantity=Decimal("1"),
        )
        self.order = order
        self.client.force_login(_user(
            "invviewer", "inventory.view_stockmovement", "inventory.view_stockreservation",
            "inventory.view_warehouse", "orders.view_order",
        ))

    def test_movement_list_is_read_only_and_filterable(self):
        response = self.client.get(MOVES_URL, {"q": "Count fix"})
        self.assertEqual(len(response.context["object_list"]), 1)
        self.assertEqual(len(self.client.get(MOVES_URL, {"reason": "sale"}).context["object_list"]), 0)
        self.assertEqual(self.client.post(MOVES_URL).status_code, 405)

    def test_reservation_list_and_detail(self):
        active = self.client.get(RES_URL, {"status": "active"}).context["object_list"]
        self.assertEqual(list(active), [self.reservation])
        found = self.client.get(RES_URL, {"q": self.order.order_number}).context["object_list"]
        self.assertEqual(list(found), [self.reservation])
        StockReservation.objects.filter(pk=self.reservation.pk).update(status=StockReservation.STATUS_RELEASED)
        self.assertEqual(list(self.client.get(RES_URL, {"status": "active"}).context["object_list"]), [])
        detail = self.client.get(reverse("ops:reservation_detail", args=[self.reservation.public_id]))
        self.assertContains(detail, reverse("ops:order_detail", args=[self.order.order_number]))

    def test_reallocate_needs_permission_and_incoming_allocation(self):
        allocation = StockReservationAllocation.objects.create(
            reservation=self.reservation, inventory_stock=self.stock,
            allocation_type=StockReservationAllocation.ALLOCATION_RETAIL_UNIT, units=Decimal("1"),
        )
        url = reverse("ops:allocation_reallocate", args=[allocation.public_id])
        self.assertEqual(self.client.post(url).status_code, 403)
        detail = self.client.get(reverse("ops:reservation_detail", args=[self.reservation.public_id]))
        self.assertFalse(detail.context["allocations"][0].can_reallocate)
        self.client.force_login(_user(
            "realloc", "inventory.view_stockreservation", "inventory.reallocate_incoming_allocation",
        ))
        self.assertEqual(self.client.post(url).status_code, 404)  # not an incoming allocation

    def test_warehouse_list_read_only(self):
        response = self.client.get(WH_URL)
        warehouse = response.context["object_list"][0]
        self.assertTrue(warehouse.is_default)
        self.assertEqual(warehouse.stock_rows, 1)
        self.assertNotContains(response, 'action="/ops/inventory/warehouses')


class SetReorderLevelServiceTests(TestCase):
    def test_sets_clears_and_refuses_negative(self):
        stock = _stock("1")
        adjustment_services.set_reorder_level(stock, Decimal("3"))
        stock.refresh_from_db()
        self.assertEqual(stock.reorder_level, Decimal("3"))
        adjustment_services.set_reorder_level(stock, None)
        stock.refresh_from_db()
        self.assertIsNone(stock.reorder_level)
        with self.assertRaises(adjustment_services.InventoryAdjustmentError):
            adjustment_services.set_reorder_level(stock, Decimal("-1"))
        self.assertFalse(StockMovement.objects.exists())
