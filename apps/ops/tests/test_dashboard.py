"""Tests for the Smerfume Panel Home dashboard (DEC-019)."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import InventoryStock, StockReservation, StockReservationAllocation, Supplier, Warehouse
from apps.ops import dashboard_selectors
from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.ops.views import home
from apps.orders.models import Order
from apps.purchases import selectors as purchase_selectors
from apps.purchases import services as purchase_services
from apps.purchases.models import PaymentAccount, PurchaseOrder, PurchaseOrderLine

from .test_ops import _order, _pay, _staff, _variant

User = get_user_model()
HOME_URL = reverse("ops:home")
ORDER_LIST_URL = reverse("ops:order_list")
STOCK_LIST_URL = reverse("ops:stock_list")
RESERVATION_LIST_URL = reverse("ops:reservation_list")
PO_LIST_URL = reverse("ops:po_list")
DOMAIN_TABLES = ('"orders_', '"inventory_', '"purchases_', '"catalog_')

# Every gated unit on Home: (selector name, permissions).
GATED = [(w.selector, w.permissions) for w in home.WIDGETS] + [(e.selector, e.permissions) for e in home.EXCEPTIONS]
ALL_PERMISSIONS = sorted({code for _, codes in GATED for code in codes})


def _domain_queries(ctx):
    return [q["sql"] for q in ctx.captured_queries if any(table in q["sql"] for table in DOMAIN_TABLES)]


class _SpyMixin:
    """Wrap every Home selector so tests see which ones ran."""

    def spy(self):
        self.stop_spies()
        self._patchers = [
            mock.patch.object(dashboard_selectors, name, wraps=getattr(dashboard_selectors, name))
            for name in sorted({selector for selector, _ in GATED})
        ]
        self.addCleanup(self.stop_spies)
        return {patcher.attribute: patcher.start() for patcher in self._patchers}

    def stop_spies(self):
        for patcher in getattr(self, "_patchers", []):
            patcher.stop()
        self._patchers = []


class PermissionQueryTests(_SpyMixin, TestCase):
    """Hard acceptance criterion: no permission, no render and no query."""

    def test_no_widget_permissions_runs_no_selector_and_no_domain_query(self):
        spies = self.spy()
        self.client.force_login(_staff("panel_only", OPS_ACCESS_PERMISSION))
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(HOME_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([name for name, spy in spies.items() if spy.called], [])
        self.assertEqual(_domain_queries(ctx), [])
        self.assertEqual(response.context["widgets"], {})
        self.assertEqual(response.context["quick_actions"], [])
        for marker in ("data-widget=", "Exceptions", "Quick actions"):
            self.assertNotContains(response, marker)

    def test_each_permission_set_runs_exactly_its_widgets(self):
        for _, permissions in GATED:
            with self.subTest(permissions=permissions):
                spies = self.spy()
                user = _staff(f"u{User.objects.count()}", OPS_ACCESS_PERMISSION, *permissions)
                self.client.force_login(user)
                response = self.client.get(HOME_URL)
                self.assertEqual(response.status_code, 200)
                expected = {name for name, codes in GATED if set(codes) <= set(permissions)}
                called = {name for name, spy in spies.items() if spy.called}
                self.assertEqual(called, expected)
                allowed_widgets = {w.key for w in home.WIDGETS if set(w.permissions) <= set(permissions)}
                self.assertEqual(set(response.context["widgets"]), allowed_widgets)
                self.stop_spies()

    def test_stock_without_po_permission_shows_no_incoming_and_no_purchase_query(self):
        self.client.force_login(_staff("stockonly", OPS_ACCESS_PERMISSION, home.VIEW_STOCK))
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(HOME_URL)
        self.assertContains(response, 'data-widget="stock_health"')
        self.assertNotContains(response, 'data-widget="stock_incoming"')
        self.assertFalse([q for q in ctx.captured_queries if '"purchases_' in q["sql"]])

    def test_all_permissions_render_every_section(self):
        self.client.force_login(_staff("allperms", OPS_ACCESS_PERMISSION, *ALL_PERMISSIONS))
        response = self.client.get(HOME_URL)
        for marker in (
            "sales_today", "orders_today", "collected_today", "in_store_balance_due", "to_fulfil",
            "stock_health", "stock_incoming", "vendor_outstanding", "order_value_chart", "exceptions",
            "oldest_outstanding", "waiting_to_ship",
        ):
            self.assertContains(response, f'data-widget="{marker}"')

    def test_quick_actions_follow_target_permissions(self):
        self.client.force_login(_staff(
            "cashier", OPS_ACCESS_PERMISSION, "orders.view_order", "orders.add_orderpayment", "orders.add_order",
        ))
        actions = {action["key"]: action["url"] for action in self.client.get(HOME_URL).context["quick_actions"]}
        self.assertEqual(set(actions), {"in_store_sale", "record_payment"})
        self.assertEqual(actions["record_payment"], f"{ORDER_LIST_URL}?channel=in_store&payment_status=outstanding")


class WidgetValueTests(TestCase):
    def setUp(self):
        self.staff = _staff("dashboss", OPS_ACCESS_PERMISSION, *ALL_PERMISSIONS)
        self.client.force_login(self.staff)

    def test_sales_today_is_order_value_excluding_cancelled_split_by_channel(self):
        _order(1, total="1000.00", channel=Order.CHANNEL_IN_STORE)
        _order(2, total="500.00", channel=Order.CHANNEL_ONLINE, status=Order.STATUS_CONFIRMED)
        _order(3, total="9000.00", status=Order.STATUS_CANCELLED)
        old = _order(4, total="700.00")
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))

        data = dashboard_selectors.order_value_today()
        self.assertEqual(data["total"], Decimal("1500.00"))
        self.assertEqual(data["count"], 2)
        by_channel = {channel["key"]: channel["total"] for channel in data["channels"]}
        self.assertEqual(
            by_channel, {Order.CHANNEL_ONLINE: Decimal("500.00"), Order.CHANNEL_IN_STORE: Decimal("1000.00")},
        )

        chart = dashboard_selectors.order_value_chart()
        self.assertEqual(len(chart["days"]), 30)
        self.assertEqual(chart["total"], Decimal("2200.00"))
        self.assertEqual(chart["days"][-1]["total"], Decimal("1500.00"))
        self.assertEqual(chart["days"][-1]["height"], 100)

        response = self.client.get(HOME_URL)
        self.assertContains(response, "Order value, 30 days")
        self.assertNotContains(response, "Net sales")
        self.assertNotContains(response, "Net Sales")

    def test_balance_due_counts_in_store_only_and_ignores_online_without_cpay(self):
        unpaid = _order(10, total="1000.00")
        partial = _order(11, total="800.00")
        _pay(partial, "300.00", self.staff)
        paid = _order(12, total="200.00")
        _pay(paid, "200.00", self.staff)
        _order(13, total="5000.00", channel=Order.CHANNEL_ONLINE, status=Order.STATUS_CONFIRMED)

        self.assertEqual(dashboard_selectors.in_store_balance_due(), {"total": Decimal("1500.00"), "count": 2})
        self.assertEqual(dashboard_selectors.collected_today()["total"], Decimal("500.00"))
        rows = dashboard_selectors.oldest_outstanding_in_store_orders()
        self.assertEqual([order.pk for order in rows], [unpaid.pk, partial.pk])

    def test_to_fulfil_and_waiting_to_ship(self):
        confirmed = _order(20, status=Order.STATUS_CONFIRMED, items=2)
        processing = _order(21, status=Order.STATUS_PROCESSING)
        _order(22, status=Order.STATUS_SHIPPED)
        self.assertEqual(dashboard_selectors.to_fulfil()["count"], 2)
        rows = dashboard_selectors.orders_waiting_to_ship()
        self.assertEqual([(order.pk, order.item_count) for order in rows], [(confirmed.pk, 2), (processing.pk, 1)])

    def test_zero_count_exceptions_are_hidden(self):
        response = self.client.get(HOME_URL)
        self.assertEqual(response.context["exceptions"], [])
        self.assertContains(response, "Nothing needs action right now.")
        _order(30, channel=Order.CHANNEL_ONLINE, status=Order.STATUS_PENDING, payment_method=Order.PAYMENT_METHOD_COD)
        rows = {row["key"]: row for row in self.client.get(HOME_URL).context["exceptions"]}
        self.assertEqual(set(rows), {"cod_unverified"})
        self.assertEqual(rows["cod_unverified"]["url"], f"{ORDER_LIST_URL}?cod_unverified=on")


class PurchaseWidgetTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="dashowner", email="dashowner@example.com", password="x")
        self.supplier = Supplier.objects.create(name="Dash Vendor")
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.variant = _variant()
        self.account = PaymentAccount.objects.create(name="Dash Bank", kind=PaymentAccount.KIND_BANK)

    def _po(self, issue=True, quantity=1, price="1000", variant=None, **kwargs):
        kwargs.setdefault("purchase_tax_treatment", PurchaseOrder.TAX_NO_GST_INVOICE)
        po = PurchaseOrder.objects.create(
            supplier=self.supplier, warehouse=self.warehouse, created_by=self.owner, **kwargs,
        )
        PurchaseOrderLine.objects.create(
            purchase_order=po, variant=variant or self.variant, quantity_ordered=quantity, unit_price=Decimal(price),
        )
        if issue:
            purchase_services.issue_purchase_order(po, self.owner)
            po.refresh_from_db()
        return po

    def test_vendor_outstanding_reuses_payable_statuses_and_payment_summary(self):
        paid_part = self._po(price="1000")
        purchase_services.record_po_payment(
            paid_part, Decimal("400.00"), timezone.localdate(), "upi", "UTR-9", "", self.owner,
            paid_through=self.account,
        )
        self._po(price="250")
        self._po(issue=False, price="9999")  # draft: not payable
        cancelled = self._po(price="7777")
        PurchaseOrder.objects.filter(pk=cancelled.pk).update(status=PurchaseOrder.STATUS_CANCELLED)
        unclassified = self._po(issue=False, price="50", purchase_tax_treatment=None)
        PurchaseOrder.objects.filter(pk=unclassified.pk).update(status=PurchaseOrder.STATUS_ISSUED)

        data = dashboard_selectors.vendor_outstanding()
        self.assertEqual(data, {"total": Decimal("850.00"), "count": 2, "unclassified": 1})
        self.assertEqual(paid_part.remaining_against_po, Decimal("600.00"))

        # The PO list drill-down lists exactly the POs the card counts.
        self.client.force_login(self.owner)
        response = self.client.get(PO_LIST_URL, {"payment_status": "outstanding"})
        self.assertEqual(response.status_code, 200)
        listed = {po.pk for po in response.context["object_list"]}
        self.assertEqual(listed, set(purchase_selectors.outstanding_purchase_orders().values_list("pk", flat=True)))
        self.assertEqual(len(listed), data["count"])
        home_response = self.client.get(HOME_URL)
        self.assertContains(home_response, f'href="{PO_LIST_URL}?payment_status=outstanding"')
        self.assertNotContains(home_response, "payment_status=unpaid")

    def test_low_stock_without_incoming_and_filter(self):
        covered_variant = _variant()
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail", quantity=2, reorder_level=5,
        )
        covered = InventoryStock.objects.create(
            variant=covered_variant, warehouse=self.warehouse, stock_type="retail", quantity=1, reorder_level=5,
        )
        po = self._po(variant=covered_variant, quantity=3)
        PurchaseOrderLine.objects.filter(purchase_order=po).update(confirmed_booked_quantity=3)

        self.assertEqual(dashboard_selectors.stock_health(), {"low": 2, "out": 0})
        self.assertEqual(dashboard_selectors.stock_incoming(), {"units": Decimal("3"), "low_with_incoming": 1})
        self.assertEqual(dashboard_selectors.low_stock_without_incoming(), {"count": 1})

        viewer = _staff("stockpo", OPS_ACCESS_PERMISSION, home.VIEW_STOCK, home.VIEW_PO)
        self.client.force_login(viewer)
        rows = self.client.get(STOCK_LIST_URL, {"low_stock": "on", "no_incoming": "on"}).context["object_list"]
        self.assertNotIn(covered, list(rows))
        self.assertEqual(len(rows), 1)
        # Without the PO permission the filter is not offered and not applied.
        self.client.force_login(_staff("stocknopo", OPS_ACCESS_PERMISSION, home.VIEW_STOCK))
        response = self.client.get(STOCK_LIST_URL, {"low_stock": "on", "no_incoming": "on"})
        self.assertEqual(len(response.context["object_list"]), 2)
        self.assertNotContains(response, 'name="no_incoming"')

    def test_booked_incoming_mismatch(self):
        po = self._po(quantity=2)
        line = po.lines.get()
        PurchaseOrderLine.objects.filter(pk=line.pk).update(confirmed_booked_quantity=1)
        order = _order(40, status=Order.STATUS_CONFIRMED)
        reservation = StockReservation.objects.create(
            order_item=order.items.get(), variant=self.variant, warehouse=self.warehouse,
            purpose=StockReservation.PURPOSE_DIRECT_SALE, quantity=Decimal("2"),
        )
        self.assertEqual(dashboard_selectors.booked_incoming_mismatches()["count"], 0)
        StockReservationAllocation.objects.create(
            reservation=reservation, allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            purchase_order_line=line, units=Decimal("2"), incoming_status=StockReservationAllocation.INCOMING_ACTIVE,
        )
        data = dashboard_selectors.booked_incoming_mismatches()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["purchase_orders"], [po])

    def test_overdue_purchase_orders(self):
        late = self._po(expected_date=timezone.localdate())
        PurchaseOrder.objects.filter(pk=late.pk).update(expected_date=timezone.localdate() - timedelta(days=1))
        self._po(expected_date=timezone.localdate() + timedelta(days=1))
        self.assertEqual(dashboard_selectors.overdue_purchase_orders(), {"count": 1})


class DrillDownFilterTests(TestCase):
    def setUp(self):
        self.staff = _staff(
            "filters", OPS_ACCESS_PERMISSION, "orders.view_order", "inventory.view_stockreservation",
        )
        self.client.force_login(self.staff)

    def _numbers(self, params):
        response = self.client.get(ORDER_LIST_URL, params)
        self.assertEqual(response.status_code, 200)
        return {row["order"].order_number for row in response.context["rows"]}

    def test_outstanding_in_store_lists_unpaid_and_partially_paid_only(self):
        unpaid = _order(50)
        partial = _order(51, total="800.00")
        _pay(partial, "100.00", self.staff)
        paid = _order(52, total="100.00")
        _pay(paid, "100.00", self.staff)
        _order(53, channel=Order.CHANNEL_ONLINE, status=Order.STATUS_CONFIRMED)
        numbers = self._numbers({"channel": "in_store", "payment_status": "outstanding"})
        self.assertEqual(numbers, {unpaid.order_number, partial.order_number})

    def test_date_range_and_invalid_range(self):
        today = _order(60)
        old = _order(61)
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=5))
        iso = timezone.localdate().isoformat()
        self.assertEqual(self._numbers({"date_from": iso, "date_to": iso}), {today.order_number})
        response = self.client.get(ORDER_LIST_URL, {"date_from": iso, "date_to": "2000-01-01"})
        self.assertContains(response, "The end date is before the start date.")
        self.assertEqual(len(response.context["rows"]), 1)  # date_to ignored, date_from applied

    def test_sales_today_drill_down_matches_kpi_population(self):
        kept = _order(90, status=Order.STATUS_CONFIRMED)
        _order(91, status=Order.STATUS_CANCELLED)
        old = _order(92)
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        iso = timezone.localdate().isoformat()
        numbers = self._numbers({"date_from": iso, "date_to": iso, "exclude_cancelled": "on"})
        self.assertEqual(numbers, {kept.order_number})
        self.assertEqual(len(numbers), dashboard_selectors.order_value_today()["count"])

        response = self.client.get(HOME_URL)
        url = f"{ORDER_LIST_URL}?date_from={iso}&amp;date_to={iso}&amp;exclude_cancelled=on"
        self.assertContains(response, url, count=2)  # Sales today + Orders today cards

    def test_collected_today_card_is_not_a_link(self):
        self.client.force_login(_staff("cpay", OPS_ACCESS_PERMISSION, "orders.view_orderpayment"))
        response = self.client.get(HOME_URL)
        self.assertContains(response, '<div class="kpi" data-widget="collected_today">')
        self.assertNotContains(response, 'data-widget="collected_today" href')
        self.assertNotRegex(response.content.decode(), r'<a[^>]*data-widget="collected_today"')

    def test_cod_unverified(self):
        cod = _order(70, channel=Order.CHANNEL_ONLINE, status=Order.STATUS_PENDING,
                     payment_method=Order.PAYMENT_METHOD_COD)
        _order(71, channel=Order.CHANNEL_ONLINE, status=Order.STATUS_CONFIRMED,
               payment_method=Order.PAYMENT_METHOD_COD, cod_verified_at=timezone.now())
        _order(72, channel=Order.CHANNEL_ONLINE, status=Order.STATUS_PENDING,
               payment_method=Order.PAYMENT_METHOD_PREPAID)
        self.assertEqual(self._numbers({"cod_unverified": "on"}), {cod.order_number})

    def test_reservation_warning_age_uses_setting_and_never_changes_reservations(self):
        order = _order(80, status=Order.STATUS_CONFIRMED)
        reservation = StockReservation.objects.create(
            order_item=order.items.get(), variant=order.items.get().variant,
            warehouse=Warehouse.objects.get(is_default=True),
            purpose=StockReservation.PURPOSE_DIRECT_SALE, quantity=Decimal("1"),
        )
        StockReservation.objects.filter(pk=reservation.pk).update(created_at=timezone.now() - timedelta(hours=30))
        with override_settings(OPS_DASHBOARD_RESERVATION_WARNING_HOURS=24):
            self.assertEqual(dashboard_selectors.reservations_past_warning_age(), {"count": 1})
            rows = self.client.get(RESERVATION_LIST_URL, {"past_warning_age": "on"}).context["object_list"]
            self.assertEqual(list(rows), [reservation])
        with override_settings(OPS_DASHBOARD_RESERVATION_WARNING_HOURS=48):
            self.assertEqual(dashboard_selectors.reservations_past_warning_age(), {"count": 0})
            response = self.client.get(RESERVATION_LIST_URL, {"past_warning_age": "on"})
            self.assertEqual(list(response.context["object_list"]), [])
            self.assertContains(response, "Past warning age (48 h)")
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, StockReservation.STATUS_HELD)

    def test_home_links_use_outstanding_filter(self):
        self.client.force_login(_staff("links", OPS_ACCESS_PERMISSION, "orders.view_order"))
        response = self.client.get(HOME_URL)
        url = f"{ORDER_LIST_URL}?channel=in_store&amp;payment_status=outstanding"
        self.assertContains(response, url, count=2)  # balance due card + table "View all"
