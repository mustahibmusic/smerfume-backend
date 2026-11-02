from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.catalog.serializers import ProductVariantSerializer

from .models import Cart, CartItem


def purchasable_variants():
    """Variants a customer can add: the variant, its edition and its product
    are all active (ProductVariant.is_purchasable, as a filter)."""
    return ProductVariant.objects.filter(
        is_active=True, edition__is_active=True, edition__product__is_active=True,
    )


class CartItemSerializer(serializers.ModelSerializer):
    """Read-only cart line. public_id identifies the line in the cart item
    URLs; the internal primary key is never exposed."""

    variant = ProductVariantSerializer(read_only=True)
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = CartItem
        fields = ("public_id", "variant", "quantity", "line_total")


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    cart_token = serializers.CharField(source="session_key", read_only=True, allow_null=True)

    class Meta:
        model = Cart
        fields = ("public_id", "cart_token", "items", "total", "item_count")

    @extend_schema_field(serializers.DecimalField(max_digits=10, decimal_places=2))
    def get_total(self, obj):
        return obj.get_total()

    @extend_schema_field(serializers.IntegerField())
    def get_item_count(self, obj):
        return obj.items.count()


class AddToCartSerializer(serializers.Serializer):
    # The variant's public_id (UUID) from the catalogue API, never the
    # internal primary key.
    variant_id = serializers.SlugRelatedField(
        slug_field="public_id",
        queryset=purchasable_variants(),
        help_text="public_id of the product variant (from the catalogue API).",
        error_messages={"does_not_exist": "This product variant is not available."},
    )
    quantity = serializers.IntegerField(min_value=1, max_value=99, default=1)


class UpdateCartItemSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=99)
