import re
from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.catalog.serializers import ProductVariantSerializer
from apps.inventory.models import Warehouse

from .models import Order, OrderItem, Return, ReturnItem, ShippingAddress


def validate_mobile_number(value):
    """Shared mobile-number rule for checkout and in-store sales."""
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9]{10,15}", value):
        raise serializers.ValidationError("Enter a valid mobile number.")
    return value


class ShippingAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShippingAddress
        fields = (
            "full_name",
            "mobile",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "pincode",
            "country",
        )

    def validate_pincode(self, value):
        if not value.isdigit() or len(value) != 6:
            raise serializers.ValidationError("Enter a valid 6-digit pincode.")
        return value

    def validate_mobile(self, value):
        return validate_mobile_number(value)


class OrderItemSerializer(serializers.ModelSerializer):
    variant = ProductVariantSerializer(read_only=True)

    class Meta:
        model = OrderItem
        # public_id is required so a customer can reference a specific line
        # (e.g. when requesting a return) without exposing the integer pk.
        fields = ("public_id", "variant", "quantity", "unit_price", "line_total")


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    shipping_address = ShippingAddressSerializer(read_only=True)
    is_guest_order = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = (
            "public_id",
            "order_number",
            "status",
            "channel",
            "payment_method",
            "subtotal",
            "discount_amount",
            "total",
            "customer_notes",
            "is_guest_order",
            "items",
            "shipping_address",
            "created_at",
        )


class CheckoutSerializer(serializers.Serializer):
    shipping_address = ShippingAddressSerializer()
    customer_notes = serializers.CharField(allow_blank=True, required=False, default="")
    guest_email = serializers.EmailField(required=False, allow_null=True, default=None)


class InStoreSaleItemSerializer(serializers.Serializer):
    sku = serializers.SlugRelatedField(
        slug_field="sku",
        queryset=ProductVariant.objects.select_related("edition__product__brand"),
        error_messages={"does_not_exist": "Unknown SKU '{value}'."},
    )
    quantity = serializers.IntegerField(min_value=1, max_value=1000)


class InStoreSaleSerializer(serializers.Serializer):
    """Staff input for a walk-in counter sale (DEC-008). Only these fields
    are accepted; prices, totals and status are always computed server-side."""

    customer_mobile = serializers.CharField(max_length=15)
    customer_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    items = InStoreSaleItemSerializer(many=True)
    payment_method = serializers.ChoiceField(
        choices=[c for c in Order.PAYMENT_METHOD_CHOICES if c[0] in Order.IN_STORE_PAYMENT_METHODS]
    )
    payment_reference = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.00"), required=False,
        default=Decimal("0.00"),
    )
    warehouse = serializers.SlugRelatedField(
        slug_field="public_id",
        queryset=Warehouse.objects.all(),
        required=False,
        allow_null=True,
        default=None,
        help_text="Public ID of the stock location. Defaults to the default warehouse.",
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_customer_mobile(self, value):
        return validate_mobile_number(value)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value


# ── Returns (read) ──────────────────────────────────────────────────────────

class ReturnItemDetailSerializer(serializers.ModelSerializer):
    order_item = OrderItemSerializer(read_only=True)

    class Meta:
        model = ReturnItem
        fields = (
            "public_id", "order_item", "reason", "reason_notes",
            "requested_quantity", "received_quantity",
        )
        read_only_fields = fields


class ReturnDetailSerializer(serializers.ModelSerializer):
    """Read-only projection of a Return. status and every ReturnItem field
    are always read-only here — nothing in this API surface ever accepts
    them as writable input; lifecycle/disposition changes only ever happen
    through apps.orders.services, never through this serializer."""

    order_number = serializers.CharField(source="order.order_number", read_only=True)
    items = ReturnItemDetailSerializer(many=True, read_only=True)

    class Meta:
        model = Return
        fields = ("public_id", "order_number", "status", "items", "created_at")
        read_only_fields = fields


# ── Returns (write) ─────────────────────────────────────────────────────────

class CreateReturnItemSerializer(serializers.Serializer):
    """Accepts only customer-controlled fields. order_item is referenced by
    public_id (never the integer pk) and must belong to the order supplied
    via serializer context — enforced here so a customer can't reference a
    line from a different order, even their own."""

    order_item = serializers.SlugRelatedField(slug_field="public_id", queryset=OrderItem.objects.all())
    reason = serializers.ChoiceField(choices=ReturnItem.REASON_CHOICES)
    requested_quantity = serializers.IntegerField(min_value=1)
    reason_notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_order_item(self, value):
        order = self.context["order"]
        if value.order_id != order.pk:
            raise serializers.ValidationError("This item does not belong to the specified order.")
        return value


class CreateReturnSerializer(serializers.Serializer):
    items = CreateReturnItemSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("At least one item is required.")
        return value
