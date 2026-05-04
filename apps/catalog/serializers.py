from rest_framework import serializers

from .models import Brand, Category, EditionNote, PerfumeNote, Product, ProductEdition, ProductVariant


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ("public_id", "name", "slug", "brand_category")


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("public_id", "name", "slug")


class PerfumeNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = PerfumeNote
        fields = ("public_id", "name", "notes_category")


class ProductVariantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariant
        fields = ("public_id", "size_ml", "is_decant", "mrp", "selling_price")


class EditionNotesGroupedSerializer(serializers.Serializer):
    """Returns notes bucketed into top / heart / base."""

    def to_representation(self, edition):
        qs = edition.ordered_notes().select_related("note")
        grouped = {"top": [], "heart": [], "base": []}
        for en in qs:
            grouped[en.position].append(en.note.name)
        return grouped


# ── Product Edition ────────────────────────────────────────────────────────────

class ProductEditionListSerializer(serializers.ModelSerializer):
    """Lightweight edition info used inside the product list endpoint."""

    min_price = serializers.SerializerMethodField()
    max_price = serializers.SerializerMethodField()
    variants_count = serializers.SerializerMethodField()

    class Meta:
        model = ProductEdition
        fields = (
            "public_id",
            "name",
            "slug",
            "gender",
            "concentration",
            "is_best_seller",
            "is_new_arrival",
            "min_price",
            "max_price",
            "variants_count",
        )

    def get_min_price(self, obj):
        prices = [v.selling_price for v in obj.variants.all() if obj.is_active]
        return min(prices) if prices else None

    def get_max_price(self, obj):
        prices = [v.selling_price for v in obj.variants.all() if obj.is_active]
        return max(prices) if prices else None

    def get_variants_count(self, obj):
        return obj.variants.count()


class ProductEditionDetailSerializer(serializers.ModelSerializer):
    """Full edition with notes and variants used in product detail."""

    notes = serializers.SerializerMethodField()
    variants = ProductVariantSerializer(many=True, read_only=True)

    class Meta:
        model = ProductEdition
        fields = (
            "public_id",
            "name",
            "slug",
            "gender",
            "concentration",
            "is_best_seller",
            "is_new_arrival",
            "notes",
            "variants",
        )

    def get_notes(self, obj):
        return EditionNotesGroupedSerializer().to_representation(obj)


# ── Product ────────────────────────────────────────────────────────────────────

class ProductListSerializer(serializers.ModelSerializer):
    """Used in the product listing / search results page."""

    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    editions = ProductEditionListSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = ("public_id", "name", "slug", "brand", "category", "editions")


class ProductDetailSerializer(serializers.ModelSerializer):
    """Full product page payload."""

    brand = BrandSerializer(read_only=True)
    category = CategorySerializer(read_only=True)
    editions = ProductEditionDetailSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = ("public_id", "name", "slug", "brand", "category", "editions")
