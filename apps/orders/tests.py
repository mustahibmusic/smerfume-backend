"""
Tests for apps.orders.

Test groups:
    CheckoutConcurrencyTests       — SEC-003: two concurrent checkout
        requests on the same cart produce exactly one Order.
    CartRowLockDeterministicTests  — SEC-003: proves the Cart row lock
        itself blocks a second concurrent acquisition attempt.
"""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.cart.models import Cart, CartItem
from apps.catalog.models import Brand, Category, Product, ProductEdition, ProductVariant
from apps.inventory.models import (
    DecantSource,
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockTransaction,
    Warehouse,
)
from apps.inventory.services import reservation as reservation_service
from apps.orders import services as order_services
from apps.orders.admin import OrderAdmin
from apps.orders.models import Order, OrderItem, Refund, RefundAdjustment, Return, ReturnItem

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
    variant = ProductVariant.objects.create(
        edition=edition, size_ml=size_ml,
        selling_price=selling_price, mrp=mrp,
        sku=f"TEST-{uuid.uuid4().hex[:10]}",
    )
    InventoryStock.objects.create(
        variant=variant,
        warehouse=Warehouse.objects.get(is_default=True),
        stock_type=InventoryStock.STOCK_TYPE_RETAIL,
        quantity=Decimal("100"),
    )
    return variant


def _make_decant_pair(source_size_ml=100, decant_volume_ml=10, source_retail_quantity=10):
    """Create a source (full-size) variant with retail stock, a decant
    variant mapped to it via DecantSource, and no independent stock for the
    decant variant — matching the approved design (decants have no
    independent sellable inventory pool)."""
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
    warehouse = Warehouse.objects.get(is_default=True)

    source = ProductVariant.objects.create(
        edition=edition, size_ml=source_size_ml,
        selling_price="5000.00", mrp="5500.00",
        sku=f"TEST-SRC-{uuid.uuid4().hex[:10]}",
    )
    InventoryStock.objects.create(
        variant=source, warehouse=warehouse,
        stock_type=InventoryStock.STOCK_TYPE_RETAIL,
        quantity=Decimal(source_retail_quantity),
    )
    decant = ProductVariant.objects.create(
        edition=edition, size_ml=decant_volume_ml, is_decant=True,
        selling_price="500.00", mrp="600.00",
        sku=f"TEST-DEC-{uuid.uuid4().hex[:10]}",
    )
    DecantSource.objects.create(
        decant_variant=decant, source_variant=source,
        decant_volume_ml=Decimal(decant_volume_ml),
    )
    return source, decant


# ── Checkout concurrency (SEC-003) ───────────────────────────────────────────
#
# Two tests, deliberately at different levels, per explicit instruction that
# a Barrier-based near-simultaneous-start test alone is not sufficient
# evidence that a row lock is what's serializing the requests:
#
#   CheckoutConcurrencyTests        — end-to-end HTTP evidence that the fix
#       produces exactly one Order when two checkout requests race.
#   CartRowLockDeterministicTests   — lower-level, Event-synchronized proof
#       that a second select_for_update() on the same Cart row genuinely
#       blocks while the first transaction holds it, independent of thread
#       scheduling luck.

class CheckoutConcurrencyTests(TransactionTestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.user = _make_user()
        self.variant = _make_variant()
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, variant=self.variant, quantity=1)

    def test_concurrent_checkout_on_same_cart_creates_only_one_order(self):
        results = {}
        barrier = threading.Barrier(2)

        def _run(key):
            barrier.wait()
            client = APIClient()
            client.credentials(**_auth_header(self.user))
            try:
                resp = client.post(
                    CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json"
                )
                results[key] = resp.status_code
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("a",))
        t2 = threading.Thread(target=_run, args=("b",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), [201, 400])
        self.assertEqual(Order.objects.filter(user=self.user).count(), 1)


class CartRowLockDeterministicTests(TransactionTestCase):
    """Proves, without relying on thread-scheduling luck, that a second
    attempt to lock the same Cart row genuinely blocks while the first
    transaction holds the lock, and proceeds only after it's released."""

    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.user = _make_user()
        self.variant = _make_variant()
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, variant=self.variant, quantity=1)

    def test_second_lock_attempt_blocks_until_first_transaction_commits(self):
        from django.db import transaction as db_transaction

        a_locked = threading.Event()
        release_a = threading.Event()
        b_locked = threading.Event()

        def _hold_lock_a():
            try:
                with db_transaction.atomic():
                    Cart.objects.select_for_update().get(pk=self.cart.pk)
                    a_locked.set()
                    release_a.wait(timeout=5)
            finally:
                connection.close()

        def _attempt_lock_b():
            try:
                with db_transaction.atomic():
                    Cart.objects.select_for_update().get(pk=self.cart.pk)
                    b_locked.set()
            finally:
                connection.close()

        t_a = threading.Thread(target=_hold_lock_a)
        t_a.start()
        self.assertTrue(a_locked.wait(timeout=5), "Thread A never acquired the lock")

        t_b = threading.Thread(target=_attempt_lock_b)
        t_b.start()

        # While A still holds the lock, B must NOT have acquired it yet.
        got_it_early = b_locked.wait(timeout=0.5)
        self.assertFalse(got_it_early, "select_for_update() did not block -- Cart row is not actually locked")

        release_a.set()
        t_a.join(timeout=5)

        # Now that A released (committed), B must acquire it promptly.
        self.assertTrue(b_locked.wait(timeout=5), "Thread B never acquired the lock after A released it")
        t_b.join(timeout=5)
