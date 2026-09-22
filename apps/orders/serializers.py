from rest_framework import serializers

from apps.catalog.serializers import ProductVariantSerializer

from .models import Order, OrderItem, Return, ReturnItem, ShippingAddress


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
        import re
        if not re.fullmatch(r"\+?[0-9]{10,15}", value.strip()):
            raise serializers.ValidationError("Enter a valid mobile number.")
        return value.strip()


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
