"""Common list-page actions on the Smerfume Panel: quick view, row actions
by permission and lifecycle, current-page selection, registered bulk
actions through services, and the mobile row menu."""

import re
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.inventory.models import StockMovement
from apps.inventory.services import adjustment as adjustment_services
from apps.ops.tests.test_inventory import _stock
from apps.ops.tests.test_ops import _order
from apps.ops.tests.test_purchases import _Base, _user
from apps.orders import services as order_services
from apps.orders.models import Order, OrderItem, Return, ReturnItem
from apps.purchases import services
from apps.purchases.models import BookedQuantityChange, PurchaseOrderPayment

LISTS = (
    "ops:order_list", "ops:return_list", "ops:refund_list", "ops:vendor_list", "ops:po_list",
    "ops:payment_list", "ops:account_list", "ops:grn_list", "ops:discrepancy_list", "ops:booked_list",
    "ops:stock_list", "ops:partial_lot_list", "ops:movement_list", "ops:reservation_list", "ops:warehouse_list",
)


def _kinds(response, key):
    for obj in response.context["object_list"]:
        if obj.ops_key == str(key):
            return {(a.kind, a.label) for a in obj.ops_actions}
    raise AssertionError(f"row {key} not on page")


def _delivered_online_order(n, variant):
    order = Order.objects.create(
        user=_order(900 + n).user, order_number=f"SMR-LA-{n:06d}", channel=Order.CHANNEL_ONLINE,
        status=Order.STATUS_DELIVERED, subtotal=Decimal("100"), total=Decimal("100"), delivered_at=timezone.now(),
    )
    OrderItem.objects.create(order=order, variant=variant, quantity=1, unit_price=Decimal("100"),
                             line_total=Decimal("100"), final_paid_line_amount=Decimal("100"))
    return order


class ListShellTests(_Base):
    def test_every_live_list_has_selection_bulk_bar_drawer_and_quick_view(self):
        self.client.force_login(self.owner)
        po = self._po()
        services.post_goods_receipt(self._receipt(po), self.owner)
        _order(1)
        _stock("3")
        for name in LISTS:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            html = response.content.decode()
            self.assertIn("data-quick-drawer", html, name)
            if response.context["object_list"]:
                self.assertIn("data-select-all", html, name)
                self.assertIn('name="keys"', html, name)
                self.assertIn('form="bulk-form"', html, name)
                self.assertIn("data-bulk-bar", html, name)
                self.assertIn("data-quick-view", html, name)
                self.assertIn('class="row-menu"', html, name)  # mobile three-dot menu
                for obj in response.context["object_list"]:
                    self.assertEqual(obj.ops_actions[0].kind, "quick", name)

    def test_quick_view_renders_detail_without_shell(self):
        self.client.force_login(self.owner)
        po = self._po()
        detail = reverse("ops:po_detail", args=[po.po_number])
        quick = self.client.get(detail, {"quick": "1"})
        self.assertTemplateUsed(quick, "ops/quick.html")
        html = quick.content.decode()
        self.assertNotIn("<html", html)
        self.assertNotIn('id="ops-sidebar"', html)
        self.assertIn(po.po_number, html)
        self.assertIn(f'href="{detail}"', html)  # open full page
        self.assertTemplateNotUsed(self.client.get(detail), "ops/quick.html")

    def test_quick_view_keeps_permissions(self):
        po = self._po()
        self.client.force_login(_user("noquick", "purchases.view_goodsreceipt"))
        self.assertEqual(self.client.get(reverse("ops:po_detail", args=[po.po_number]), {"quick": "1"}).status_code, 403)


class RowActionPermissionTests(_Base):
    def test_po_row_actions_follow_permission_and_status(self):
        draft = self._po(issue=False)
        issued = self._po()
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ops:po_list"))
        self.assertIn(("edit", "Edit"), _kinds(page, draft.po_number))
        self.assertNotIn(("edit", "Edit"), _kinds(page, issued.po_number))
        self.assertIn(("domain", "Receive goods"), _kinds(page, issued.po_number))
        self.client.force_login(_user("poview", "purchases.view_purchaseorder"))
        page = self.client.get(reverse("ops:po_list"))
        self.assertEqual(_kinds(page, draft.po_number), {("quick", "Quick view")})

    def test_posted_payment_has_no_edit_or_delete(self):
        po = self._po(quantity=1, price="1000")
        posted = services.post_payment(
            po, user=self.owner, amount=Decimal("100"), entry_date=timezone.localdate(), method="upi",
            reference="U1", notes="", paid_through=self.account,
        )
        draft = services.create_payment_draft(
            po, user=self.owner, amount=Decimal("50"), entry_date=timezone.localdate(), method="cash",
            reference="", notes="", paid_through=None,
        )
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ops:payment_list"))
        posted_kinds = {kind for kind, _ in _kinds(page, posted.public_id)}
        self.assertFalse(posted_kinds & {"edit", "delete"})
        self.assertIn(("domain", "Reverse"), _kinds(page, posted.public_id))
        self.assertTrue({"edit", "delete"} <= {kind for kind, _ in _kinds(page, draft.public_id)})
        self.assertIn('data-confirm="Delete this draft payment? This cannot be undone."', page.content.decode())

    def test_immutable_ledgers_are_quick_view_only(self):
        stock = _stock("5")
        adjustment_services.adjust_inventory_stock(stock, quantity_delta=Decimal("1"),
                                                   reason=StockMovement.REASON_STOCKTAKING_RESULTS)
        po = self._po()
        services.set_confirmed_booked_quantities(po, {po.lines.get().pk: 3}, self.owner, "Confirmed")
        self.client.force_login(self.owner)
        movement = StockMovement.objects.get()
        self.assertEqual(_kinds(self.client.get(reverse("ops:movement_list")), movement.public_id),
                         {("quick", "Quick view")})
        change = BookedQuantityChange.objects.get()
        self.assertEqual(_kinds(self.client.get(reverse("ops:booked_list")), change.public_id),
                         {("quick", "Quick view")})
        for name in ("ops:vendor_list", "ops:account_list", "ops:stock_list", "ops:grn_list"):
            for obj in self.client.get(reverse(name)).context["object_list"]:
                self.assertNotIn("delete", {a.kind for a in obj.ops_actions}, name)


class BulkActionTests(_Base):
    def _bulk(self, name, action, keys, next_url=None):
        return self.client.post(reverse(name), {"action": action, "keys": [str(k) for k in keys],
                                                "next": next_url or ""})

    def test_bulk_bar_lists_only_allowed_actions(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ops:payment_list"))
        self.assertEqual([a.key for a in page.context["ops_bulk_actions"]], ["delete_drafts"])
        self.client.force_login(_user("payview", "purchases.view_purchaseorderpayment"))
        page = self.client.get(reverse("ops:payment_list"))
        self.assertEqual(page.context["ops_bulk_actions"], [])

    def test_bulk_delete_drafts_needs_permission_and_spares_posted(self):
        po = self._po(quantity=1, price="1000")
        draft = services.create_payment_draft(
            po, user=self.owner, amount=Decimal("50"), entry_date=timezone.localdate(), method="cash",
            reference="", notes="", paid_through=None,
        )
        posted = services.post_payment(
            po, user=self.owner, amount=Decimal("100"), entry_date=timezone.localdate(), method="upi",
            reference="U1", notes="", paid_through=self.account,
        )
        self.client.force_login(_user("payview2", "purchases.view_purchaseorderpayment"))
        self.assertEqual(self._bulk("ops:payment_bulk", "delete_drafts", [draft.public_id]).status_code, 403)
        self.assertTrue(PurchaseOrderPayment.objects.filter(pk=draft.pk).exists())

        self.client.force_login(self.owner)
        list_url = reverse("ops:payment_list") + "?status=draft"
        response = self._bulk("ops:payment_bulk", "delete_drafts", [draft.public_id, posted.public_id], list_url)
        self.assertRedirects(response, list_url)
        self.assertFalse(PurchaseOrderPayment.objects.filter(pk=draft.pk).exists())
        self.assertTrue(PurchaseOrderPayment.objects.filter(pk=posted.pk).exists())  # posted never deleted
        warnings = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("not available" in m for m in warnings))

    def test_unknown_action_and_unsafe_next(self):
        self.client.force_login(self.owner)
        self.assertEqual(self._bulk("ops:payment_bulk", "delete_everything", []).status_code, 403)
        response = self._bulk("ops:grn_bulk", "post", [], "https://evil.example/")
        self.assertRedirects(response, reverse("ops:grn_list"))

    def test_bulk_post_receipts_through_service(self):
        po = self._po(quantity=10)
        first = self._receipt(po, quantity=2)
        second = self._receipt(po, quantity=3)
        self.client.force_login(self.owner)
        self._bulk("ops:grn_bulk", "post", [first.grn_number, second.grn_number])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.status, second.status), ("posted", "posted"))
        self.client.force_login(_user("grnview", "purchases.view_goodsreceipt"))
        self.assertEqual(self._bulk("ops:grn_bulk", "post", [first.grn_number]).status_code, 403)

    def test_bulk_approve_returns_only_eligible(self):
        order = _delivered_online_order(1, self.variant)
        requested = order_services.create_return(order, [{
            "order_item": order.items.get(), "requested_quantity": 1,
            "reason": ReturnItem.REASON_WRONG_VARIANT, "reason_notes": "",
        }])
        other = _delivered_online_order(2, self.variant)
        rejected = order_services.create_return(other, [{
            "order_item": other.items.get(), "requested_quantity": 1,
            "reason": ReturnItem.REASON_WRONG_VARIANT, "reason_notes": "",
        }])
        order_services.reject_return(rejected)
        self.client.force_login(_user("returnview", "orders.view_return"))
        self.assertEqual(self._bulk("ops:return_bulk", "approve", [requested.public_id]).status_code, 403)
        self.client.force_login(self.owner)
        self._bulk("ops:return_bulk", "approve", [requested.public_id, rejected.public_id])
        requested.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual((requested.status, rejected.status), (Return.STATUS_APPROVED, Return.STATUS_REJECTED))


class MobileMenuTests(_Base):
    def test_row_menu_holds_the_same_actions(self):
        self._po(issue=False)
        self.client.force_login(self.owner)
        html = self.client.get(reverse("ops:po_list")).content.decode()
        menu = re.search(r'<details class="row-menu">(.*?)</details>', html, re.S).group(1)
        self.assertIn("Quick view", menu)
        self.assertIn("Edit", menu)
        self.assertIn('aria-label="Actions for', menu)
