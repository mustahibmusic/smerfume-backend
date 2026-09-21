from apps.inventory.models import InventoryStock
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    Brand,
    Category,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    ProductVariantImage,
)


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


class ProductVariantImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariantImage
        fields = ("public_id", "image", "role", "alt_text", "sort_order")


class ProductVariantSerializer(serializers.ModelSerializer):
    """images/inventory_stocks are expected to be prefetched by the caller's
    queryset (see ProductViewSet.get_queryset) — .all() here reads from that
    cache, no extra query per variant."""

    images = ProductVariantImageSerializer(many=True, read_only=True)
    primary_image = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = (
            "public_id", "sku", "image", "size_ml", "is_decant", "mrp", "selling_price",
            "images", "primary_image", "is_available",
        )

    @extend_schema_field(ProductVariantImageSerializer(allow_null=True))
    def get_primary_image(self, obj):
        for img in obj.images.all():
            if img.role == ProductVariantImage.ROLE_PRIMARY:
                return ProductVariantImageSerializer(img, context=self.context).data
        return None

    @extend_schema_field(serializers.BooleanField())
    def get_is_available(self, obj):
        # Purchasable stock is retail-type InventoryStock only — matches
        # apps.inventory.services.reservation, which reserves against retail
        # stock (decants open retail bottles on demand). This is a simple
        # "is there sellable stock right now" signal for catalogue/SEM
        # display, not a simulation of full decant-fulfillment logic — the
        # authoritative check still happens at reservation time in checkout.
        return any(
            (s.quantity - s.quantity_reserved) > 0
            for s in obj.inventory_stocks.all()
            if s.stock_type == InventoryStock.STOCK_TYPE_RETAIL
        )


class EditionNotesGroupedSerializer(serializers.Serializer):
    """Returns notes bucketed into top / heart / base, sorted top → heart → base."""

    _POSITION_ORDER = {"top": 0, "heart": 1, "base": 2}

    def to_representation(self, edition):
        # edition.edition_notes.all() reads from the prefetch cache populated by
        # Prefetch("editions__edition_notes", queryset=EditionNote.objects.select_related("note"))
        # — no extra DB query. Sorting is done in Python.
        edition_notes = sorted(
            edition.edition_notes.all(),
            key=lambda en: (self._POSITION_ORDER.get(en.position, 99), en.note.name),
        )
        grouped = {"top": [], "heart": [], "base": []}
        for en in edition_notes:
            grouped[en.position].append(en.note.name)
        return grouped


# ── Product Edition ────────────────────────────────────────────────────────────

class ProductEditionListSerializer(serializers.ModelSerializer):
    """Edition info used inside the product list endpoint."""

    variants = ProductVariantSerializer(many=True, read_only=True)

    class Meta:
        model = ProductEdition
        fields = (
            "public_id",
            "name",
            "slug",
            "image",
            "gender",
            "concentration",
            "is_best_seller",
            "is_new_arrival",
            "variants",
        )


class EditionSEOSerializer(serializers.Serializer):
    """Resolved SEO metadata for this edition's canonical product page.
    title/og_title fall back to the edition's display name; meta_description/
    og_description have no safe fallback (no generated-copy engine exists)
    and are simply null when unset — see the Catalogue SEO Architecture doc."""

    def to_representation(self, edition):
        return {
            "title": edition.seo_title or edition.display_name(),
            "meta_description": edition.meta_description or None,
            "og_title": edition.og_title or edition.seo_title or edition.display_name(),
            "og_description": edition.og_description or edition.meta_description or None,
            "is_indexable": edition.is_indexable,
        }


class ProductEditionDetailSerializer(serializers.ModelSerializer):
    """Full edition with notes and variants used in product detail."""

    notes = serializers.SerializerMethodField()
    variants = ProductVariantSerializer(many=True, read_only=True)
    seo = EditionSEOSerializer(source="*")

    class Meta:
        model = ProductEdition
        fields = (
            "public_id",
            "name",
            "slug",
            "image",
            "gender",
            "concentration",
            "is_best_seller",
            "is_new_arrival",
            "notes",
            "variants",
            "seo",
        )

    @extend_schema_field(
        serializers.DictField(
            child=serializers.ListField(child=serializers.CharField()),
            help_text='Notes grouped by position: {"top": [...], "heart": [...], "base": [...]}',
        )
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
