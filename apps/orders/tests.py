"""
Tests for apps.orders.


Test groups:
    CheckoutConcurrencyTests       — SEC-003: two concurrent checkout
        requests on the same cart produce exactly one Order.
    CartRowLockDeterministicTests  — SEC-003: proves the Cart row lock
        itself blocks a second concurrent acquisition attempt.
    AuthenticatedCheckoutTests    — existing checkout flow (regression guard)
    GuestCheckoutTests            — guest checkout end-to-end
    CheckoutInventoryTests        — Phase 3: reservation integration, insufficient stock
    CheckoutFinancialSnapshotTests — Phase 3: discount/shipping/tax snapshot fields
    AllocateDiscountTests         — order_services.allocate_discount unit tests
    CODVerificationTests          — order_services.verify_cod_order
    OrderDeliveredAtTests         — Order.save() delivered_at auto-population
    PackOrderDirectRetailTests    — Phase 4: pack_order for direct retail lines
    PackOrderDecantTests          — Phase 4: pack_order FIFO/bottle-opening for decants
    PackOrderRollbackTests        — Phase 4: atomicity across a failed packing attempt
    CreateReturnEligibilityTests   — Phase 5.1: create_return eligibility rules
    ReturnLifecycleTests           — Phase 5.1: Return status transitions
    ReturnEntitlementConcurrencyTests — Phase 5.1: concurrent-request safety
    ReturnAPITests                 — Phase 5.1b: customer-facing Return API
    OrderStatusAdminLockdownTests — status is not editable through the admin form
    OperationalStatusServiceTests — Phase 4.5: shipped/delivered/cancel services
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
from apps.orders.models import Order, OrderItem, Return, ReturnItem

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


# ── Phase 3: inventory reservation integration ────────────────────────────────

class CheckoutInventoryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_checkout_reserves_inventory(self):
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock_before.quantity_reserved, Decimal("0"))

        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 201)

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock_after.quantity_reserved, Decimal("2"))

        order = Order.objects.get(order_number=resp.data["order_number"])
        order_item = order.items.get()
        reservation = StockReservation.objects.get(order_item=order_item)
        self.assertEqual(reservation.status, StockReservation.STATUS_HELD)
        self.assertEqual(reservation.quantity, Decimal("2"))

    def test_checkout_insufficient_stock_rejected_and_rolls_back(self):
        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        stock.quantity = Decimal("1")  # cart wants 2
        stock.save(update_fields=["quantity"])

        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 400)

        self.assertEqual(Order.objects.count(), 0)
        cart = Cart.objects.get(user=self.user)
        self.assertEqual(cart.items.count(), 1)  # cart untouched — rolled back

        stock.refresh_from_db()
        self.assertEqual(stock.quantity_reserved, Decimal("0"))  # nothing partially reserved


class CheckoutFinancialSnapshotTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.variant = _make_variant(selling_price="500.00")
        self.variant.shipping_surcharge = Decimal("50.00")
        self.variant.save(update_fields=["shipping_surcharge"])
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=3)

    def test_financial_snapshot_fields_populated(self):
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 201)

        order = Order.objects.get(order_number=resp.data["order_number"])
        item = order.items.get()

        self.assertEqual(item.gross_line_amount, Decimal("1500.00"))  # 500 * 3
        self.assertEqual(item.discount_allocated, Decimal("0.00"))    # no discount engine yet
        self.assertEqual(item.net_line_amount, Decimal("1500.00"))
        self.assertEqual(item.tax_amount, Decimal("0.00"))
        self.assertEqual(item.final_paid_line_amount, Decimal("1500.00"))
        self.assertEqual(item.shipping_surcharge, Decimal("150.00"))  # 50 * 3

        self.assertEqual(order.base_shipping_charge, Decimal("0.00"))
        self.assertEqual(order.shipping_charge, Decimal("150.00"))
        self.assertEqual(order.total, order.subtotal + order.shipping_charge)
        self.assertEqual(order.payment_method, Order.PAYMENT_METHOD_COD)


class _FakeCartItem:
    """Minimal stand-in with the .id/.line_total attributes allocate_discount needs."""

    def __init__(self, id, line_total):
        self.id = id
        self.line_total = line_total


class AllocateDiscountTests(TestCase):
    def test_zero_discount_returns_all_zero(self):
        items = [_FakeCartItem(1, Decimal("100.00")), _FakeCartItem(2, Decimal("200.00"))]
        result = order_services.allocate_discount(items, Decimal("0.00"))
        self.assertEqual(result, {1: Decimal("0.00"), 2: Decimal("0.00")})

    def test_proportional_allocation_sums_exactly(self):
        items = [_FakeCartItem(1, Decimal("300.00")), _FakeCartItem(2, Decimal("700.00"))]
        result = order_services.allocate_discount(items, Decimal("100.00"))
        self.assertEqual(result[1], Decimal("30.00"))
        self.assertEqual(result[2], Decimal("70.00"))
        self.assertEqual(sum(result.values()), Decimal("100.00"))

    def test_rounding_remainder_goes_to_last_item(self):
        items = [
            _FakeCartItem(1, Decimal("100.00")),
            _FakeCartItem(2, Decimal("100.00")),
            _FakeCartItem(3, Decimal("100.00")),
        ]
        result = order_services.allocate_discount(items, Decimal("10.00"))
        self.assertEqual(sum(result.values()), Decimal("10.00"))

    def test_empty_items_returns_empty(self):
        self.assertEqual(order_services.allocate_discount([], Decimal("50.00")), {})


class CODVerificationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000099", username="staffuser")
        self.variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.order = Order.objects.get(order_number=resp.data["order_number"])

    def test_verify_cod_order_confirms_order_and_reservations(self):
        self.assertEqual(self.order.status, Order.STATUS_PENDING)
        order_services.verify_cod_order(self.order, verified_by=self.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)
        self.assertEqual(self.order.cod_verified_by, self.staff)
        self.assertIsNotNone(self.order.cod_verified_at)

        for reservation in StockReservation.objects.filter(order_item__order=self.order):
            self.assertEqual(reservation.status, StockReservation.STATUS_CONFIRMED)

    def test_cannot_verify_already_confirmed_order(self):
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        with self.assertRaises(order_services.CODVerificationError):
            order_services.verify_cod_order(self.order, verified_by=self.staff)

    def test_cannot_verify_prepaid_order(self):
        self.order.payment_method = Order.PAYMENT_METHOD_PREPAID
        self.order.save(update_fields=["payment_method"])
        with self.assertRaises(order_services.CODVerificationError):
            order_services.verify_cod_order(self.order, verified_by=self.staff)


class OrderDeliveredAtTests(TestCase):
    def setUp(self):
        self.user = _make_user()

    def test_delivered_at_set_on_transition_to_delivered(self):
        order = Order.objects.create(
            user=self.user, order_number="SMR-DELIVTEST-000001",
            subtotal=Decimal("100.00"), total=Decimal("100.00"),
        )
        self.assertIsNone(order.delivered_at)

        order.status = Order.STATUS_DELIVERED
        order.save()

        order.refresh_from_db()
        self.assertIsNotNone(order.delivered_at)

    def test_delivered_at_not_overwritten_on_subsequent_saves(self):
        order = Order.objects.create(
            user=self.user, order_number="SMR-DELIVTEST-000002",
            subtotal=Decimal("100.00"), total=Decimal("100.00"),
        )
        order.status = Order.STATUS_DELIVERED
        order.save()
        order.refresh_from_db()
        first_delivered_at = order.delivered_at
        self.assertIsNotNone(first_delivered_at)

        order.customer_notes = "updated"
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.delivered_at, first_delivered_at)

    def test_delivered_at_not_set_for_other_statuses(self):
        order = Order.objects.create(
            user=self.user, order_number="SMR-DELIVTEST-000003",
            subtotal=Decimal("100.00"), total=Decimal("100.00"),
        )
        order.status = Order.STATUS_PROCESSING
        order.save()
        self.assertIsNone(order.delivered_at)


# ── Phase 4: packing / exact reservation consumption ──────────────────────────

class PackOrderDirectRetailTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000090", username="packstaff1")
        self.variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=3)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.order = Order.objects.get(order_number=resp.data["order_number"])
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_pack_rejects_order_not_yet_confirmed(self):
        with self.assertRaises(order_services.OrderPackingError):
            order_services.pack_order(self.order, performed_by=self.staff)

    def test_pack_consumes_exact_reserved_quantity_and_advances_status(self):
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock_before.quantity, Decimal("100"))
        self.assertEqual(stock_before.quantity_reserved, Decimal("3"))

        order_services.pack_order(self.order, performed_by=self.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PROCESSING)

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock_after.quantity, Decimal("97"))
        self.assertEqual(stock_after.quantity_reserved, Decimal("0"))

        reservation = StockReservation.objects.get(order_item__order=self.order)
        self.assertEqual(reservation.status, StockReservation.STATUS_CONSUMED)

    def test_pack_creates_sale_out_movement_linked_to_order_item(self):
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        order_item = self.order.items.get()
        order_services.pack_order(self.order, performed_by=self.staff)

        movement = StockMovement.objects.get(source_order_item=order_item)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_SALE_OUT)
        self.assertEqual(movement.quantity_delta, Decimal("-3"))
        self.assertEqual(movement.performed_by, self.staff)

    def test_cannot_pack_the_same_order_twice(self):
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        order_services.pack_order(self.order, performed_by=self.staff)
        with self.assertRaises(order_services.OrderPackingError):
            order_services.pack_order(self.order, performed_by=self.staff)

    def test_shipped_and_delivered_do_not_deduct_inventory_again(self):
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        order_services.pack_order(self.order, performed_by=self.staff)

        stock_after_pack = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        movement_count_after_pack = StockMovement.objects.count()

        self.order.refresh_from_db()
        self.order.status = Order.STATUS_SHIPPED
        self.order.save()
        self.order.status = Order.STATUS_DELIVERED
        self.order.save()

        stock_after_delivery = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after_delivery, stock_after_pack)
        self.assertEqual(StockMovement.objects.count(), movement_count_after_pack)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)


class PackOrderDecantTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000091", username="packstaff2")
        self.warehouse = Warehouse.objects.get(is_default=True)

    def _checkout_decant(self, source, decant, quantity):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=decant, quantity=quantity)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 201)
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(order, verified_by=self.staff)
        return order

    def test_pack_decant_from_existing_partial_lot_consumes_exact_lot(self):
        source, decant = _make_decant_pair(source_size_ml=100, decant_volume_ml=10, source_retail_quantity=10)
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        lot = PartialBottleLot.objects.create(
            variant=source, warehouse=self.warehouse, remaining_ml=Decimal("50.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )
        order = self._checkout_decant(source, decant, quantity=1)  # 10ml needed

        order_services.pack_order(order, performed_by=self.staff)

        retail = InventoryStock.objects.get(variant=source, warehouse=self.warehouse, stock_type="retail")
        self.assertEqual(retail.quantity, Decimal("10"))  # no bottle opened — no decrement

        lot.refresh_from_db()
        self.assertEqual(lot.remaining_ml, Decimal("40.00"))
        self.assertEqual(lot.reserved_ml, Decimal("0.00"))
        self.assertFalse(lot.is_depleted)

        # No independent stock pool for the decant variant itself.
        self.assertFalse(InventoryStock.objects.filter(variant=decant).exists())

    def test_pack_decant_opens_bottle_and_creates_leftover_partial_lot(self):
        source, decant = _make_decant_pair(source_size_ml=100, decant_volume_ml=10, source_retail_quantity=10)
        order = self._checkout_decant(source, decant, quantity=1)  # 10ml, no partial stock exists

        order_services.pack_order(order, performed_by=self.staff)

        retail = InventoryStock.objects.get(variant=source, warehouse=self.warehouse, stock_type="retail")
        self.assertEqual(retail.quantity, Decimal("9"))  # one bottle opened

        lot = PartialBottleLot.objects.get(variant=source, warehouse=self.warehouse)
        self.assertEqual(lot.remaining_ml, Decimal("90.00"))  # nothing lost
        self.assertFalse(lot.is_depleted)

        movements = list(StockMovement.objects.filter(
            source_order_item=order.items.get()
        ).order_by("id"))
        self.assertEqual(
            [m.movement_type for m in movements],
            [
                StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT,
                StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN,
                StockMovement.MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL,
            ],
        )
        group_ids = {m.transaction_group_id for m in movements}
        self.assertEqual(len(group_ids), 1)  # all grouped under one StockTransaction

    def test_pack_decant_mixed_partial_and_bottle_opening(self):
        """15ml already sits in a partial lot; a 20ml order draws all 15ml
        from it, then must open a bottle for the remaining 5ml — leaving
        95ml as a fresh partial lot. Nothing is lost or double-counted."""
        source, decant = _make_decant_pair(source_size_ml=100, decant_volume_ml=20, source_retail_quantity=5)
        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_DECANT_BOTTLE_OPENED
        )
        existing_lot = PartialBottleLot.objects.create(
            variant=source, warehouse=self.warehouse, remaining_ml=Decimal("15.00"),
            opened_at=timezone.now(), source_transaction=txn,
        )
        order = self._checkout_decant(source, decant, quantity=1)  # 20ml needed

        order_services.pack_order(order, performed_by=self.staff)

        existing_lot.refresh_from_db()
        self.assertEqual(existing_lot.remaining_ml, Decimal("0.00"))
        self.assertTrue(existing_lot.is_depleted)

        retail = InventoryStock.objects.get(variant=source, warehouse=self.warehouse, stock_type="retail")
        self.assertEqual(retail.quantity, Decimal("4"))  # exactly one new bottle opened

        new_lot = PartialBottleLot.objects.exclude(pk=existing_lot.pk).get(
            variant=source, warehouse=self.warehouse
        )
        self.assertEqual(new_lot.remaining_ml, Decimal("95.00"))  # 100 opened - 5 claimed


class PackOrderRollbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000092", username="packstaff3")
        self.variant_a = _make_variant()
        self.variant_b = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant_a, quantity=1)
        CartItem.objects.create(cart=cart, variant=self.variant_b, quantity=1)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_failed_packing_rolls_back_all_inventory_changes(self):
        stock_a_before = InventoryStock.objects.get(
            variant=self.variant_a, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        stock_b_before = InventoryStock.objects.get(
            variant=self.variant_b, warehouse=self.warehouse, stock_type="retail"
        ).quantity

        original_consume = reservation_service.consume_reservation
        call_count = {"n": 0}

        def _flaky_consume(reservation, performed_by=None):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated failure mid-packing")
            return original_consume(reservation, performed_by=performed_by)

        with patch(
            "apps.orders.services.reservation_service.consume_reservation",
            side_effect=_flaky_consume,
        ):
            with self.assertRaises(RuntimeError):
                order_services.pack_order(self.order, performed_by=self.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)  # never advanced

        stock_a_after = InventoryStock.objects.get(
            variant=self.variant_a, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        stock_b_after = InventoryStock.objects.get(
            variant=self.variant_b, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        # Item A "succeeded" before item B's simulated failure — but the
        # whole packing transaction must roll back together.
        self.assertEqual(stock_a_after, stock_a_before)
        self.assertEqual(stock_b_after, stock_b_before)

        for res in StockReservation.objects.filter(order_item__order=self.order):
            self.assertEqual(res.status, StockReservation.STATUS_CONFIRMED)


# ── Admin integrity: status is not directly editable ──────────────────────────

class OrderStatusAdminLockdownTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            username="adminstaff", email="adminstaff@example.com", password="testpass123",
        )
        self.user = _make_user(mobile="+919700000093")
        self.order = Order.objects.create(
            user=self.user, order_number="SMR-ADMINTEST-000001",
            subtotal=Decimal("500.00"), total=Decimal("500.00"),
            status=Order.STATUS_CONFIRMED,
        )
        self.order_admin = OrderAdmin(Order, django_admin.site)

    def test_status_is_declared_readonly(self):
        self.assertIn("status", self.order_admin.readonly_fields)

    def test_admin_form_excludes_status_from_editable_fields(self):
        """readonly_fields are excluded from the ModelForm entirely — this
        is what actually makes the field non-editable, not just visually
        disabled. Proving it at the form-class level, not just by rendering."""
        request = RequestFactory().get("/")
        request.user = self.staff
        form_class = self.order_admin.get_form(request, self.order)
        self.assertNotIn("status", form_class.base_fields)

    def test_admin_change_page_does_not_render_status_as_an_editable_widget(self):
        self.client.login(username="adminstaff@example.com", password="testpass123")
        url = f"/admin/orders/order/{self.order.pk}/change/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="status"')
        # Still visible for staff to see, just not as an editable input —
        # readonly fields render their human-readable choice label.
        self.assertContains(resp, "Confirmed")

    def test_pack_order_remains_the_only_path_to_processing(self):
        """Regression guard: the lockdown must not have broken the service
        path itself — pack_order still works exactly as Phase 4 left it."""
        variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=variant, quantity=1)
        self.client_api = APIClient()
        self.client_api.credentials(**_auth_header(self.user))
        resp = self.client_api.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])

        with self.assertRaises(order_services.OrderPackingError):
            order_services.pack_order(order, performed_by=self.staff)  # still pending

        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PROCESSING)


# ── Phase 4.5: operational status services ────────────────────────────────────

class OperationalStatusServiceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000094", username="opsstaff")
        self.variant = _make_variant()
        self.warehouse = Warehouse.objects.get(is_default=True)

    def _new_order(self, quantity=2):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=quantity)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.assertEqual(resp.status_code, 201)
        return Order.objects.get(order_number=resp.data["order_number"])

    def _order_at_processing(self, quantity=2):
        order = self._new_order(quantity)
        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order.refresh_from_db()
        return order

    # ── mark_order_shipped ──────────────────────────────────────────────

    def test_processing_to_shipped_works(self):
        order = self._order_at_processing()
        order_services.mark_order_shipped(order, tracking_number="TRK123")

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_SHIPPED)
        self.assertEqual(order.tracking_number, "TRK123")

    def test_mark_shipped_rejected_from_non_processing_status(self):
        order = self._new_order()  # still pending
        with self.assertRaises(order_services.OrderTransitionError):
            order_services.mark_order_shipped(order)

    def test_mark_shipped_without_tracking_number_is_allowed(self):
        order = self._order_at_processing()
        order_services.mark_order_shipped(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_SHIPPED)
        self.assertIsNone(order.tracking_number)

    def test_shipped_does_not_mutate_inventory(self):
        order = self._order_at_processing()
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        movement_count_before = StockMovement.objects.count()

        order_services.mark_order_shipped(order, tracking_number="TRK456")

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after, stock_before)
        self.assertEqual(StockMovement.objects.count(), movement_count_before)

    # ── mark_order_delivered ────────────────────────────────────────────

    def test_shipped_to_delivered_works_and_sets_delivered_at(self):
        order = self._order_at_processing()
        order_services.mark_order_shipped(order)
        order.refresh_from_db()
        self.assertIsNone(order.delivered_at)

        order_services.mark_order_delivered(order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_DELIVERED)
        self.assertIsNotNone(order.delivered_at)

    def test_mark_delivered_rejected_from_non_shipped_status(self):
        order = self._order_at_processing()  # processing, not shipped
        with self.assertRaises(order_services.OrderTransitionError):
            order_services.mark_order_delivered(order)

    def test_delivered_does_not_mutate_inventory(self):
        order = self._order_at_processing()
        order_services.mark_order_shipped(order)
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        movement_count_before = StockMovement.objects.count()

        order_services.mark_order_delivered(order)

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after, stock_before)
        self.assertEqual(StockMovement.objects.count(), movement_count_before)

    # ── cancel_order ─────────────────────────────────────────────────────

    def test_cancelling_from_pending_releases_reservation(self):
        order = self._new_order(quantity=3)
        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("3"))

        order_services.cancel_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity_reserved, Decimal("0"))
        reservation = StockReservation.objects.get(order_item__order=order)
        self.assertEqual(reservation.status, StockReservation.STATUS_RELEASED)

    def test_cancelling_from_confirmed_releases_reservation(self):
        order = self._new_order(quantity=4)
        order_services.verify_cod_order(order, verified_by=self.staff)
        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("4"))

        order_services.cancel_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_CANCELLED)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity_reserved, Decimal("0"))
        reservation = StockReservation.objects.get(order_item__order=order)
        self.assertEqual(reservation.status, StockReservation.STATUS_RELEASED)

    def test_cancelling_after_processing_is_rejected(self):
        order = self._order_at_processing()
        with self.assertRaises(order_services.OrderTransitionError):
            order_services.cancel_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PROCESSING)  # unchanged

    def test_cancelling_after_shipped_is_rejected(self):
        order = self._order_at_processing()
        order_services.mark_order_shipped(order)
        with self.assertRaises(order_services.OrderTransitionError):
            order_services.cancel_order(order)

    def test_reservation_cannot_be_released_twice_via_repeated_cancellation(self):
        order = self._new_order(quantity=2)
        order_services.cancel_order(order)

        with self.assertRaises(order_services.OrderTransitionError):
            order_services.cancel_order(order)  # already cancelled

        stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        )
        self.assertEqual(stock.quantity_reserved, Decimal("0"))  # never went negative
        reservation = StockReservation.objects.get(order_item__order=order)
        self.assertEqual(reservation.status, StockReservation.STATUS_RELEASED)
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


class CreateReturnEligibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000095", username="returnstaff1")
        self.variant = _make_variant()

    def _delivered_order(self, quantity=3):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=quantity)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order_services.mark_order_shipped(order)
        order_services.mark_order_delivered(order)
        order.refresh_from_db()
        return order

    def test_valid_return_request_succeeds(self):
        order = self._delivered_order(quantity=3)
        order_item = order.items.get()

        return_request = order_services.create_return(order, items=[
            {"order_item": order_item, "reason": ReturnItem.REASON_WRONG_VARIANT,
             "requested_quantity": 1, "reason_notes": "wrong size shipped"},
        ])

        self.assertEqual(return_request.status, Return.STATUS_REQUESTED)
        item = return_request.items.get()
        self.assertEqual(item.order_item, order_item)
        self.assertEqual(item.reason, ReturnItem.REASON_WRONG_VARIANT)
        self.assertEqual(item.requested_quantity, 1)

    def test_rejected_when_order_not_delivered(self):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])  # still pending
        order_item = order.items.get()

        with self.assertRaises(order_services.ReturnEligibilityError):
            order_services.create_return(order, items=[
                {"order_item": order_item, "reason": ReturnItem.REASON_WRONG_ITEM, "requested_quantity": 1},
            ])
        self.assertEqual(Return.objects.count(), 0)

    def test_rejected_outside_return_window(self):
        order = self._delivered_order(quantity=1)
        order.delivered_at = timezone.now() - timedelta(days=2)
        order.save(update_fields=["delivered_at"])
        order_item = order.items.get()

        with self.assertRaises(order_services.ReturnEligibilityError):
            order_services.create_return(order, items=[
                {"order_item": order_item, "reason": ReturnItem.REASON_TRANSIT_DAMAGE, "requested_quantity": 1},
            ])

    def test_invalid_reason_rejected(self):
        order = self._delivered_order(quantity=1)
        order_item = order.items.get()

        with self.assertRaises(order_services.ReturnEligibilityError):
            order_services.create_return(order, items=[
                {"order_item": order_item, "reason": "did_not_like_fragrance", "requested_quantity": 1},
            ])
        # Change-of-mind reasons simply have no matching choice at all.
        self.assertNotIn("did_not_like_fragrance", dict(ReturnItem.REASON_CHOICES))

    def test_requested_quantity_cannot_exceed_purchased_quantity(self):
        order = self._delivered_order(quantity=2)
        order_item = order.items.get()

        with self.assertRaises(order_services.ReturnEligibilityError):
            order_services.create_return(order, items=[
                {"order_item": order_item, "reason": ReturnItem.REASON_MISSING_ITEMS, "requested_quantity": 3},
            ])

    def test_cumulative_quantity_across_multiple_returns_is_capped(self):
        order = self._delivered_order(quantity=3)
        order_item = order.items.get()

        order_services.create_return(order, items=[
            {"order_item": order_item, "reason": ReturnItem.REASON_WRONG_VARIANT, "requested_quantity": 1},
        ])
        order_services.create_return(order, items=[
            {"order_item": order_item, "reason": ReturnItem.REASON_TRANSIT_DAMAGE, "requested_quantity": 2},
        ])
        # 1 + 2 == 3, fully claimed — a further return must be rejected.
        with self.assertRaises(order_services.ReturnEligibilityError):
            order_services.create_return(order, items=[
                {"order_item": order_item, "reason": ReturnItem.REASON_MISSING_ITEMS, "requested_quantity": 1},
            ])

    def test_rejected_return_releases_entitlement(self):
        order = self._delivered_order(quantity=2)
        order_item = order.items.get()

        first = order_services.create_return(order, items=[
            {"order_item": order_item, "reason": ReturnItem.REASON_WRONG_VARIANT, "requested_quantity": 2},
        ])
        order_services.reject_return(first)

        # The full quantity is claimable again since the rejected return no
        # longer counts toward the cumulative entitlement.
        second = order_services.create_return(order, items=[
            {"order_item": order_item, "reason": ReturnItem.REASON_TRANSIT_DAMAGE, "requested_quantity": 2},
        ])
        self.assertEqual(second.items.get().requested_quantity, 2)


class ReturnLifecycleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user()
        self.client.credentials(**_auth_header(self.user))
        self.staff = _make_user(mobile="+919700000096", username="returnstaff2")
        self.variant = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=2)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order_services.mark_order_shipped(order)
        order_services.mark_order_delivered(order)
        order.refresh_from_db()
        self.order_item = order.items.get()
        self.return_request = order_services.create_return(order, items=[
            {"order_item": self.order_item, "reason": ReturnItem.REASON_WRONG_VARIANT, "requested_quantity": 2},
        ])

    def test_full_happy_path_lifecycle(self):
        order_services.approve_return(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_APPROVED)

        order_services.mark_return_in_transit(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_IN_TRANSIT)

        item = self.return_request.items.get()
        item.received_quantity = 2
        item.save(update_fields=["received_quantity"])

        order_services.mark_return_received(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_RECEIVED)

        order_services.start_inspection(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_INSPECTION_PENDING)

    def test_reject_from_requested(self):
        order_services.reject_return(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_REJECTED)

    def test_cannot_approve_twice(self):
        order_services.approve_return(self.return_request)
        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.approve_return(self.return_request)

    def test_mark_received_rejects_when_quantity_not_recorded(self):
        order_services.approve_return(self.return_request)
        order_services.mark_return_in_transit(self.return_request)
        # received_quantity never set on the item.
        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.mark_return_received(self.return_request)

    def test_cannot_skip_lifecycle_states(self):
        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.mark_return_in_transit(self.return_request)  # still requested

    def test_cancel_allowed_before_received(self):
        order_services.approve_return(self.return_request)
        order_services.cancel_return(self.return_request)
        self.return_request.refresh_from_db()
        self.assertEqual(self.return_request.status, Return.STATUS_CANCELLED)

    def test_cancel_rejected_after_received(self):
        order_services.approve_return(self.return_request)
        order_services.mark_return_in_transit(self.return_request)
        item = self.return_request.items.get()
        item.received_quantity = 2
        item.save(update_fields=["received_quantity"])
        order_services.mark_return_received(self.return_request)

        with self.assertRaises(order_services.ReturnTransitionError):
            order_services.cancel_return(self.return_request)


class ReturnEntitlementConcurrencyTests(TransactionTestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.client = APIClient()
        self.user = User.objects.create(
            username="returnconcurrencyuser", mobile_number="+919700000097", role="customer",
        )
        self.user.set_unusable_password()
        self.user.save()
        self.client.credentials(**_auth_header(self.user))
        self.staff = User.objects.create(
            username="returnconcurrencystaff", mobile_number="+919700000098", role="staff",
        )

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
        self.variant = ProductVariant.objects.create(
            edition=edition, size_ml=10, selling_price="500.00", mrp="600.00",
            sku=f"TEST-{uuid.uuid4().hex[:10]}",
        )
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("100"),
        )

        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=3)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        self.order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(self.order, verified_by=self.staff)
        order_services.pack_order(self.order, performed_by=self.staff)
        order_services.mark_order_shipped(self.order)
        order_services.mark_order_delivered(self.order)
        self.order.refresh_from_db()
        self.order_item = self.order.items.get()  # quantity=3

    def test_concurrent_return_requests_cannot_exceed_purchased_quantity(self):
        results = {}
        barrier = threading.Barrier(2)

        def _run(key, quantity):
            barrier.wait()
            try:
                order_services.create_return(self.order, items=[
                    {"order_item": self.order_item, "reason": ReturnItem.REASON_WRONG_VARIANT,
                     "requested_quantity": quantity},
                ])
                results[key] = "ok"
            except order_services.ReturnEligibilityError:
                results[key] = "rejected"
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("a", 2))
        t2 = threading.Thread(target=_run, args=("b", 2))  # 2 + 2 > 3
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), ["ok", "rejected"])
        total_claimed = sum(
            ri.requested_quantity for ri in ReturnItem.objects.filter(order_item=self.order_item)
        )
        self.assertLessEqual(total_claimed, self.order_item.quantity)


# ── Phase 5.1b: customer-facing Return API ─────────────────────────────────────

RETURNS_LIST_URL = "/api/orders/returns/"


def _return_detail_url(public_id):
    return f"/api/orders/returns/{public_id}/"


def _return_cancel_url(public_id):
    return f"/api/orders/returns/{public_id}/cancel/"


def _order_return_create_url(order_number):
    return f"/api/orders/{order_number}/returns/"


class ReturnAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919700000099")
        self.other_user = _make_user(mobile="+919700000100")
        self.staff = _make_user(mobile="+919700000101", username="returnapistaff")
        self.variant = _make_variant()

    def _deliver_order_for(self, user, quantity=3):
        client = APIClient()
        client.credentials(**_auth_header(user))
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=quantity)
        resp = client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order_services.mark_order_shipped(order)
        order_services.mark_order_delivered(order)
        order.refresh_from_db()
        return order

    def test_authenticated_customer_creates_eligible_return(self):
        order = self._deliver_order_for(self.user, quantity=3)
        order_item = order.items.get()
        self.client.credentials(**_auth_header(self.user))

        resp = self.client.post(_order_return_create_url(order.order_number), {
            "items": [
                {"order_item": str(order_item.public_id), "reason": "wrong_variant",
                 "requested_quantity": 1, "reason_notes": "size mismatch"},
            ],
        }, format="json")

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], Return.STATUS_REQUESTED)
        self.assertEqual(len(resp.data["items"]), 1)
        self.assertEqual(resp.data["items"][0]["reason"], "wrong_variant")

    def test_unauthenticated_request_rejected(self):
        order = self._deliver_order_for(self.user, quantity=1)
        order_item = order.items.get()
        anon_client = APIClient()

        resp = anon_client.post(_order_return_create_url(order.order_number), {
            "items": [{"order_item": str(order_item.public_id), "reason": "wrong_item", "requested_quantity": 1}],
        }, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_cannot_create_return_for_another_customers_order(self):
        order = self._deliver_order_for(self.other_user, quantity=1)
        order_item = order.items.get()
        self.client.credentials(**_auth_header(self.user))  # different customer

        resp = self.client.post(_order_return_create_url(order.order_number), {
            "items": [{"order_item": str(order_item.public_id), "reason": "wrong_item", "requested_quantity": 1}],
        }, format="json")
        self.assertEqual(resp.status_code, 404)  # order not found for this user, existence not confirmed
        self.assertEqual(Return.objects.count(), 0)

    def test_cannot_reference_another_customers_order_item(self):
        own_order = self._deliver_order_for(self.user, quantity=1)
        other_order = self._deliver_order_for(self.other_user, quantity=1)
        other_order_item = other_order.items.get()
        self.client.credentials(**_auth_header(self.user))

        resp = self.client.post(_order_return_create_url(own_order.order_number), {
            "items": [
                {"order_item": str(other_order_item.public_id), "reason": "wrong_item", "requested_quantity": 1},
            ],
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Return.objects.count(), 0)

    def test_existing_eligibility_rules_still_apply_via_api(self):
        """Order not yet delivered — the service's eligibility check, not
        anything re-implemented in the API layer, is what rejects this."""
        self.client.credentials(**_auth_header(self.user))
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_item = order.items.get()

        resp = self.client.post(_order_return_create_url(order.order_number), {
            "items": [{"order_item": str(order_item.public_id), "reason": "wrong_item", "requested_quantity": 1}],
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Return.objects.count(), 0)

    def test_list_returns_only_authenticated_customers_returns(self):
        own_order = self._deliver_order_for(self.user, quantity=1)
        other_order = self._deliver_order_for(self.other_user, quantity=1)
        own_return = order_services.create_return(own_order, items=[
            {"order_item": own_order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])
        order_services.create_return(other_order, items=[
            {"order_item": other_order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])

        self.client.credentials(**_auth_header(self.user))
        resp = self.client.get(RETURNS_LIST_URL)
        self.assertEqual(resp.status_code, 200)
        results = resp.data["results"] if isinstance(resp.data, dict) and "results" in resp.data else resp.data
        public_ids = [r["public_id"] for r in results]
        self.assertEqual(public_ids, [str(own_return.public_id)])

    def test_detail_ownership_enforced(self):
        other_order = self._deliver_order_for(self.other_user, quantity=1)
        other_return = order_services.create_return(other_order, items=[
            {"order_item": other_order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])

        self.client.credentials(**_auth_header(self.user))  # different customer
        resp = self.client.get(_return_detail_url(other_return.public_id))
        self.assertEqual(resp.status_code, 404)

    def test_customer_can_cancel_eligible_return(self):
        order = self._deliver_order_for(self.user, quantity=1)
        return_request = order_services.create_return(order, items=[
            {"order_item": order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])
        self.client.credentials(**_auth_header(self.user))

        resp = self.client.post(_return_cancel_url(return_request.public_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], Return.STATUS_CANCELLED)
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_CANCELLED)

    def test_invalid_cancellation_rejected_by_service(self):
        order = self._deliver_order_for(self.user, quantity=1)
        return_request = order_services.create_return(order, items=[
            {"order_item": order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])
        order_services.approve_return(return_request)
        order_services.mark_return_in_transit(return_request)
        item = return_request.items.get()
        item.received_quantity = 1
        item.save(update_fields=["received_quantity"])
        order_services.mark_return_received(return_request)  # no longer cancellable

        self.client.credentials(**_auth_header(self.user))
        resp = self.client.post(_return_cancel_url(return_request.public_id))
        self.assertEqual(resp.status_code, 400)
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_RECEIVED)  # unchanged

    def test_status_field_in_create_payload_is_ignored(self):
        order = self._deliver_order_for(self.user, quantity=1)
        order_item = order.items.get()
        self.client.credentials(**_auth_header(self.user))

        resp = self.client.post(_order_return_create_url(order.order_number), {
            "status": Return.STATUS_APPROVED,  # not a field the serializer accepts
            "items": [{"order_item": str(order_item.public_id), "reason": "wrong_item", "requested_quantity": 1}],
        }, format="json")

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], Return.STATUS_REQUESTED)  # always starts here, unaffected

    def test_no_generic_update_endpoint_exists_for_return(self):
        order = self._deliver_order_for(self.user, quantity=1)
        return_request = order_services.create_return(order, items=[
            {"order_item": order.items.get(), "reason": "wrong_item", "requested_quantity": 1},
        ])
        self.client.credentials(**_auth_header(self.user))

        resp = self.client.patch(_return_detail_url(return_request.public_id), {"status": "approved"}, format="json")
        self.assertEqual(resp.status_code, 405)  # RetrieveAPIView — GET only, no update mixin


def _return_item_ready_for_inspection(client, user, staff, variant, quantity=1, reason=ReturnItem.REASON_WRONG_VARIANT):
    """Drive one order+return through checkout -> delivered -> requested ->
    approved -> in_transit -> received -> inspection_pending, and return the
    single resulting ReturnItem, ready for finalize_return_item()."""
    cart = Cart.objects.create(user=user)
    CartItem.objects.create(cart=cart, variant=variant, quantity=quantity)
    client.credentials(**_auth_header(user))
    resp = client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
    order = Order.objects.get(order_number=resp.data["order_number"])
    order_services.verify_cod_order(order, verified_by=staff)
    order_services.pack_order(order, performed_by=staff)
    order_services.mark_order_shipped(order)
    order_services.mark_order_delivered(order)
    order.refresh_from_db()
    order_item = order.items.get()

    return_request = order_services.create_return(order, items=[
        {"order_item": order_item, "reason": reason, "requested_quantity": quantity},
    ])
    order_services.approve_return(return_request)
    order_services.mark_return_in_transit(return_request)
    item = return_request.items.get()
    item.received_quantity = quantity
    item.save(update_fields=["received_quantity"])
    order_services.mark_return_received(return_request)
    order_services.start_inspection(return_request)
    return return_request.items.get()


class ReturnInspectionRetailTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919700000102")
        self.staff = _make_user(mobile="+919700000103", username="inspectstaff1")
        self.variant = _make_variant()
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_restocked_retail_creates_positive_movement(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=3)
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity

        finalized = order_services.finalize_return_item(
            item, received_quantity=2, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )

        self.assertEqual(finalized.disposition, ReturnItem.DISPOSITION_RESTOCKED_RETAIL)
        self.assertEqual(finalized.inspected_by, self.staff)
        self.assertIsNotNone(finalized.inspected_at)

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after, stock_before + 2)

        movement = StockMovement.objects.get(source_return_item=item)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_RETURN_RESTOCKED_RETAIL)
        self.assertEqual(movement.quantity_delta, Decimal("2"))
        self.assertEqual(movement.reason, StockMovement.REASON_RETURN)
        self.assertEqual(movement.source_order_item, item.order_item)

    def test_damaged_creates_damaged_stock_movement(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=2)

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_DAMAGED,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )

        damaged_stock = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="damaged"
        )
        self.assertEqual(damaged_stock.quantity, Decimal("1"))

        movement = StockMovement.objects.get(source_return_item=item)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_RETURN_DAMAGED)
        self.assertEqual(movement.quantity_delta, Decimal("1"))

    def test_rejected_creates_no_movement(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        movement_count_before = StockMovement.objects.count()

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_REJECTED,
            resolution=ReturnItem.RESOLUTION_NOT_APPLICABLE, inspected_by=self.staff,
        )

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after, stock_before)
        self.assertEqual(StockMovement.objects.count(), movement_count_before)
        self.assertFalse(StockMovement.objects.filter(source_return_item=item).exists())

    def test_return_completes_after_its_only_item_is_finalized(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        # Fetch fresh rather than via item.return_request — the reverse-then-
        # forward relation traversal inside the helper leaves Django's FK
        # cache pointing at the stale, pre-lifecycle Return instance.
        return_request = Return.objects.get(pk=item.return_request_id)
        self.assertEqual(return_request.status, Return.STATUS_INSPECTION_PENDING)

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_REJECTED,
            resolution=ReturnItem.RESOLUTION_NOT_APPLICABLE, inspected_by=self.staff,
        )
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_COMPLETED)


class ReturnInspectionPartialTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919700000104")
        self.staff = _make_user(mobile="+919700000105", username="inspectstaff2")
        self.variant = _make_variant(size_ml=100)
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_restocked_partial_creates_correct_lot(self):
        before = timezone.now()
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_PARTIAL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            remaining_quantity_ml=Decimal("35.00"),
        )

        lot = PartialBottleLot.objects.get(source_return_item=item)
        self.assertEqual(lot.variant, self.variant)
        self.assertEqual(lot.warehouse, self.warehouse)
        self.assertEqual(lot.remaining_ml, Decimal("35.00"))
        self.assertEqual(lot.reserved_ml, Decimal("0.00"))
        self.assertFalse(lot.is_depleted)
        self.assertGreaterEqual(lot.opened_at, before)  # inspection time, not the original packing time

        movement = StockMovement.objects.get(source_return_item=item, partial_lot=lot)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_RETURN_RESTOCKED_PARTIAL)
        self.assertEqual(movement.quantity_delta, Decimal("35.00"))

    def test_restocked_partial_does_not_restore_full_bottle_to_retail(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        stock_before = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_PARTIAL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            remaining_quantity_ml=Decimal("40.00"),
        )

        stock_after = InventoryStock.objects.get(
            variant=self.variant, warehouse=self.warehouse, stock_type="retail"
        ).quantity
        self.assertEqual(stock_after, stock_before)  # unchanged — no retail pcs restoration


class ReturnInspectionDecantTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919700000106")
        self.staff = _make_user(mobile="+919700000107", username="inspectstaff3")
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.source, self.decant = _make_decant_pair(
            source_size_ml=100, decant_volume_ml=10, source_retail_quantity=10
        )

    def test_damaged_decant_creates_no_inventory_movement(self):
        item = _return_item_ready_for_inspection(
            self.client, self.user, self.staff, self.decant, quantity=1,
            reason=ReturnItem.REASON_MANUFACTURING_DEFECT,
        )
        movement_count_before = StockMovement.objects.count()

        finalized = order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_DAMAGED,
            resolution=ReturnItem.RESOLUTION_NOT_APPLICABLE, inspected_by=self.staff,
        )

        self.assertEqual(finalized.disposition, ReturnItem.DISPOSITION_DAMAGED)
        self.assertEqual(StockMovement.objects.count(), movement_count_before)  # no new movement at all
        self.assertFalse(InventoryStock.objects.filter(variant=self.decant).exists())  # no decant stock pool, ever

    def test_decant_cannot_be_restocked_retail(self):
        item = _return_item_ready_for_inspection(
            self.client, self.user, self.staff, self.decant, quantity=1,
            reason=ReturnItem.REASON_WRONG_VARIANT,
        )
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )
        item.refresh_from_db()
        self.assertIsNone(item.disposition)

    def test_decant_restocked_partial_merges_into_source_variant_pool(self):
        item = _return_item_ready_for_inspection(
            self.client, self.user, self.staff, self.decant, quantity=1,
            reason=ReturnItem.REASON_TRANSIT_DAMAGE,
        )

        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_PARTIAL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            remaining_quantity_ml=Decimal("6.00"),
        )

        lot = PartialBottleLot.objects.get(source_return_item=item)
        self.assertEqual(lot.variant, self.source)  # merged into the SOURCE variant, not the decant
        self.assertEqual(lot.remaining_ml, Decimal("6.00"))
        self.assertFalse(InventoryStock.objects.filter(variant=self.decant).exists())


class ReturnInspectionIntegrityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user(mobile="+919700000108")
        self.staff = _make_user(mobile="+919700000109", username="inspectstaff4")
        self.variant = _make_variant()
        self.warehouse = Warehouse.objects.get(is_default=True)

    def test_received_quantity_exceeding_approved_quantity_rejected(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=2)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=3, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )
        item.refresh_from_db()
        self.assertIsNone(item.disposition)

    def test_negative_received_quantity_rejected(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=-1, disposition=ReturnItem.DISPOSITION_DAMAGED,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )

    def test_zero_received_quantity_allowed_for_rejected(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        finalized = order_services.finalize_return_item(
            item, received_quantity=0, disposition=ReturnItem.DISPOSITION_REJECTED,
            resolution=ReturnItem.RESOLUTION_NOT_APPLICABLE, inspected_by=self.staff,
        )
        self.assertEqual(finalized.disposition, ReturnItem.DISPOSITION_REJECTED)
        self.assertFalse(StockMovement.objects.filter(source_return_item=item).exists())

    def test_zero_received_quantity_rejected_for_restocked_retail(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=0, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )
        item.refresh_from_db()
        self.assertIsNone(item.disposition)
        self.assertFalse(StockMovement.objects.filter(source_return_item=item).exists())

    def test_zero_received_quantity_rejected_for_damaged(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=0, disposition=ReturnItem.DISPOSITION_DAMAGED,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )
        self.assertFalse(StockMovement.objects.filter(source_return_item=item).exists())

    def test_zero_received_quantity_rejected_for_restocked_partial(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=0, disposition=ReturnItem.DISPOSITION_RESTOCKED_PARTIAL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
                remaining_quantity_ml=Decimal("10.00"),
            )
        self.assertFalse(PartialBottleLot.objects.filter(source_return_item=item).exists())

    def test_restocked_partial_requires_positive_remaining_ml(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_PARTIAL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
                remaining_quantity_ml=None,
            )

    def test_remaining_ml_rejected_for_non_partial_disposition(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
                remaining_quantity_ml=Decimal("10.00"),
            )

    def test_cannot_finalize_same_return_item_twice(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)
        order_services.finalize_return_item(
            item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )
        with self.assertRaises(order_services.ReturnInspectionError):
            order_services.finalize_return_item(
                item, received_quantity=1, disposition=ReturnItem.DISPOSITION_DAMAGED,
                resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
            )
        # Only the first disposition's movement exists — never double-counted.
        movements = StockMovement.objects.filter(source_return_item=item)
        self.assertEqual(movements.count(), 1)

    def test_failed_inventory_mutation_rolls_back_finalization(self):
        item = _return_item_ready_for_inspection(self.client, self.user, self.staff, self.variant, quantity=1)

        with patch(
            "apps.orders.services._apply_return_disposition_inventory",
            side_effect=RuntimeError("simulated inventory failure"),
        ):
            with self.assertRaises(RuntimeError):
                order_services.finalize_return_item(
                    item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
                    resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
                )

        item.refresh_from_db()
        self.assertIsNone(item.disposition)  # rolled back completely
        self.assertIsNone(item.inspected_at)
        self.assertFalse(StockMovement.objects.filter(source_return_item=item).exists())

    def test_return_does_not_complete_until_all_items_finalized(self):
        variant_b = _make_variant()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, variant=self.variant, quantity=1)
        CartItem.objects.create(cart=cart, variant=variant_b, quantity=1)
        self.client.credentials(**_auth_header(self.user))
        resp = self.client.post(CHECKOUT_URL, {"shipping_address": _SHIPPING}, format="json")
        order = Order.objects.get(order_number=resp.data["order_number"])
        order_services.verify_cod_order(order, verified_by=self.staff)
        order_services.pack_order(order, performed_by=self.staff)
        order_services.mark_order_shipped(order)
        order_services.mark_order_delivered(order)
        order.refresh_from_db()
        items = list(order.items.all())

        return_request = order_services.create_return(order, items=[
            {"order_item": items[0], "reason": ReturnItem.REASON_WRONG_VARIANT, "requested_quantity": 1},
            {"order_item": items[1], "reason": ReturnItem.REASON_TRANSIT_DAMAGE, "requested_quantity": 1},
        ])
        order_services.approve_return(return_request)
        order_services.mark_return_in_transit(return_request)
        for ri in return_request.items.all():
            ri.received_quantity = 1
            ri.save(update_fields=["received_quantity"])
        order_services.mark_return_received(return_request)
        order_services.start_inspection(return_request)

        return_items = list(return_request.items.all())
        order_services.finalize_return_item(
            return_items[0], received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_INSPECTION_PENDING)  # one item still pending

        order_services.finalize_return_item(
            return_items[1], received_quantity=1, disposition=ReturnItem.DISPOSITION_DAMAGED,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )
        return_request.refresh_from_db()
        self.assertEqual(return_request.status, Return.STATUS_COMPLETED)  # now both are done


class ReturnInspectionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        Warehouse.objects.filter(is_default=True).delete()
        self.warehouse = Warehouse.objects.create(name="Smerfume Default", is_default=True)
        self.client = APIClient()
        self.user = User.objects.create(
            username="inspectconcuser", mobile_number="+919700000110", role="customer",
        )
        self.user.set_unusable_password()
        self.user.save()
        self.staff = User.objects.create(
            username="inspectconcstaff", mobile_number="+919700000111", role="staff",
        )
        self.client.credentials(**_auth_header(self.user))

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
        self.variant = ProductVariant.objects.create(
            edition=edition, size_ml=10, selling_price="500.00", mrp="600.00",
            sku=f"TEST-{uuid.uuid4().hex[:10]}",
        )
        InventoryStock.objects.create(
            variant=self.variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("50"),
        )

        self.return_item = _return_item_ready_for_inspection(
            self.client, self.user, self.staff, self.variant, quantity=1,
        )

    def test_concurrent_finalization_cannot_create_duplicate_inventory(self):
        results = {}
        barrier = threading.Barrier(2)

        def _run(key, disposition):
            barrier.wait()
            try:
                order_services.finalize_return_item(
                    self.return_item, received_quantity=1, disposition=disposition,
                    resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
                )
                results[key] = "ok"
            except order_services.ReturnInspectionError:
                results[key] = "rejected"
            finally:
                connection.close()

        t1 = threading.Thread(target=_run, args=("a", ReturnItem.DISPOSITION_RESTOCKED_RETAIL))
        t2 = threading.Thread(target=_run, args=("b", ReturnItem.DISPOSITION_DAMAGED))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(sorted(results.values()), ["ok", "rejected"])
        movements = StockMovement.objects.filter(source_return_item=self.return_item)
        self.assertEqual(movements.count(), 1)  # exactly one, regardless of which disposition won


class ReturnItemAdminInspectionTests(TestCase):
    def setUp(self):
        User.objects.create_superuser(
            username="returnitemadminstaff", email="returnitemadminstaff@example.com", password="testpass123",
        )
        self.client_api = APIClient()
        self.user = _make_user(mobile="+919700000112")
        self.staff = _make_user(mobile="+919700000113", username="inspectstaff5")
        self.variant = _make_variant()
        self.warehouse = Warehouse.objects.get(is_default=True)
        self.return_item = _return_item_ready_for_inspection(
            self.client_api, self.user, self.staff, self.variant, quantity=1,
        )

    def test_inspecting_through_admin_change_form_creates_real_inventory_movement(self):
        self.client.login(username="returnitemadminstaff@example.com", password="testpass123")
        url = f"/admin/orders/returnitem/{self.return_item.pk}/change/"

        self.client.post(url, data={
            "received_quantity": 1,
            "disposition": ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            "resolution": ReturnItem.RESOLUTION_REFUND,
            "remaining_quantity_ml": "",
            "inspection_notes": "checked via admin",
            "_save": "Save",
        })

        self.return_item.refresh_from_db()
        self.assertEqual(self.return_item.disposition, ReturnItem.DISPOSITION_RESTOCKED_RETAIL)
        self.assertIsNotNone(self.return_item.inspected_at)

        movement = StockMovement.objects.get(source_return_item=self.return_item)
        self.assertEqual(movement.movement_type, StockMovement.MOVEMENT_RETURN_RESTOCKED_RETAIL)

    def test_inspection_fields_become_readonly_after_finalization(self):
        order_services.finalize_return_item(
            self.return_item, received_quantity=1, disposition=ReturnItem.DISPOSITION_RESTOCKED_RETAIL,
            resolution=ReturnItem.RESOLUTION_REFUND, inspected_by=self.staff,
        )
        self.client.login(username="returnitemadminstaff@example.com", password="testpass123")
        url = f"/admin/orders/returnitem/{self.return_item.pk}/change/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="disposition"')  # readonly now, not a select widget


# ── Phase 5.3: refund calculation and financial records ───────────────────────

def _finalize_one_item_return(
    client, user, staff, variant, quantity, reason, disposition, resolution,
    received_quantity=None, remaining_quantity_ml=None,
):
    """Drive one order+return all the way through finalize_return_item()
    and return the resulting ReturnItem. Fetch the parent Return fresh via
    Return.objects.get(pk=item.return_request_id) afterward — item.return_request
    can carry Django's stale FK cache from the reverse-relation traversal
    inside _return_item_ready_for_inspection (see the note further up)."""
    item = _return_item_ready_for_inspection(client, user, staff, variant, quantity=quantity, reason=reason)
    if received_quantity is None:
        received_quantity = quantity
    return order_services.finalize_return_item(
        item, received_quantity=received_quantity, disposition=disposition, resolution=resolution,
        inspected_by=staff, remaining_quantity_ml=remaining_quantity_ml,
    )
