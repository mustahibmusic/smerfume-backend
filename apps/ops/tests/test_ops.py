"""Tests for the Smerfume Operations backend shell (/ops/), Phase 1."""

import re
import uuid
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.orders import payments as order_payments
from apps.orders.models import Order, OrderItem, ShippingAddress

User = get_user_model()
PASSWORD = "ops-test-pass-123"

LIST_URL = reverse("ops:order_list")
LOGIN_URL = reverse("ops:login")
LOGOUT_URL = reverse("ops:logout")
HOME_URL = reverse("ops:home")
ADMIN_ORDER_CHANGELIST = reverse("admin:orders_order_changelist")


def _perm(code):
    app_label, codename = code.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


def _staff(username, *codes, is_staff=False, is_active=True):
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        is_staff=is_staff, is_active=is_active,
    )
    user.user_permissions.add(*(_perm(code) for code in codes))
    return user


def _ops_user(username="opsuser", **kwargs):
    return _staff(username, OPS_ACCESS_PERMISSION, "orders.view_order", **kwargs)


def _customer(n):
    user = User(
        username=f"ops_customer_{n}", mobile_number=f"+9196{n:08d}", email=None,
        role="customer", first_name="Asha", last_name=f"Customer{n}",
    )
    user.set_unusable_password()
    user.save()
    return user


def _variant():
    brand = Brand.objects.get_or_create(name="OpsBrand", slug="opsbrand")[0]
    category = Category.objects.get_or_create(name="OpsCat", slug="opscat")[0]
    product = Product.objects.get_or_create(
        name="OpsProduct", slug="opsproduct", defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="opsproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=50, selling_price="1000.00", mrp="1200.00",
        sku=f"OPS-{uuid.uuid4().hex[:10]}",
    )


def _order(n, total="1000.00", channel=Order.CHANNEL_IN_STORE, status=Order.STATUS_DELIVERED,
           variant=None, items=1, **extra):
    order = Order.objects.create(
        user=_customer(n), order_number=f"SMR-OPS-{n:06d}", channel=channel, status=status,
        subtotal=Decimal(total), total=Decimal(total), **extra,
    )
    variant = variant or _variant()
    for _ in range(items):
        OrderItem.objects.create(
            order=order, variant=variant, quantity=1,
            unit_price=Decimal(total) / items, line_total=Decimal(total) / items,
        )
    return order


def _pay(order, amount, recorded_by, method="cash", reference=""):
    order_payments.record_order_payment(
        order, method=method, amount=Decimal(amount), reference=reference, recorded_by=recorded_by,
    )


class AccessTests(TestCase):
    def setUp(self):
        self.order = _order(1)
        self.detail_url = reverse("ops:order_detail", args=[self.order.order_number])

    def test_anonymous_is_redirected_to_ops_login(self):
        for url in (HOME_URL, LIST_URL, self.detail_url, "/ops/unknown/path/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith(f"{LOGIN_URL}?next="), url)

    def test_staff_without_access_ops_gets_branded_403(self):
        user = _staff("staffonly", "orders.view_order", is_staff=True)
        self.client.force_login(user)
        for url in (HOME_URL, LIST_URL, self.detail_url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertTemplateUsed(response, "ops/403.html")
            self.assertNotContains(response, self.order.order_number, status_code=403)

    def test_access_ops_without_view_order_gets_403_and_no_orders_nav(self):
        user = _staff("noview", OPS_ACCESS_PERMISSION)
        self.client.force_login(user)
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url).status_code, 403)
        response = self.client.get(HOME_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "ops/home.html")
        self.assertEqual(response.context["ops_nav"], [])

    def test_ops_user_sees_orders_and_nav(self):
        self.client.force_login(_ops_user())
        response = self.client.get(HOME_URL)
        self.assertRedirects(response, LIST_URL)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["key"] for item in response.context["ops_nav"]], ["orders"])
        self.assertTrue(response.context["ops_nav"][0]["active"])
        self.assertContains(response, self.order.order_number)

    def test_inactive_user_is_refused(self):
        # The auth backend drops an inactive user from the session, so the
        # request is anonymous and goes to the ops login.
        user = _ops_user("inactive", is_active=False)
        self.client.force_login(user)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))

    def test_superuser_passes(self):
        superuser = User.objects.create_superuser(
            username="opsroot", email="opsroot@example.com", password=PASSWORD,
        )
        self.client.force_login(superuser)
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)

    def test_access_ops_does_not_grant_django_admin(self):
        self.client.force_login(_ops_user())
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_admin_link_only_for_admin_capable_users(self):
        self.client.force_login(_ops_user())
        response = self.client.get(self.detail_url)
        self.assertIsNone(response.context["admin_url"])
        self.assertNotContains(response, "Open in Django Admin")

        self.client.force_login(_ops_user("opsstaff", is_staff=True))
        response = self.client.get(self.detail_url)
        admin_url = reverse("admin:orders_order_change", args=[self.order.pk])
        self.assertEqual(response.context["admin_url"], admin_url)
        self.assertContains(response, admin_url)


class LoginLogoutTests(TestCase):
    def test_login_with_access_redirects_to_ops(self):
        _ops_user()
        response = self.client.post(LOGIN_URL, {"username": "opsuser@example.com", "password": PASSWORD})
        self.assertRedirects(response, HOME_URL, target_status_code=302)
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)

    def test_login_follows_safe_next_only(self):
        _ops_user()
        response = self.client.post(
            LOGIN_URL, {"username": "opsuser@example.com", "password": PASSWORD, "next": LIST_URL},
        )
        self.assertRedirects(response, LIST_URL)
        self.client.logout()
        response = self.client.post(
            LOGIN_URL,
            {"username": "opsuser@example.com", "password": PASSWORD, "next": "https://evil.example/"},
        )
        self.assertRedirects(response, HOME_URL, target_status_code=302)

    def test_login_without_access_ops_is_refused(self):
        _staff("plainstaff", "orders.view_order", is_staff=True)
        response = self.client.post(
            LOGIN_URL, {"username": "plainstaff@example.com", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have access to Smerfume Operations")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_inactive_or_wrong_password_is_generic(self):
        _ops_user("sleepy", is_active=False)
        for username, password in (("sleepy@example.com", PASSWORD), ("opsnobody@example.com", "x")):
            response = self.client.post(LOGIN_URL, {"username": username, "password": password})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("_auth_user_id", self.client.session)
            self.assertNotContains(response, "does not have access")

    def test_logout_is_post_only(self):
        self.client.force_login(_ops_user())
        self.assertEqual(self.client.get(LOGOUT_URL).status_code, 405)
        response = self.client.post(LOGOUT_URL)
        self.assertRedirects(response, LOGIN_URL)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_page_uses_ops_assets_only(self):
        response = self.client.get(LOGIN_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ops/ops.css")
        self.assertNotContains(response, "unfold")


class NotFoundTests(TestCase):
    def setUp(self):
        self.client.force_login(_ops_user())

    def test_unknown_order_number_is_branded_404(self):
        response = self.client.get(reverse("ops:order_detail", args=["SMR-NOPE-000000"]))
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "ops/404.html")

    def test_unknown_ops_path_is_branded_404(self):
        response = self.client.get("/ops/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "ops/404.html")

    def test_invalid_page_is_branded_404(self):
        _order(2)
        for page in ("999", "abc"):
            response = self.client.get(LIST_URL, {"page": page})
            self.assertEqual(response.status_code, 404, page)
            self.assertTemplateUsed(response, "ops/404.html")

    def test_order_pk_is_not_an_ops_identifier(self):
        order = _order(3)
        response = self.client.get(f"/ops/orders/{order.pk}/")
        self.assertEqual(response.status_code, 404)


class OrderListTests(TestCase):
    def setUp(self):
        self.staff = _ops_user()
        self.client.force_login(self.staff)
        self.variant = _variant()

    def test_search_and_filters(self):
        unpaid = _order(10, variant=self.variant)
        paid = _order(11, variant=self.variant, status=Order.STATUS_PROCESSING)
        _pay(paid, "1000.00", self.staff)
        online = _order(12, variant=self.variant, channel=Order.CHANNEL_ONLINE)
        ShippingAddress.objects.create(
            order=online, full_name="Ravi Shipper", mobile="9123456780",
            address_line1="1 Road", city="Pune", state="MH", pincode="411001",
        )

        def numbers(**params):
            response = self.client.get(LIST_URL, params)
            self.assertEqual(response.status_code, 200)
            return {row["order"].order_number for row in response.context["rows"]}

        self.assertEqual(numbers(q="OPS-000010"), {unpaid.order_number})
        self.assertEqual(numbers(q="9123456780"), {online.order_number})
        self.assertEqual(numbers(q="Ravi"), {online.order_number})
        self.assertEqual(numbers(q=unpaid.user.mobile_number[-8:]), {unpaid.order_number})
        self.assertEqual(numbers(status="processing"), {paid.order_number})
        self.assertEqual(numbers(channel="online"), {online.order_number})
        self.assertEqual(numbers(payment_status="paid"), {paid.order_number})

    def test_invalid_filter_is_reported_and_ignored(self):
        order = _order(13, variant=self.variant)
        response = self.client.get(LIST_URL, {"status": "bogus"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("status", response.context["filter_form"].errors)
        self.assertContains(response, "This filter was ignored")
        self.assertContains(response, order.order_number)

    def test_list_query_count_is_flat(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(LIST_URL).status_code, 200)
            return len(ctx.captured_queries)

        for n in range(2):
            _pay(_order(20 + n, variant=self.variant), "100.00", self.staff)
        with_two = count()
        for n in range(2, 10):
            _pay(_order(20 + n, variant=self.variant), "100.00", self.staff)
        self.assertEqual(with_two, count())

    def test_detail_query_count_is_bounded(self):
        def count(order):
            url = reverse("ops:order_detail", args=[order.order_number])
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(ctx.captured_queries)

        small = _order(40, variant=self.variant, items=1)
        _pay(small, "100.00", self.staff)
        large = _order(41, variant=self.variant, items=4)
        for _ in range(3):
            _pay(large, "100.00", self.staff)
        self.assertEqual(count(small), count(large))


class ReadOnlyAndIdentifierTests(TestCase):
    WRITE_SQL = re.compile(r"^\s*(insert\s+into|update|delete\s+from)\s+\"?(\w+)", re.IGNORECASE)
    # Framework bookkeeping (sessions, auth) is not a business write.
    FRAMEWORK_TABLES = ("django_", "auth_")

    def setUp(self):
        self.staff = _ops_user(is_staff=True)
        self.client.force_login(self.staff)
        self.order = _order(50)
        _pay(self.order, "400.00", self.staff)

    def _domain_writes(self, url):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        writes = []
        for query in ctx.captured_queries:
            match = self.WRITE_SQL.match(query["sql"])
            if match and not match.group(2).lower().startswith(self.FRAMEWORK_TABLES):
                writes.append(query["sql"])
        return writes

    def test_get_list_and_detail_cause_no_domain_writes(self):
        detail = reverse("ops:order_detail", args=[self.order.order_number])
        self.assertEqual(self._domain_writes(LIST_URL), [])
        self.assertEqual(self._domain_writes(detail), [])

    def test_ops_native_urls_expose_no_db_pks(self):
        detail = reverse("ops:order_detail", args=[self.order.order_number])
        admin_url = reverse("admin:orders_order_change", args=[self.order.pk])
        pks = {str(self.order.pk), str(self.order.user.pk), str(self.staff.pk)}
        for url in (LIST_URL, detail):
            html = self.client.get(url).content.decode()
            for href in re.findall(r'href="([^"]+)"', html):
                if href == admin_url or not href.startswith("/ops/"):
                    continue
                segments = set(href.split("?")[0].strip("/").split("/"))
                self.assertFalse(segments & pks, href)
            for action in re.findall(r'action="([^"]+)"', html):
                self.assertFalse(set(action.strip("/").split("/")) & pks, action)


class SourceOfTruthParityTests(TestCase):
    """Ops and Django Admin show the same totals, paid, balance due and
    statuses, because both read apps.orders (fields and payments module)."""

    def setUp(self):
        self.staff = _ops_user(is_staff=True)
        self.variant = _variant()
        self.orders = {
            "online_unpaid": _order(60, "2500.00", channel=Order.CHANNEL_ONLINE,
                                    status=Order.STATUS_PENDING, variant=self.variant),
            "in_store_unpaid": _order(61, "1000.00", variant=self.variant),
            "partial": _order(62, "3600.00", variant=self.variant),
            "split_paid": _order(63, "3000.00", variant=self.variant),
        }
        _pay(self.orders["partial"], "1999.00", self.staff, "upi", "UTR1")
        _pay(self.orders["split_paid"], "1000.00", self.staff, "cash")
        _pay(self.orders["split_paid"], "2000.00", self.staff, "card", "SLIP9")

    @staticmethod
    def _values(order):
        return (order.total, order.total_paid, order.balance_due, order.payment_status, order.status)

    def test_list_detail_and_admin_agree(self):
        self.client.force_login(self.staff)
        ops_rows = {
            row["order"].order_number: self._values(row["order"])
            for row in self.client.get(LIST_URL).context["rows"]
        }

        superuser = User.objects.create_superuser(
            username="parityroot", email="parityroot@example.com", password=PASSWORD,
        )
        self.client.force_login(superuser)
        admin_rows = {
            order.order_number: self._values(order)
            for order in self.client.get(ADMIN_ORDER_CHANGELIST).context["cl"].result_list
        }
        self.assertEqual(ops_rows, admin_rows)

        self.client.force_login(self.staff)
        expected = {
            "online_unpaid": ("0.00", "2500.00", "unpaid"),
            "in_store_unpaid": ("0.00", "1000.00", "unpaid"),
            "partial": ("1999.00", "1601.00", "partially_paid"),
            "split_paid": ("3000.00", "0.00", "paid"),
        }
        for key, order in self.orders.items():
            response = self.client.get(reverse("ops:order_detail", args=[order.order_number]))
            summary = response.context["summary"]
            self.assertEqual(summary, order_payments.payment_summary(order), key)
            paid, balance, status = expected[key]
            self.assertEqual(
                (summary["total_paid"], summary["balance_due"], summary["payment_status"]),
                (Decimal(paid), Decimal(balance), status), key,
            )
            self.assertEqual(ops_rows[order.order_number][1:4], (Decimal(paid), Decimal(balance), status))
            self.assertEqual(response.context["order"].get_status_display(), order.get_status_display())


class StaffGroupsTests(TestCase):
    def test_every_staff_group_gets_access_ops(self):
        call_command("setup_staff_groups", stdout=StringIO())
        for name in ("Ops", "Warehouse", "Finance", "Manager"):
            group = Group.objects.get(name=name)
            self.assertTrue(
                group.permissions.filter(
                    content_type__app_label="core", codename="access_ops",
                ).exists(),
                name,
            )
