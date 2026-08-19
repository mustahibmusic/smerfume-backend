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
        fields = ("public_id", "image", "size_ml", "is_decant", "selling_price")


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
            "image",
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
