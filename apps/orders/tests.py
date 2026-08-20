"""
Tests for apps.orders.

Test groups:
    AuthenticatedCheckoutTests   — existing checkout flow (regression guard)
    GuestCheckoutTests           — guest checkout end-to-end
    GuestUserResolutionTests     — _resolve_guest_user helper unit tests
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.cart.models import Cart, CartItem
from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.orders.models import Order

User = get_user_model()

CHECKOUT_URL = "/api/orders/checkout/"

_SHIPPING = {
    "full_name": "Test User",
    "mobile": "9876543210",
    "address_line1": "42, MG Road",
    "city": "Bengaluru",
    "state": "KA",
    "pincode": "560001",
    "country": "India",
}


def _make_user(mobile="+919700000001", email=None, **kwargs):
    username = kwargs.pop("username", f"u_{mobile[-4:]}")
    user = User(username=username, mobile_number=mobile, email=email, role="customer")
    user.set_unusable_password()
    for attr, val in kwargs.items():
        setattr(user, attr, val)
    user.save()
    return user


def _auth_header(user):
    refresh = RefreshToken.for_user(user)
    return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}


def _make_variant(selling_price="500.00", mrp="600.00", size_ml=10):
    brand = Brand.objects.get_or_create(name="OrderBrand", slug="orderbrand")[0]
    category = Category.objects.get_or_create(name="OrderCat", slug="ordercat")[0]
    product = Product.objects.get_or_create(
        name="OrderProduct", slug="orderproduct",
        defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="orderproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition, size_ml=size_ml,
        selling_price=selling_price, mrp=mrp,
    )


# ── Authenticated checkout regression guard ──────────────────────────────────

class AuthenticatedCheckoutTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)

    def test_checkout_creates_order(self):
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data["order_number"].startswith("SMR-"))
        self.assertEqual(resp.data["is_guest_order"], False)

    def test_checkout_clears_cart(self):
        self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        cart = Cart.objects.get(user=self.user)
        self.assertEqual(cart.items.count(), 0)

    def test_checkout_empty_cart_returns_400(self):
        Cart.objects.get(user=self.user).items.all().delete()
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_checkout_snapshots_price(self):
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        item = resp.data["items"][0]
        self.assertEqual(str(item["unit_price"]), str(self.variant.selling_price))


# ── Guest checkout tests ─────────────────────────────────────────────────────

class GuestCheckoutTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.variant = _make_variant(size_ml=20)

    def _make_guest_cart(self, token="guesttoken123"):
        cart = Cart.objects.create(session_key=token)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        return token

    def test_guest_checkout_creates_order_and_user(self):
        """A new guest mobile creates a User and links the order."""
        token = self._make_guest_cart()
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "guest@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN=token,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["is_guest_order"], True)

        order = Order.objects.get(order_number=resp.data["order_number"])
        self.assertEqual(order.guest_email, "guest@example.com")
        self.assertIsNotNone(order.user)
        self.assertEqual(order.user.mobile_number, "9876543210")

    def test_guest_checkout_clears_guest_cart(self):
        token = self._make_guest_cart(token="cleartoken")
        self.client.post(
            CHECKOUT_URL,
            {"guest_email": "guest2@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="cleartoken",
        )
        cart = Cart.objects.get(session_key="cleartoken")
        self.assertEqual(cart.items.count(), 0)

    def test_guest_checkout_missing_email_returns_400(self):
        token = self._make_guest_cart(token="noemail")
        resp = self.client.post(
            CHECKOUT_URL,
            {"shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="noemail",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("guest_email", resp.data["error"])

    def test_guest_checkout_missing_cart_token_returns_400(self):
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "g@example.com", "shipping_address": _SHIPPING},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("X-Cart-Token", resp.data["error"])

    def test_guest_checkout_invalid_cart_token_returns_400(self):
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "g@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="doesnotexist",
        )
        self.assertEqual(resp.status_code, 400)

    def test_guest_checkout_empty_cart_returns_400(self):
        Cart.objects.create(session_key="emptyguest")
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "g@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="emptyguest",
        )
        self.assertEqual(resp.status_code, 400)

    def test_guest_checkout_links_to_existing_user_by_mobile(self):
        """If the mobile already has an account, the order links to it."""
        existing_user = _make_user(mobile="9876543210")
        token = self._make_guest_cart(token="existingmobile")
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "new@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="existingmobile",
        )
        self.assertEqual(resp.status_code, 201)
        order = Order.objects.get(order_number=resp.data["order_number"])
        self.assertEqual(order.user.pk, existing_user.pk)

    def test_guest_checkout_sets_email_on_new_user(self):
        token = self._make_guest_cart(token="emailtoken")
        self.client.post(
            CHECKOUT_URL,
            {"guest_email": "setme@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="emailtoken",
        )
        user = User.objects.get(mobile_number="9876543210")
        self.assertEqual(user.email, "setme@example.com")

    def test_guest_checkout_does_not_overwrite_existing_email(self):
        """If the existing user already has an email, it is not overwritten."""
        _make_user(mobile="9876543210", email="original@example.com")
        token = self._make_guest_cart(token="dontoverwrite")
        self.client.post(
            CHECKOUT_URL,
            {"guest_email": "new@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="dontoverwrite",
        )
        user = User.objects.get(mobile_number="9876543210")
        self.assertEqual(user.email, "original@example.com")

    def test_auto_created_user_can_see_order_after_login(self):
        """Guest user created at checkout appears in order list after OTP login."""
        token = self._make_guest_cart(token="logincheck")
        resp = self.client.post(
            CHECKOUT_URL,
            {"guest_email": "logincheck@example.com", "shipping_address": _SHIPPING},
            format="json",
            HTTP_X_CART_TOKEN="logincheck",
        )
        self.assertEqual(resp.status_code, 201)

        user = User.objects.get(mobile_number="9876543210")
        self.client.credentials(**_auth_header(user))
        list_resp = self.client.get("/api/orders/")
        self.assertEqual(list_resp.status_code, 200)
        results = list_resp.data["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["order_number"], resp.data["order_number"])
