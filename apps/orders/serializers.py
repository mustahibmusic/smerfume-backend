from rest_framework import serializers

from apps.catalog.serializers import ProductVariantSerializer

from .models import Order, OrderItem, ShippingAddress


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
        fields = ("variant", "quantity", "unit_price", "line_total")


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    shipping_address = ShippingAddressSerializer(read_only=True)

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
            "items",
            "shipping_address",
            "created_at",
        )


class CheckoutSerializer(serializers.Serializer):
    shipping_address = ShippingAddressSerializer()
    customer_notes = serializers.CharField(allow_blank=True, required=False, default="")
