import datetime

from django.db import transaction
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cart.models import Cart

from .models import Order, OrderItem, ShippingAddress
from .serializers import CheckoutSerializer, OrderSerializer


def _generate_order_number():
    today = datetime.date.today().strftime("%Y%m%d")
    # Use a tight random suffix so numbers stay short and readable
    import secrets
    suffix = secrets.token_hex(3).upper()
    return f"SMR-{today}-{suffix}"


class CheckoutView(APIView):
    """
    POST /api/orders/checkout/

    Converts the authenticated user's cart into a confirmed order.
    Body: { "shipping_address": {...}, "customer_notes": "..." }
    """

    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cart = Cart.objects.prefetch_related("items__variant").get(user=request.user)
        except Cart.DoesNotExist:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        cart_items = list(cart.items.select_related("variant").all())
        if not cart_items:
            return Response({"error": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        subtotal = sum(item.line_total for item in cart_items)
        total = subtotal  # discount logic can be added later via offers app

        order = Order.objects.create(
            user=request.user,
            order_number=_generate_order_number(),
            subtotal=subtotal,
            total=total,
            customer_notes=serializer.validated_data.get("customer_notes", ""),
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


class OrderListView(generics.ListAPIView):
    """GET /api/orders/ — returns the authenticated user's order history."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            .prefetch_related("items__variant", "shipping_address")
            .order_by("-created_at")
        )


class OrderDetailView(generics.RetrieveAPIView):
    """GET /api/orders/{order_number}/"""

    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer
    lookup_field = "order_number"

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related(
            "items__variant", "shipping_address"
        )
