"""
Order views.

CheckoutView supports both authenticated users (JWT) and guests
(X-Cart-Token header + guest_email in the request body).

Guest checkout flow:
1. Guest provides shipping address, guest_email, and X-Cart-Token header.
2. A User is resolved (or created) from the shipping address mobile number.
3. The Order is created linked to that User.
4. The guest cart is cleared.
5. The guest can later log in with their mobile number to see their order.
"""

import datetime
import logging
import secrets
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cart.models import Cart
from apps.inventory.models import Warehouse
from apps.inventory.services import reservation as reservation_service
from apps.inventory.services.reservation import InsufficientStockError

from . import services as order_services
from .models import Order, OrderItem, Return, ShippingAddress
from .serializers import (
    CheckoutSerializer,
    CreateReturnSerializer,
    OrderSerializer,
    ReturnDetailSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()

_CART_TOKEN_HEADER = OpenApiParameter(
    name="X-Cart-Token",
    type=str,
    location=OpenApiParameter.HEADER,
    required=False,
    description="Guest cart token. Required for unauthenticated checkout.",
)

_RETURN_PUBLIC_ID_PATH = OpenApiParameter(
    "public_id",
    OpenApiTypes.UUID,
    OpenApiParameter.PATH,
    description="Public UUID of the return request.",
)


class EmptyCartError(Exception):
    """Raised when a locked cart has no items — either it started empty,
    or a concurrent request already checked it out and cleared it."""


def _generate_order_number():
    """Generate a unique order number in the format SMR-YYYYMMDD-XXXXXX."""
    today = datetime.date.today().strftime("%Y%m%d")
    suffix = secrets.token_hex(3).upper()
    return f"SMR-{today}-{suffix}"


def _resolve_guest_user(mobile: str, guest_email: str):
    """Find or silently create a User for a guest checkout.

    Looks up by mobile number. If an existing account is found, email is set
    on it only when it has none and the provided email is not already taken.
    If a new account is created, it is set up with an unusable password so
    the guest can log in via OTP at any time.

    Args:
        mobile: Mobile number string from the shipping address.
        guest_email: Email address provided by the guest at checkout.

    Returns:
        User instance.
    """
    mobile_normalized = mobile.strip()

    user, created = User.objects.get_or_create(
        mobile_number=mobile_normalized,
        defaults={
            "username": f"user_{mobile_normalized[-4:]}_{secrets.token_hex(3)}",
            "role": "customer",
            "is_staff": False,
        },
    )

    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        logger.info(
            "Guest checkout: created new user (pk=%s) for mobile %s",
            user.pk,
            mobile_normalized,
        )

    # Set email only if the user has none and the address is not already taken
    if guest_email and not user.email:
        email_taken = User.objects.filter(email=guest_email).exclude(pk=user.pk).exists()
        if not email_taken:
            user.email = guest_email
            user.save(update_fields=["email"])

    return user


class CheckoutView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Orders"],
        summary="Checkout",
        description=(
            "Convert a cart into a confirmed order.\n\n"
            "**Authenticated checkout** (JWT required):\n"
            "- Cart is resolved from the authenticated user.\n"
            "- `guest_email` is ignored.\n\n"
            "**Guest checkout** (no JWT):\n"
            "- `X-Cart-Token` header is required.\n"
            "- `guest_email` is required in the request body.\n"
            "- A user account is silently created from the shipping address "
            "mobile number if one does not already exist.\n"
            "- The guest can log in via OTP with their mobile number to view "
            "their order history.\n\n"
            "Cart items are cleared after the order is created."
        ),
        parameters=[_CART_TOKEN_HEADER],
        request=CheckoutSerializer,
        responses={
            201: OrderSerializer,
            400: OpenApiResponse(
                description=(
                    "Cart is empty, request data is invalid, or "
                    "guest_email / X-Cart-Token missing for guest checkout."
                ),
                examples=[
                    OpenApiExample("Empty cart", value={"error": "Your cart is empty."}),
                    OpenApiExample(
                        "Missing guest_email",
                        value={"error": "guest_email is required for guest checkout."},
                    ),
                ],
            ),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
        examples=[
            OpenApiExample(
                "Authenticated checkout",
                request_only=True,
                value={
                    "shipping_address": {
                        "full_name": "Rahul Sharma",
                        "mobile": "9876543210",
                        "address_line1": "42, MG Road",
                        "address_line2": "Near Central Mall",
                        "city": "Bengaluru",
                        "state": "KA",
                        "pincode": "560001",
                        "country": "India",
                    },
                    "customer_notes": "Please pack as a gift.",
                },
            ),
            OpenApiExample(
                "Guest checkout",
                request_only=True,
                value={
                    "guest_email": "rahul@example.com",
                    "shipping_address": {
                        "full_name": "Rahul Sharma",
                        "mobile": "9876543210",
                        "address_line1": "42, MG Road",
                        "city": "Bengaluru",
                        "state": "KA",
                        "pincode": "560001",
                    },
                    "customer_notes": "",
                },
            ),
        ],
    )
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if request.user.is_authenticated:
            cart, user, guest_email = self._resolve_authenticated(request)
        else:
            result = self._resolve_guest(request, serializer)
            if isinstance(result, Response):
                return result
            cart, user, guest_email = result

        if cart is None:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                locked_cart = Cart.objects.select_for_update().get(pk=cart.pk)
                cart_items = list(locked_cart.items.select_related("variant").all())
                if not cart_items:
                    raise EmptyCartError()
                order = self._create_order_with_reservations(
                    locked_cart, cart_items, user, guest_email, serializer
                )
        except InsufficientStockError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except EmptyCartError:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    def _create_order_with_reservations(self, cart, cart_items, user, guest_email, serializer):
        """Everything that must succeed or fail together: Order/OrderItem
        creation, inventory reservation per line, the shipping address, and
        clearing the cart. Raises InsufficientStockError (caller maps it to
        a 400) if any line can't be reserved — the whole transaction rolls
        back, nothing partially created.
        """
        warehouse = Warehouse.objects.get(is_default=True)

        subtotal = sum((item.line_total for item in cart_items), Decimal("0.00"))
        # No discount engine is wired in yet (apps.offers is still an empty
        # stub) — this is always 0 today. The allocation machinery below is
        # ready for when a coupon/offer sets a real Order-level discount.
        discount_amount = Decimal("0.00")
        discount_allocations = order_services.allocate_discount(cart_items, discount_amount)

        item_financials = {
            item.id: order_services.build_order_item_financials(item, discount_allocations[item.id])
            for item in cart_items
        }

        # base_shipping_charge is intentionally 0 and free_shipping_applied
        # False — there is no free-shipping-threshold/base-charge rules
        # engine defined anywhere in the project yet. Flagged as an open
        # business decision rather than assumed. Per-item shipping_surcharge
        # IS applied, since it's a direct snapshot of an already-configured
        # ProductVariant.shipping_surcharge value.
        base_shipping_charge = Decimal("0.00")
        shipping_charge = base_shipping_charge + sum(
            (f["shipping_surcharge"] for f in item_financials.values()), Decimal("0.00")
        )

        total = subtotal - discount_amount + shipping_charge

        order = Order.objects.create(
            user=user,
            order_number=_generate_order_number(),
            subtotal=subtotal,
            discount_amount=discount_amount,
            total=total,
            base_shipping_charge=base_shipping_charge,
            shipping_charge=shipping_charge,
            customer_notes=serializer.validated_data.get("customer_notes", ""),
            guest_email=guest_email,
        )

        for cart_item in cart_items:
            order_item = OrderItem.objects.create(
                order=order,
                variant=cart_item.variant,
                quantity=cart_item.quantity,
                unit_price=cart_item.variant.selling_price,
                **item_financials[cart_item.id],
            )
            reservation_service.reserve_for_order_item(order_item, warehouse)

        addr_data = serializer.validated_data["shipping_address"]
        ShippingAddress.objects.create(order=order, **addr_data)

        cart.items.all().delete()

        return order

    def _resolve_authenticated(self, request):
        """Return (cart, user, guest_email=None) for an authenticated request."""
        try:
            cart = Cart.objects.prefetch_related("items__variant").get(user=request.user)
        except Cart.DoesNotExist:
            cart = None
        return cart, request.user, None

    def _resolve_guest(self, request, serializer):
        """Return (cart, user, guest_email) for a guest request, or a Response on error."""
        guest_email = serializer.validated_data.get("guest_email")
        if not guest_email:
            return Response(
                {"error": "guest_email is required for guest checkout."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cart_token = request.headers.get("X-Cart-Token")
        if not cart_token:
            return Response(
                {"error": "X-Cart-Token header is required for guest checkout."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cart = Cart.objects.prefetch_related("items__variant").get(
                session_key=cart_token, user__isnull=True
            )
        except Cart.DoesNotExist:
            return Response(
                {"error": "Your cart is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mobile = serializer.validated_data["shipping_address"]["mobile"]
        user = _resolve_guest_user(mobile, guest_email)

        return cart, user, guest_email


class OrderListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer

    @extend_schema(
        tags=["Orders"],
        summary="List my orders",
        description="Return the authenticated user's full order history, newest first.",
        responses={
            200: OrderSerializer(many=True),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .prefetch_related("items__variant", "shipping_address")
            .order_by("-created_at")
        )


class OrderDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer
    lookup_field = "order_number"

    @extend_schema(
        tags=["Orders"],
        summary="Get order detail",
        description="Return full details of a single order by its order number.",
        parameters=[
            OpenApiParameter(
                "order_number",
                str,
                OpenApiParameter.PATH,
                description="Order number e.g. SMR-20260819-A3F2B1",
            )
        ],
        responses={
            200: OrderSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Order not found or does not belong to this user."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related(
            "items__variant", "shipping_address"
        )


# ── Returns ─────────────────────────────────────────────────────────────────
#
# Thin by design: every view here does authentication + ownership + input
# validation, then delegates entirely to apps.orders.services (create_return,
# cancel_return). No eligibility, quantity, date-window, or concurrency logic
# is duplicated here — that all remains authoritative in the service layer,
# unchanged from Phase 5.1. Return.status and every ReturnItem field are
# read-only everywhere in this surface; there is no endpoint that accepts
# status, disposition, or any lifecycle field as customer input.

class CreateReturnView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Orders"],
        summary="Request a return",
        description=(
            "Request a return for one or more lines of a delivered order. "
            "Eligibility (delivery status, 1-day window, valid reason, "
            "remaining returnable quantity) is enforced by the service layer, "
            "not this endpoint — see create_return()."
        ),
        parameters=[
            OpenApiParameter(
                "order_number", str, OpenApiParameter.PATH,
                description="Order number e.g. SMR-20260819-A3F2B1",
            )
        ],
        request=CreateReturnSerializer,
        responses={
            201: ReturnDetailSerializer,
            400: OpenApiResponse(description="Ineligible order/item, invalid reason, or quantity exceeds what's returnable."),
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Order not found or does not belong to this user."),
        },
    )
    def post(self, request, order_number):
        try:
            order = Order.objects.get(order_number=order_number, user=request.user)
        except Order.DoesNotExist:
            return Response({"error": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = CreateReturnSerializer(data=request.data, context={"order": order})
        serializer.is_valid(raise_exception=True)

        items = [
            {
                "order_item": entry["order_item"],
                "reason": entry["reason"],
                "requested_quantity": entry["requested_quantity"],
                "reason_notes": entry.get("reason_notes", ""),
            }
            for entry in serializer.validated_data["items"]
        ]

        try:
            return_request = order_services.create_return(order, items=items)
        except order_services.ReturnEligibilityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ReturnDetailSerializer(return_request).data, status=status.HTTP_201_CREATED)


class ReturnListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReturnDetailSerializer

    @extend_schema(
        tags=["Orders"],
        summary="List my returns",
        description="Return the authenticated user's return requests across all orders, newest first.",
        responses={
            200: ReturnDetailSerializer(many=True),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            Return.objects.filter(order__user=self.request.user)
            .select_related("order")
            .prefetch_related("items__order_item__variant")
            .order_by("-created_at")
        )


class ReturnDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReturnDetailSerializer
    lookup_field = "public_id"

    @extend_schema(
        tags=["Orders"],
        summary="Get return detail",
        description="Return full details of a single return request.",
        parameters=[_RETURN_PUBLIC_ID_PATH],
        responses={
            200: ReturnDetailSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Return not found or does not belong to this user."),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        return Return.objects.filter(order__user=self.request.user).prefetch_related(
            "items__order_item__variant"
        )


class CancelReturnView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Orders"],
        summary="Cancel a return request",
        description=(
            "Cancel a return the customer no longer wants to proceed with. "
            "Only allowed before the return has been received — see "
            "cancel_return() for the exact rule, enforced entirely by the "
            "service layer."
        ),
        parameters=[_RETURN_PUBLIC_ID_PATH],
        request=None,
        responses={
            200: ReturnDetailSerializer,
            400: OpenApiResponse(description="Return is no longer in a cancellable state."),
            401: OpenApiResponse(description="Access token missing or expired."),
            404: OpenApiResponse(description="Return not found or does not belong to this user."),
        },
    )
    def post(self, request, public_id):
        try:
            return_request = Return.objects.get(public_id=public_id, order__user=request.user)
        except Return.DoesNotExist:
            return Response({"error": "Return not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            return_request = order_services.cancel_return(return_request)
        except order_services.ReturnTransitionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ReturnDetailSerializer(return_request).data, status=status.HTTP_200_OK)
