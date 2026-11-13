"""Smerfume Panel Sales group: New In-store Sale, Returns and Refunds.

Every write goes through apps.orders.services; these tests check the panel
routes, permissions and that the outcome matches the domain rules."""

import re
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import InventoryStock, Warehouse
from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.ops.tests.test_ops import PASSWORD, _customer, _perm, _variant
from apps.orders import payments as order_payments
from apps.orders import services as order_services
from apps.orders.models import Order, OrderItem, OrderPayment, Refund, RefundAdjustment, Return, ReturnItem

User = get_user_model()

SALE_URL = reverse("ops:in_store_sale")
ITEMS_URL = reverse("ops:in_store_items")
RETURNS_URL = reverse("ops:return_list")
REFUNDS_URL = reverse("ops:refund_list")


def _user(username, *codes, is_staff=False):
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, is_staff=is_staff,
    )
    user.user_permissions.add(*(_perm(code) for code in (OPS_ACCESS_PERMISSION, *codes)))
    return user


def _stocked_variant(quantity=10, barcode=None):
    variant = _variant()
    if barcode:
        variant.barcode = barcode
        variant.save(update_fields=["barcode"])
    InventoryStock.objects.create(
        variant=variant, warehouse=Warehouse.objects.get(is_default=True),
        stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal(quantity),
    )
    return variant


def _delivered_online_order(n, variant, price="1000.00"):
    """A delivered online order inside the return window, with the
    checkout-time financial snapshot the refund calculation reads."""
    amount = Decimal(price)
    order = Order.objects.create(
        user=_customer(n), order_number=f"SMR-OPSR-{n:06d}", channel=Order.CHANNEL_ONLINE,
        status=Order.STATUS_DELIVERED, subtotal=amount, total=amount, delivered_at=timezone.now(),
    )
    OrderItem.objects.create(
        order=order, variant=variant, quantity=1, unit_price=amount, line_total=amount,
        gross_line_amount=amount, net_line_amount=amount, final_paid_line_amount=amount,
    )
    return order


def _sale_post(variant_ref, quantity=1, key=None, mobile="9812345670", extra_lines=()):
    data = {
        "customer_mobile": mobile, "customer_name": "Walk In", "discount_amount": "0",
        "warehouse": str(Warehouse.objects.get(is_default=True).public_id), "notes": "",
        "submission_key": str(key or uuid.uuid4()),
        "lines-TOTAL_FORMS": str(1 + len(extra_lines)), "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-sku": variant_ref, "lines-0-quantity": str(quantity),
    }
    for index, (ref, qty) in enumerate(extra_lines, start=1):
        data[f"lines-{index}-sku"] = ref
        data[f"lines-{index}-quantity"] = str(qty)
    return data


def _retail_qty(variant):
    return InventoryStock.objects.get(
        variant=variant, stock_type=InventoryStock.STOCK_TYPE_RETAIL,
        warehouse=Warehouse.objects.get(is_default=True),
    ).quantity


class InStoreSaleTests(TestCase):
    def setUp(self):
        self.staff = _user("counter", "orders.add_order", "orders.view_order")
        self.client.force_login(self.staff)
        self.variant = _stocked_variant(quantity=5, barcode="8900000000017")

    def test_permission_required(self):
        self.client.force_login(_user("noadd", "orders.view_order"))
        self.assertEqual(self.client.get(SALE_URL).status_code, 403)
        self.assertEqual(self.client.post(SALE_URL, _sale_post(self.variant.sku)).status_code, 403)
        self.assertEqual(self.client.get(ITEMS_URL, {"q": "Ops"}).status_code, 403)
        self.assertFalse(Order.objects.exists())

    def test_get_renders_without_db_ids(self):
        response = self.client.get(SALE_URL)
        self.assertEqual(response.status_code, 200)
        warehouse = Warehouse.objects.get(is_default=True)
        self.assertContains(response, f'<option value="{warehouse.public_id}"')
        self.assertNotRegex(response.content.decode(), rf'<option value="{warehouse.pk}"')
        self.assertTrue(response.context["ops_nav"]["groups"][0]["items"][1]["active"])

    def test_records_sale_through_service(self):
        with patch.object(
            order_services, "create_in_store_order", wraps=order_services.create_in_store_order,
        ) as service:
            response = self.client.post(SALE_URL, _sale_post(self.variant.sku, quantity=2))
        service.assert_called_once()
        order = Order.objects.get()
        self.assertRedirects(response, reverse("ops:order_detail", args=[order.order_number]))
        self.assertEqual(order.channel, Order.CHANNEL_IN_STORE)
        self.assertEqual(order.created_by, self.staff)
        self.assertEqual(order.total, Decimal("2000.00"))
        self.assertEqual(_retail_qty(self.variant), Decimal("3"))
        self.assertFalse(OrderPayment.objects.exists())  # DEC-013: no payment at sale time

    def test_barcode_is_accepted_as_item(self):
        response = self.client.post(SALE_URL, _sale_post("8900000000017"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().items.get().variant, self.variant)

    def test_same_submission_key_records_once(self):
        key = uuid.uuid4()
        first = self.client.post(SALE_URL, _sale_post(self.variant.sku, key=key))
        second = self.client.post(SALE_URL, _sale_post(self.variant.sku, key=key))
        order = Order.objects.get()
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(second["Location"], reverse("ops:order_detail", args=[order.order_number]))
        self.assertEqual(_retail_qty(self.variant), Decimal("4"))

    def test_invalid_input_records_nothing(self):
        cases = (
            _sale_post(self.variant.sku, quantity=9),  # more than the 5 in stock
            _sale_post("NO-SUCH-SKU"),
            _sale_post(self.variant.sku, mobile="12"),
        )
        for data in cases:
            response = self.client.post(SALE_URL, data)
            self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(_retail_qty(self.variant), Decimal("5"))

    def test_item_search_json(self):
        warehouse = Warehouse.objects.get(is_default=True)
        response = self.client.get(ITEMS_URL, {"q": "8900000000017", "warehouse": str(warehouse.public_id)})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual([(r["id"], r["sku"], r["stock"], r["exact"]) for r in rows],
                         [(self.variant.sku, self.variant.sku, 5, True)])
        self.assertNotIn(str(self.variant.pk), {str(v) for row in rows for v in row.values()} - {"5"})
        self.assertEqual(self.client.get(ITEMS_URL, {"q": "x", "warehouse": str(warehouse.pk)}).status_code, 400)
        refresh = self.client.get(ITEMS_URL, {"skus": self.variant.sku, "warehouse": str(warehouse.public_id)})
        self.assertEqual(refresh.json()["results"][0]["stock"], 5)


class ReturnWorkflowTests(TestCase):
    """Create -> approve -> in transit -> received -> inspect -> refund,
    all from the panel, each step through its service."""

    def setUp(self):
        self.variant = _stocked_variant(quantity=0)
        self.order = _delivered_online_order(1, self.variant)
        self.manager = _user(
            "returnsmgr", "orders.view_order", "orders.view_return", "orders.process_return",
            "orders.receive_return", "orders.change_returnitem", "orders.view_refund",
            "orders.process_refund",
        )
        self.client.force_login(self.manager)

    def _create_return(self):
        item = self.order.items.get()
        response = self.client.post(
            reverse("ops:order_create_return", args=[self.order.order_number]),
            {f"quantity_{item.pk}": "1", f"reason_{item.pk}": ReturnItem.REASON_WRONG_VARIANT},
        )
        return_request = Return.objects.get(order=self.order)
        self.assertRedirects(response, reverse("ops:return_detail", args=[return_request.public_id]))
        return return_request

    def _action(self, return_request, action):
        return self.client.post(reverse("ops:return_action", args=[return_request.public_id, action]))

    def test_full_return_to_refund(self):
        detail = self.client.get(reverse("ops:order_detail", args=[self.order.order_number]))
        self.assertTrue(detail.context["can_create_return"])
        return_request = self._create_return()
        self.assertEqual(return_request.status, Return.STATUS_REQUESTED)

        self._action(return_request, "approve")
        self._action(return_request, "in_transit")
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_IN_TRANSIT)

        # Received cannot be marked before quantities are recorded.
        self._action(return_request, "received")
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_IN_TRANSIT)

        line = return_request.items.get()
        self.client.post(
            reverse("ops:return_received_quantities", args=[return_request.public_id]),
            {f"received_{line.public_id}": "1"},
        )
        line.refresh_from_db()
        self.assertEqual(line.received_quantity, 1)
        self._action(return_request, "received")
        self._action(return_request, "start_inspection")
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_INSPECTION_PENDING)

        inspect_url = reverse("ops:return_item_inspect", args=[return_request.public_id, line.public_id])
        self.assertEqual(self.client.get(inspect_url).status_code, 200)
        response = self.client.post(inspect_url, {
            "received_quantity": "1", "disposition": ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            "resolution": ReturnItem.RESOLUTION_REFUND, "inspection_notes": "Sealed",
        })
        self.assertRedirects(response, reverse("ops:return_detail", args=[return_request.public_id]))
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_COMPLETED)
        self.assertEqual(_retail_qty(self.variant), Decimal("1"))  # restocked by the service

        response = self._action(return_request, "create_refund")
        refund = Refund.objects.get(return_request=return_request)
        self.assertRedirects(response, reverse("ops:refund_detail", args=[refund.public_id]))
        self.assertEqual(refund.refund_status, Refund.STATUS_PENDING)
        self.assertIsNone(refund.refund_method)
        self.assertEqual(refund.approved_by, self.manager)

    def test_action_needs_its_own_permission(self):
        return_request = self._create_return()
        viewer = _user("returnsview", "orders.view_return")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(RETURNS_URL).status_code, 200)
        detail = self.client.get(reverse("ops:return_detail", args=[return_request.public_id]))
        self.assertEqual(detail.context["actions"], [])
        self.assertEqual(self._action(return_request, "approve").status_code, 403)
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_REQUESTED)
        self.client.force_login(_user("noreturns", "orders.view_order"))
        self.assertEqual(self.client.get(RETURNS_URL).status_code, 403)

    def test_service_errors_are_reported_not_applied(self):
        return_request = self._create_return()
        response = self._action(return_request, "in_transit")  # not approved yet
        self.assertEqual(response.status_code, 302)
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_REQUESTED)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("Cannot mark in-transit" in m for m in messages))

    def test_in_store_order_has_no_create_return(self):
        order = Order.objects.create(
            user=_customer(9), order_number="SMR-OPSR-000009", channel=Order.CHANNEL_IN_STORE,
            status=Order.STATUS_DELIVERED, subtotal=Decimal("10"), total=Decimal("10"),
        )
        detail = self.client.get(reverse("ops:order_detail", args=[order.order_number]))
        self.assertFalse(detail.context["can_create_return"])
        self.assertNotContains(detail, "Create return")

    def test_return_list_filters_and_ids(self):
        return_request = self._create_return()
        response = self.client.get(RETURNS_URL, {"status": "requested", "q": self.order.order_number})
        self.assertEqual(list(response.context["object_list"]), [return_request])
        html = response.content.decode()
        self.assertIn(str(return_request.public_id), html)
        self.assertNotRegex(html, rf'href="/ops/returns/{return_request.pk}/')


class RefundWorkflowTests(TestCase):
    def setUp(self):
        variant = _stocked_variant(quantity=0)
        order = _delivered_online_order(2, variant)
        staff = _user("refundsetup", "orders.view_order")
        return_request = order_services.create_return(order, [{
            "order_item": order.items.get(), "requested_quantity": 1,
            "reason": ReturnItem.REASON_WRONG_VARIANT, "reason_notes": "",
        }], requested_by=staff)
        order_services.approve_return(return_request)
        order_services.mark_return_in_transit(return_request)
        order_services.record_return_received_quantities(
            return_request, {return_request.items.get(): 1},
        )
        order_services.mark_return_received(return_request)
        order_services.start_inspection(return_request)
        order_services.finalize_return_item(
            return_request.items.get(), received_quantity=1,
            disposition=ReturnItem.DISPOSITION_REJECTED, resolution=ReturnItem.RESOLUTION_REFUND,
            inspected_by=staff,
        )
        self.refund = order_services.create_refund_for_return(return_request, approved_by=staff)
        self.url = reverse("ops:refund_detail", args=[self.refund.public_id])

    def _post(self, name, data=None):
        return self.client.post(reverse(f"ops:{name}", args=[self.refund.public_id]), data or {})

    def test_full_refund_flow(self):
        finance = _user(
            "finance", "orders.view_refund", "orders.process_refund", "orders.complete_refund",
            "orders.add_refundadjustment",
        )
        self.client.force_login(finance)
        self.assertEqual(self.client.get(REFUNDS_URL).status_code, 200)
        page = self.client.get(self.url)
        self.assertIsNotNone(page.context["method_form"])
        self.assertIsNone(page.context["complete_form"])

        self._post("refund_processing")  # refused by the service: no method yet
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.refund_status, Refund.STATUS_PENDING)

        self._post("refund_method", {"refund_method": Refund.METHOD_UPI})
        self._post("refund_processing")
        self.refund.refresh_from_db()
        self.assertEqual((self.refund.refund_method, self.refund.refund_status),
                         (Refund.METHOD_UPI, Refund.STATUS_PROCESSING))

        self._post("refund_complete", {"refund_reference": "UTR123", "notes": "Paid"})
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.refund_status, Refund.STATUS_COMPLETED)
        self.assertEqual(self.refund.processed_by, finance)

        self._post("refund_adjustment", {"adjustment_amount": "-50.00", "reason": "Courier fee"})
        adjustment = RefundAdjustment.objects.get(refund=self.refund)
        self.assertEqual((adjustment.adjustment_amount, adjustment.approved_by), (Decimal("-50.00"), finance))

    def test_fail_needs_complete_permission(self):
        processor = _user("processor", "orders.view_refund", "orders.process_refund")
        self.client.force_login(processor)
        self._post("refund_method", {"refund_method": Refund.METHOD_UPI})
        self._post("refund_processing")
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.refund_status, Refund.STATUS_PROCESSING)
        self.assertIsNone(self.client.get(self.url).context["fail_form"])
        self.assertEqual(self._post("refund_fail", {"failure_reason": "x"}).status_code, 403)
        self.assertEqual(self._post("refund_complete", {"refund_reference": "x"}).status_code, 403)

        finance = _user("finance2", "orders.view_refund", "orders.complete_refund")
        self.client.force_login(finance)
        self._post("refund_fail", {"failure_reason": "Bank rejected"})
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.refund_status, Refund.STATUS_FAILED)

    def test_view_only_user_sees_no_forms(self):
        self.client.force_login(_user("refundview", "orders.view_refund"))
        page = self.client.get(self.url)
        for key in ("method_form", "complete_form", "fail_form", "adjustment_form"):
            self.assertIsNone(page.context[key], key)
        self.assertFalse(page.context["can_mark_processing"])
        self.assertEqual(self._post("refund_method", {"refund_method": Refund.METHOD_UPI}).status_code, 403)
        self.client.force_login(_user("norefund", "orders.view_order"))
        self.assertEqual(self.client.get(REFUNDS_URL).status_code, 403)

    def test_urls_use_public_ids(self):
        self.client.force_login(_user("refundview2", "orders.view_refund"))
        html = self.client.get(REFUNDS_URL).content.decode()
        self.assertIn(f"/ops/refunds/{self.refund.public_id}/", html)
        self.assertIsNone(re.search(rf'href="/ops/refunds/{self.refund.pk}/', html))
        self.assertEqual(self.client.get(f"/ops/refunds/{self.refund.pk}/").status_code, 404)


class RecordReceivedQuantitiesServiceTests(TestCase):
    def test_only_while_approved_or_in_transit(self):
        variant = _stocked_variant(quantity=0)
        order = _delivered_online_order(3, variant)
        return_request = order_services.create_return(order, [{
            "order_item": order.items.get(), "requested_quantity": 1,
            "reason": ReturnItem.REASON_WRONG_VARIANT, "reason_notes": "",
        }])
        line = return_request.items.get()
        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.record_return_received_quantities(return_request, {line: 1})
        order_services.approve_return(return_request)
        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.record_return_received_quantities(return_request, {line: -1})
        order_services.record_return_received_quantities(return_request, {line: 1})
        line.refresh_from_db()
        self.assertEqual(line.received_quantity, 1)


class SalesNavigationTests(TestCase):
    def test_sales_pages_are_live(self):
        superuser = User.objects.create_superuser(
            username="salesroot", email="salesroot@example.com", password=PASSWORD,
        )
        self.client.force_login(superuser)
        response = self.client.get(RETURNS_URL)
        sales = response.context["ops_nav"]["groups"][0]
        self.assertEqual(
            [(i["key"], i["implemented"], i["url"]) for i in sales["items"]],
            [("orders", True, reverse("ops:order_list")), ("in_store_sale", True, SALE_URL),
             ("returns", True, RETURNS_URL), ("refunds", True, REFUNDS_URL)],
        )
        self.assertTrue(sales["open"])
        self.assertNotIn('data-coming-soon="returns"', response.content.decode())
        home = self.client.get(reverse("ops:home"))
        self.assertEqual(
            [link["key"] for link in home.context["quick_links"]][:4],
            ["orders", "in_store_sale", "returns", "refunds"],
        )


class RecordPaymentTests(TestCase):
    """Smerfume Panel Record Payment: the admin rules, through
    record_order_payment()."""

    def setUp(self):
        from apps.ops.tests.test_ops import _order

        self.order = _order(500, total="3000.00")  # in-store, delivered, unpaid
        self.cashier = _user("cashier", "orders.view_order", "orders.add_orderpayment")
        self.client.force_login(self.cashier)
        self.url = reverse("ops:order_record_payment", args=[self.order.order_number])
        self.detail = reverse("ops:order_detail", args=[self.order.order_number])

    def _pay(self, amount, method="cash", reference="", expected=None, order=None, **extra):
        order = order or self.order
        if expected is None:
            expected = self.client.get(
                reverse("ops:order_record_payment", args=[order.order_number])
            ).context["form"].initial["expected_balance"]
        data = {
            "amount": amount, "method": method, "reference": reference,
            "payment_date": timezone.localdate().isoformat(), "notes": "",
            "expected_balance": str(expected), **extra,
        }
        return self.client.post(reverse("ops:order_record_payment", args=[order.order_number]), data)

    def _summary(self):
        return order_payments.payment_summary(self.order)

    def test_detail_shows_summary_and_button(self):
        response = self.client.get(self.detail)
        self.assertTrue(response.context["can_record_payment"])
        self.assertContains(response, f'href="{self.url}"')
        for label in ("Order total", "Paid", "Balance due", "Payment status", "Payments received"):
            self.assertContains(response, label)
        self.assertContains(response, "Unpaid")

    def test_partial_then_balance_through_service(self):
        with patch.object(
            order_payments, "record_order_payment", wraps=order_payments.record_order_payment,
        ) as service:
            response = self._pay("1000.00")
        service.assert_called_once()
        self.assertRedirects(response, self.detail)
        summary = self._summary()
        self.assertEqual((summary["total_paid"], summary["balance_due"], summary["payment_status"]),
                         (Decimal("1000.00"), Decimal("2000.00"), "partially_paid"))

        self._pay("2000.00", method="upi", reference="UTR555")
        summary = self._summary()
        self.assertEqual((summary["balance_due"], summary["payment_status"]), (Decimal("0.00"), "paid"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, Order.PAYMENT_METHOD_SPLIT)  # legacy summary refreshed

        detail = self.client.get(self.detail)
        self.assertFalse(detail.context["can_record_payment"])
        self.assertNotContains(detail, f'href="{self.url}"')
        payments = list(detail.context["payments"])
        self.assertEqual([(p.method, p.amount) for p in payments],
                         [("cash", Decimal("1000.00")), ("upi", Decimal("2000.00"))])
        self.assertContains(detail, payments[1].payment_number)
        # Fully paid: the page itself is refused.
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_single_payment_sets_its_method(self):
        self._pay("3000.00", method="card", reference="SLIP1")
        self.order.refresh_from_db()
        self.assertEqual((self.order.payment_method, self.order.payment_reference), ("card", "SLIP1"))

    def test_overpayment_and_bad_input_refused(self):
        tomorrow = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        cases = (
            {"amount": "3000.01"},
            {"amount": "0"},
            {"amount": "100", "method": ""},
            {"amount": "100", "method": "upi", "reference": ""},
            {"amount": "100", "payment_date": tomorrow},
        )
        for case in cases:
            response = self._pay(**case)
            self.assertEqual(response.status_code, 200, case)
            self.assertTrue(response.context["form"].errors, case)
        self.assertFalse(OrderPayment.objects.exists())

    def test_stale_page_or_resubmit_records_once(self):
        first = self._pay("500.00", expected="3000.00")
        self.assertRedirects(first, self.detail)
        again = self._pay("500.00", expected="3000.00")  # same page submitted twice
        self.assertEqual(again.status_code, 200)
        self.assertIn("Payments changed", " ".join(again.context["form"].non_field_errors()))
        self.assertEqual(OrderPayment.objects.count(), 1)
        self.assertEqual(again.context["form"]["expected_balance"].value(), "2500.00")

    def test_cancelled_and_online_orders_refused(self):
        from apps.ops.tests.test_ops import _order

        cancelled = _order(501, status=Order.STATUS_CANCELLED)
        online = _order(502, channel=Order.CHANNEL_ONLINE)
        for order in (cancelled, online):
            url = reverse("ops:order_record_payment", args=[order.order_number])
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self._pay("100", order=order, expected="1000.00").status_code, 403)
            detail = self.client.get(reverse("ops:order_detail", args=[order.order_number]))
            self.assertFalse(detail.context["can_record_payment"])
        self.assertFalse(OrderPayment.objects.exists())

    def test_permission_and_direct_post_refused(self):
        self.client.force_login(_user("viewer", "orders.view_order"))
        self.assertFalse(self.client.get(self.detail).context["can_record_payment"])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.client.post(self.url, {
            "amount": "100", "method": "cash", "payment_date": timezone.localdate().isoformat(),
            "expected_balance": "3000.00",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(OrderPayment.objects.exists())
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"amount": "1"}).status_code, 302)
        self.assertFalse(OrderPayment.objects.exists())

    def test_unknown_order_is_404(self):
        response = self.client.get(reverse("ops:order_record_payment", args=["SMR-NOPE-1"]))
        self.assertEqual(response.status_code, 404)

    def test_sale_then_payment_without_admin(self):
        variant = _stocked_variant(quantity=3)
        self.cashier.user_permissions.add(_perm("orders.add_order"))
        sale = self.client.post(SALE_URL, _sale_post(variant.sku))
        order = Order.objects.get(channel=Order.CHANNEL_IN_STORE, items__variant=variant)
        detail = self.client.get(sale["Location"])
        self.assertTrue(detail.context["can_record_payment"])
        response = self._pay("1000.00", order=order)
        self.assertRedirects(response, reverse("ops:order_detail", args=[order.order_number]))
        self.assertEqual(order.payments.get().recorded_by, self.cashier)


# ── Customer lookup on the New In-store Sale page ────────────────────────────

from django.core.cache import cache  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from apps.accounts.models import CustomerProfile  # noqa: E402
from apps.orders.models import ShippingAddress  # noqa: E402

LOOKUP_URL = reverse("ops:customer_lookup")


def _lookup_customer(username, mobile, first_name="Asha", **extra):
    user = User(username=username, mobile_number=mobile, role="customer", first_name=first_name,
                email=f"{username}@example.com", **extra)
    user.set_unusable_password()
    user.save()
    return user


class CustomerLookupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = _user("lookupstaff", "orders.add_order")
        self.client.force_login(self.staff)
        self.asha = _lookup_customer("asha", "+919876543210", last_name="Rao")
        self.other = _lookup_customer("other", "+919876543299", first_name="Ravi", last_name="Kumar")
        self.london = _lookup_customer("london", "+447911123456", first_name="Asha", last_name="Shah")

    def _get(self, **params):
        response = self.client.get(LOOKUP_URL, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _mobile(self, national, code="+91"):
        return self._get(country_code=code, mobile=national)

    def test_needs_permission_and_runs_no_customer_query(self):
        self.client.force_login(_user("nolookup"))
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(LOOKUP_URL, {"name": "asha"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse([q for q in ctx.captured_queries if "LIKE" in q["sql"]])

    def test_exactly_one_mode(self):
        for params in ({}, {"name": "asha", "recent": "1"}, {"mobile": "987654"},
                       {"country_code": "+91", "name": "asha"},
                       {"name": "asha", "country_code": "+91", "mobile": "9"}):
            self.assertEqual(self.client.get(LOOKUP_URL, params).status_code, 400, params)

    def test_country_code_encoding_and_validation(self):
        self.assertEqual(self.client.get(f"{LOOKUP_URL}?country_code=%2B91&mobile=987654").json()["status"], "ok")
        # An unencoded "+" arrives as a space; it is still read as +91.
        self.assertEqual(self.client.get(f"{LOOKUP_URL}?country_code=+91&mobile=987654").json()["status"], "ok")
        self.assertEqual(self.client.get(LOOKUP_URL, {"country_code": "+999", "mobile": "987654"}).status_code, 400)

    def test_mobile_prefix_scoped_to_country_with_safe_fields(self):
        data = self._mobile("987654")
        self.assertEqual((data["mode"], data["status"], data["exact"]), ("mobile", "ok", False))
        self.assertEqual([r["mobile"] for r in data["results"]], ["+919876543210", "+919876543299"])
        row = data["results"][0]
        self.assertEqual(set(row), {"id", "name", "country", "country_code", "national_number", "mobile", "email",
                                    "has_last_address"})
        self.assertEqual((row["id"], row["name"], row["email"]),
                         (str(self.asha.public_id), "Asha Rao", "a***@e***.com"))
        self.assertEqual((row["country"], row["country_code"], row["national_number"]), ("IN", "+91", "9876543210"))
        # +44 numbers are found only with the +44 code.
        self.assertEqual(self._mobile("791112")["results"], [])
        uk = self._mobile("791112", code="+44")["results"]
        self.assertEqual([(r["country"], r["national_number"]) for r in uk], [("GB", "7911123456")])

    def test_minimum_six_digits(self):
        self.assertEqual(self._mobile("98765")["status"], "too_short")
        self.assertEqual(self._mobile("79111", code="+44")["status"], "too_short")

    def test_exact_full_number_selects_one_customer(self):
        for national in ("9876543210", "98765 43210", "09876543210"):
            data = self._mobile(national)
            self.assertTrue(data["exact"], national)
            self.assertEqual([r["mobile"] for r in data["results"]], ["+919876543210"])
        self.assertTrue(self._mobile("7911123456", code="+44")["exact"])
        self.assertEqual(self._mobile("9123456789")["results"], [])

    def test_name_search(self):
        self.assertEqual(self._get(name="a")["status"], "too_short")
        self.assertEqual([r["name"] for r in self._get(name="asha")["results"]], ["Asha Rao", "Asha Shah"])
        self.assertEqual([r["name"] for r in self._get(name="asha  rao")["results"]], ["Asha Rao"])
        self.assertEqual([r["name"] for r in self._get(name="KUM")["results"]], ["Ravi Kumar"])

    def test_name_search_at_most_fifteen(self):
        for n in range(16):
            _lookup_customer(f"many{n}", f"+9170000000{n:02d}", first_name="Meera")
        data = self._get(name="meera")
        self.assertEqual((len(data["results"]), data["truncated"]), (15, True))

    def test_recent_customers_from_latest_orders_only(self):
        self.assertEqual(self._get(recent="1")["results"], [])  # no orders: never the customer table
        first = Order.objects.create(user=self.other, order_number="SMR-REC-000001", subtotal=1, total=1)
        Order.objects.create(user=self.asha, order_number="SMR-REC-000002", subtotal=1, total=1,
                             status=Order.STATUS_CANCELLED)
        Order.objects.create(user=self.other, order_number="SMR-REC-000003", subtotal=1, total=1)
        Order.objects.filter(pk=first.pk).update(created_at=timezone.now() - timezone.timedelta(days=1))
        data = self._get(recent="1")
        self.assertEqual(data["mode"], "recent")
        self.assertEqual([r["name"] for r in data["results"]], ["Ravi Kumar", "Asha Rao"])

    def test_recent_limit_fifteen(self):
        for n in range(17):
            customer = _lookup_customer(f"recent{n}", f"+9171000000{n:02d}")
            Order.objects.create(user=customer, order_number=f"SMR-RCL-{n:06d}", subtotal=1, total=1)
        self.assertEqual(len(self._get(recent="1")["results"]), 15)

    def test_excludes_staff_inactive_no_mobile_and_profile_mobile(self):
        _lookup_customer("staffer", "+919876543211", is_staff=True)
        _lookup_customer("gone", "+919876543212", is_active=False)
        _lookup_customer("nomobile", None, first_name="Asha", last_name="NoPhone")
        profile_only = _lookup_customer("profile", "+918000000000")
        CustomerProfile.objects.update_or_create(user=profile_only, defaults={"mobile": "9876543213"})
        self.assertEqual([r["mobile"] for r in self._mobile("98765432")["results"]],
                         ["+919876543210", "+919876543299"])
        self.assertNotIn("Asha NoPhone", [r["name"] for r in self._get(name="asha")["results"]])

    def test_has_last_address_and_read_only_address_endpoint(self):
        order = Order.objects.create(user=self.asha, order_number="SMR-LOOK-000001", subtotal=1, total=1)
        ShippingAddress.objects.create(
            order=order, full_name="Asha Rao", mobile="+919876543210", address_line1="1 MG Road",
            city="Pune", state="MH", pincode="411001",
        )
        rows = {r["mobile"]: r for r in self._mobile("987654")["results"]}
        self.assertTrue(rows["+919876543210"]["has_last_address"])
        self.assertFalse(rows["+919876543299"]["has_last_address"])

        url = reverse("ops:customer_last_address", args=[self.asha.public_id])
        before = ShippingAddress.objects.count()
        address = self.client.get(url).json()["address"]
        self.assertEqual((address["city"], address["pincode"]), ("Pune", "411001"))
        self.assertEqual(ShippingAddress.objects.count(), before)
        self.assertEqual(self.client.get(reverse("ops:customer_last_address", args=[self.other.public_id])).json(),
                         {"address": None})
        staff_account = _lookup_customer("staffaddr", "+919876543298", is_staff=True)
        self.assertEqual(
            self.client.get(reverse("ops:customer_last_address", args=[staff_account.public_id])).status_code, 404,
        )

    @override_settings(OPS_LOOKUP_RATE_PER_MINUTE=2)
    def test_rate_limited_per_staff_user(self):
        self._get(name="asha")
        self._get(name="asha")
        self.assertEqual(self.client.get(LOOKUP_URL, {"name": "asha"}).status_code, 429)

    def test_sale_page_has_both_controls_with_india_default(self):
        self.client.force_login(_user("salepage", "orders.add_order", "orders.view_order"))
        response = self.client.get(SALE_URL)
        self.assertContains(response, "data-customer-name")
        self.assertContains(response, "data-customer-country")
        self.assertContains(response, "data-customer-mobile")
        self.assertContains(response, '<option value="IN" selected>India (+91)</option>', html=True)

    def test_sale_with_bare_mobile_reuses_canonical_customer_and_adds_no_address(self):
        self.client.force_login(_user("seller", "orders.add_order", "orders.view_order"))
        variant = _stocked_variant()
        response = self.client.post(SALE_URL, _sale_post(variant.sku, mobile="9876543210"))
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(channel=Order.CHANNEL_IN_STORE)
        self.assertEqual(order.user, self.asha)
        self.assertFalse(ShippingAddress.objects.filter(order=order).exists())
        self.assertEqual(User.objects.filter(mobile_number__endswith="9876543210").count(), 1)

    def test_sale_combines_country_code_and_national_number(self):
        self.client.force_login(_user("seller2", "orders.add_order", "orders.view_order"))
        data = _sale_post(_stocked_variant().sku, mobile="7911123456")
        data["customer_country"] = "GB"
        self.assertEqual(self.client.post(SALE_URL, data).status_code, 302)
        self.assertEqual(Order.objects.get(channel=Order.CHANNEL_IN_STORE).user, self.london)

        data = _sale_post(_stocked_variant().sku, mobile="+447911123456")
        data["customer_country"] = "GB"
        response = self.client.post(SALE_URL, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "without the country code")
