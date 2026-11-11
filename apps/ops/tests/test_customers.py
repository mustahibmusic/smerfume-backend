"""Customers and Users pages on the Smerfume Panel: read-only, operational
fields only, order history with payment status, returns/refunds links."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import OTPVerification
from apps.ops.tests.test_list_actions import _delivered_online_order
from apps.ops.tests.test_ops import _order, _pay
from apps.ops.tests.test_purchases import User, _user
from apps.orders import services as order_services
from apps.orders.models import ReturnItem

PASSWORD = "Str0ng-Passw0rd!"


class _Customers(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser(username="custroot", email="custroot@example.com", password=PASSWORD)
        self.order = _order(1, total="1000.00")
        self.customer = self.order.user
        _pay(self.order, "400", self.owner)


class CustomerPageTests(_Customers):
    def test_list_uses_list_pattern_and_searches(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ops:customer_list"), {"q": self.customer.mobile_number})
        self.assertEqual(list(page.context["object_list"]), [self.customer])
        html = page.content.decode()
        for marker in ("data-select-all", "data-bulk-bar", "data-quick-drawer", 'class="row-menu"'):
            self.assertIn(marker, html)
        row = page.context["object_list"][0]
        self.assertEqual(row.ops_key, str(self.customer.public_id))
        self.assertEqual([a.kind for a in row.ops_actions], ["quick", "view"])
        # Staff accounts are not customers.
        self.assertNotIn(self.owner, self.client.get(reverse("ops:customer_list")).context["object_list"])

    def test_detail_shows_contact_orders_balance_and_returns(self):
        online = _delivered_online_order(5, self.order.items.get().variant)
        online.user = self.customer
        online.save(update_fields=["user"])
        ret = order_services.create_return(online, [{
            "order_item": online.items.get(), "requested_quantity": 1,
            "reason": ReturnItem.REASON_WRONG_VARIANT, "reason_notes": "",
        }])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("ops:customer_detail", args=[self.customer.public_id]))
        html = response.content.decode()
        self.assertIn(self.customer.mobile_number, html)
        self.assertIn("Asha", html)
        self.assertIn(reverse("ops:order_detail", args=[self.order.order_number]), html)
        row = next(r for r in response.context["orders"] if r["order"] == self.order)
        self.assertEqual(row["order"].balance_due, Decimal("600.00"))
        self.assertEqual(row["payment_status_label"], "Partially paid")
        self.assertIn(reverse("ops:return_detail", args=[ret.public_id]), html)

    def test_sections_follow_orders_permissions(self):
        self.client.force_login(_user("custonly", "accounts.view_customerprofile"))
        response = self.client.get(reverse("ops:customer_detail", args=[self.customer.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["orders"], [])
        self.assertNotIn(self.order.order_number, response.content.decode())
        self.assertEqual(self.client.get(reverse("ops:user_list")).status_code, 403)

    def test_non_customer_and_unknown_are_404(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("ops:customer_detail", args=[self.owner.public_id])).status_code, 404)

    def test_needs_permission(self):
        self.client.force_login(_user("nocust", "orders.view_order"))
        self.assertEqual(self.client.get(reverse("ops:customer_list")).status_code, 403)


class UserPageTests(_Customers):
    def test_users_page_exposes_operational_fields_only(self):
        OTPVerification.objects.create(user=self.customer, otp_hash="$2b$12$secrethashvalue")
        self.client.force_login(self.owner)
        for response in (
            self.client.get(reverse("ops:user_list")),
            self.client.get(reverse("ops:user_detail", args=[self.owner.public_id])),
            self.client.get(reverse("ops:user_detail", args=[self.customer.public_id])),
        ):
            self.assertEqual(response.status_code, 200)
            html = response.content.decode()
            self.assertNotIn(self.owner.password, html)
            self.assertNotIn("pbkdf2", html)
            self.assertNotIn("secrethashvalue", html)
            self.assertNotIn("otp_session_token", html)
        detail = self.client.get(reverse("ops:user_detail", args=[self.customer.public_id]))
        self.assertIn(reverse("ops:customer_detail", args=[self.customer.public_id]), detail.content.decode())

    def test_users_are_read_only(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("ops:user_list"), {"role": "customer", "staff": "no"})
        self.assertEqual(list(page.context["object_list"]), [self.customer])
        self.assertEqual(page.context["ops_bulk_actions"], [])
        for obj in page.context["object_list"]:
            self.assertFalse({a.kind for a in obj.ops_actions} & {"edit", "delete"})
        self.assertEqual(
            self.client.post(reverse("ops:user_detail", args=[self.customer.public_id]), {}).status_code, 405,
        )
