import datetime
import secrets

from django.db import transaction
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cart.models import Cart

from .models import Order, OrderItem, ShippingAddress
from .serializers import CheckoutSerializer, OrderSerializer


def _generate_order_number():
    today = datetime.date.today().strftime("%Y%m%d")
    suffix = secrets.token_hex(3).upper()
    return f"SMR-{today}-{suffix}"


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Orders"],
        summary="Checkout",
        description=(
            "Convert the authenticated user's cart into a confirmed order.\n\n"
            "- The cart must be non-empty.\n"
            "- A shipping address is required.\n"
            "- Cart items are cleared after the order is created.\n"
            "- Returns the full order object including the generated order number."
        ),
        request=CheckoutSerializer,
        responses={
            201: OrderSerializer,
            400: OpenApiResponse(
                description="Cart is empty or request data is invalid.",
                examples=[OpenApiExample("Empty cart", value={"error": "Your cart is empty."})],
            ),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
        examples=[
            OpenApiExample(
                "Checkout request",
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
            )
        ],
    )
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
        total = subtotal  # discount logic added later via offers app

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
