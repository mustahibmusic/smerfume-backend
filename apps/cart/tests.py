"""
Tests for apps.cart.

Test groups:
    GuestCartTests        — guest cart creation and X-Cart-Token resolution
    AuthenticatedCartTests — authenticated cart CRUD (regression guard)
    CartMergeServiceTests  — merge_guest_cart service unit tests
    CartOwnershipTests      — stabilization: cross-owner item access is rejected
"""

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.cart.models import Cart, CartItem
from apps.cart.services import merge_guest_cart

User = get_user_model()

CART_URL = "/api/cart/"
ADD_URL = "/api/cart/add/"
CLEAR_URL = "/api/cart/clear/"


def _make_user(mobile="+919800000001", **kwargs):
    username = kwargs.pop("username", f"u_{mobile[-4:]}")
    user = User(username=username, mobile_number=mobile, email=None, role="customer")
    user.set_unusable_password()
    for attr, val in kwargs.items():
        setattr(user, attr, val)
    user.save()
    return user


def _auth_header(user):
    refresh = RefreshToken.for_user(user)
    return {"HTTP_AUTHORIZATION": f"Bearer {refresh.access_token}"}


def _make_variant(selling_price="500.00", mrp="600.00", size_ml=10):
    """Create a minimal active ProductVariant for testing."""
    brand = Brand.objects.get_or_create(name="TestBrand", slug="testbrand")[0]
    category = Category.objects.get_or_create(name="TestCat", slug="testcat")[0]
    product = Product.objects.get_or_create(
        name="TestProduct", slug="testproduct",
        defaults={"brand": brand, "category": category},
    )[0]
    edition = ProductEdition.objects.get_or_create(
        product=product, slug="testproduct-edp",
        defaults={"name": "EDP", "concentration": "edp", "gender": "unisex"},
    )[0]
    return ProductVariant.objects.create(
        edition=edition,
        size_ml=size_ml,
        selling_price=selling_price,
        mrp=mrp,
        sku=f"TEST-{uuid.uuid4().hex[:10]}",
    )


# ── Guest cart tests ─────────────────────────────────────────────────────────

class GuestCartTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.variant = _make_variant()

    def test_get_cart_without_token_creates_new_cart_with_token(self):
        """A guest with no X-Cart-Token receives a fresh cart and a cart_token."""
        resp = self.client.get(CART_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("cart_token", resp.data)
        self.assertIsNotNone(resp.data["cart_token"])
        # A Cart record should exist
        token = resp.data["cart_token"]
        self.assertTrue(Cart.objects.filter(session_key=token, user__isnull=True).exists())

    def test_get_cart_with_existing_token_returns_same_cart(self):
        """X-Cart-Token resolves to the same cart on subsequent requests."""
        resp1 = self.client.get(CART_URL)
        token = resp1.data["cart_token"]

        resp2 = self.client.get(CART_URL, HTTP_X_CART_TOKEN=token)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["cart_token"], token)
        self.assertEqual(Cart.objects.filter(session_key=token).count(), 1)

    def test_add_item_without_token_creates_cart_and_returns_token(self):
        """Guest adding an item without a token gets a new cart with cart_token."""
        resp = self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data["cart_token"])
        self.assertEqual(resp.data["item_count"], 1)

    def test_add_item_with_token_persists_to_same_cart(self):
        """Items added via X-Cart-Token accumulate in the same guest cart."""
        resp1 = self.client.get(CART_URL)
        token = resp1.data["cart_token"]

        resp2 = self.client.post(
            ADD_URL,
            {"variant_id": self.variant.pk, "quantity": 2},
            HTTP_X_CART_TOKEN=token,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["item_count"], 1)

        # Second add combines quantities
        resp3 = self.client.post(
            ADD_URL,
            {"variant_id": self.variant.pk, "quantity": 3},
            HTTP_X_CART_TOKEN=token,
        )
        self.assertEqual(resp3.data["items"][0]["quantity"], 5)

    def test_quantity_capped_at_99_for_guest(self):
        resp1 = self.client.get(CART_URL)
        token = resp1.data["cart_token"]
        self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 90}, HTTP_X_CART_TOKEN=token)
        resp = self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 90}, HTTP_X_CART_TOKEN=token)
        self.assertEqual(resp.data["items"][0]["quantity"], 99)

    def test_clear_cart_removes_all_items(self):
        resp1 = self.client.get(CART_URL)
        token = resp1.data["cart_token"]
        self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 2}, HTTP_X_CART_TOKEN=token)

        resp = self.client.delete(CLEAR_URL, HTTP_X_CART_TOKEN=token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["item_count"], 0)

    def test_cart_token_is_null_for_authenticated_user(self):
        """Authenticated user cart response has cart_token=null."""
        user = _make_user(mobile="+919800000099")
        self.client.credentials(**_auth_header(user))
        resp = self.client.get(CART_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.data["cart_token"])


# ── Authenticated cart regression guard ─────────────────────────────────────

class AuthenticatedCartTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919800000002")
        self.client.credentials(**_auth_header(self.user))
        self.variant = _make_variant(size_ml=50)

    def test_add_and_retrieve_cart(self):
        self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1})
        resp = self.client.get(CART_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["item_count"], 1)

    def test_update_item_quantity(self):
        self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1})
        resp = self.client.get(CART_URL)
        item_id = resp.data["items"][0]["id"]

        resp2 = self.client.patch(f"/api/cart/items/{item_id}/", {"quantity": 5})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["items"][0]["quantity"], 5)

    def test_remove_item(self):
        self.client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1})
        resp = self.client.get(CART_URL)
        item_id = resp.data["items"][0]["id"]

        resp2 = self.client.delete(f"/api/cart/items/{item_id}/")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data["item_count"], 0)


# ── Cart merge service unit tests ────────────────────────────────────────────

class CartMergeServiceTests(TestCase):

    def setUp(self):
        self.user = _make_user(mobile="+919800000003")
        self.variant1 = _make_variant(size_ml=10)
        self.variant2 = _make_variant(size_ml=30)

    def _make_guest_cart(self, token="abc123"):
        return Cart.objects.create(session_key=token)

    def test_merge_moves_items_to_user_cart(self):
        guest_cart = self._make_guest_cart()
        CartItem.objects.create(cart=guest_cart, variant=self.variant1, quantity=2)

        merge_guest_cart(guest_cart.session_key, self.user)

        user_cart = Cart.objects.get(user=self.user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().quantity, 2)

    def test_merge_adds_quantities_for_existing_variant(self):
        guest_cart = self._make_guest_cart(token="token_add")
        CartItem.objects.create(cart=guest_cart, variant=self.variant1, quantity=3)

        user_cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=user_cart, variant=self.variant1, quantity=5)

        merge_guest_cart(guest_cart.session_key, self.user)

        item = CartItem.objects.get(cart=user_cart, variant=self.variant1)
        self.assertEqual(item.quantity, 8)

    def test_merge_caps_quantity_at_99(self):
        guest_cart = self._make_guest_cart(token="token_cap")
        CartItem.objects.create(cart=guest_cart, variant=self.variant1, quantity=90)

        user_cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=user_cart, variant=self.variant1, quantity=50)

        merge_guest_cart(guest_cart.session_key, self.user)

        item = CartItem.objects.get(cart=user_cart, variant=self.variant1)
        self.assertEqual(item.quantity, 99)

    def test_merge_deletes_guest_cart(self):
        guest_cart = self._make_guest_cart(token="token_del")
        CartItem.objects.create(cart=guest_cart, variant=self.variant1, quantity=1)

        merge_guest_cart(guest_cart.session_key, self.user)

        self.assertFalse(Cart.objects.filter(session_key="token_del").exists())

    def test_merge_with_invalid_token_does_nothing(self):
        """Non-existent cart token is silently ignored."""
        merge_guest_cart("nonexistent_token", self.user)
        self.assertFalse(Cart.objects.filter(user=self.user).exists())

    def test_merge_empty_guest_cart_deletes_it(self):
        guest_cart = self._make_guest_cart(token="token_empty")
        merge_guest_cart(guest_cart.session_key, self.user)
        self.assertFalse(Cart.objects.filter(session_key="token_empty").exists())


# ── Ownership/authorization (stabilization pass — no dedicated test existed) ──

class CartOwnershipTests(TestCase):
    """_get_cart_item() in apps/cart/views.py already scopes its query by
    owner (cart__user=request.user, or cart__session_key=<token> for a
    guest) — this proves that scoping actually holds at the HTTP layer,
    since item_id in the URL is a raw integer pk with no token-cross-check
    other than that query."""

    def setUp(self):
        self.variant = _make_variant()

    def test_guest_a_cannot_modify_guest_b_cart_item(self):
        client_a = APIClient()
        client_a.credentials(HTTP_X_CART_TOKEN="token_guest_a")
        resp = client_a.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1}, format="json")
        item_id = resp.data["items"][0]["id"]

        client_b = APIClient()
        client_b.credentials(HTTP_X_CART_TOKEN="token_guest_b")
        patch_resp = client_b.patch(f"/api/cart/items/{item_id}/", {"quantity": 5}, format="json")
        self.assertEqual(patch_resp.status_code, 404)

        delete_resp = client_b.delete(f"/api/cart/items/{item_id}/")
        self.assertEqual(delete_resp.status_code, 404)

        # Item A's cart is untouched by B's failed attempts.
        check_resp = client_a.get(CART_URL)
        self.assertEqual(check_resp.data["items"][0]["quantity"], 1)

    def test_authenticated_user_cannot_modify_another_users_cart_item(self):
        user_a = _make_user(mobile="+919800000010")
        user_b = _make_user(mobile="+919800000011")

        client_a = APIClient()
        client_a.credentials(**_auth_header(user_a))
        resp = client_a.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1}, format="json")
        item_id = resp.data["items"][0]["id"]

        client_b = APIClient()
        client_b.credentials(**_auth_header(user_b))
        patch_resp = client_b.patch(f"/api/cart/items/{item_id}/", {"quantity": 5}, format="json")
        self.assertEqual(patch_resp.status_code, 404)

    def test_guest_cannot_access_item_without_sending_a_token(self):
        client = APIClient()
        client.credentials(HTTP_X_CART_TOKEN="token_guest_c")
        resp = client.post(ADD_URL, {"variant_id": self.variant.pk, "quantity": 1}, format="json")
        item_id = resp.data["items"][0]["id"]

        anon = APIClient()  # no X-Cart-Token at all
        patch_resp = anon.patch(f"/api/cart/items/{item_id}/", {"quantity": 2}, format="json")
        self.assertEqual(patch_resp.status_code, 404)
