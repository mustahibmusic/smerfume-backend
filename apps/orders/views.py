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

from django.contrib.auth import get_user_model
from django.db import transaction
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

from .models import Order, OrderItem, ShippingAddress
from .serializers import CheckoutSerializer, OrderSerializer

logger = logging.getLogger(__name__)
User = get_user_model()

_CART_TOKEN_HEADER = OpenApiParameter(
    name="X-Cart-Token",
    type=str,
    location=OpenApiParameter.HEADER,
    required=False,
    description="Guest cart token. Required for unauthenticated checkout.",
)


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
    @transaction.atomic
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

        cart_items = list(cart.items.select_related("variant").all())
        if not cart_items:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        subtotal = sum(item.line_total for item in cart_items)
        total = subtotal  # discount logic added later via offers app

        order = Order.objects.create(
            user=user,
            order_number=_generate_order_number(),
            subtotal=subtotal,
            total=total,
            customer_notes=serializer.validated_data.get("customer_notes", ""),
            guest_email=guest_email,
        )

        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                variant=item.variant,
                quantity=item.quantity,
                unit_price=item.variant.selling_price,
                line_total=item.line_total,
            )
            for item in cart_items
        ])

        addr_data = serializer.validated_data["shipping_address"]
        ShippingAddress.objects.create(order=order, **addr_data)

        cart.items.all().delete()

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

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
