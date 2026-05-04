from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.paginator import InfinitePaginator
from django.db.models import Case, When, Value, IntegerField
from django.utils.safestring import mark_safe


from .models import (
    Brand,
    Category,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    EditionNote,
)


# -------------------------
# Brand
# -------------------------
@admin.register(Brand)
class BrandAdmin(ModelAdmin):
    list_display = ("name", "brand_category")
    list_filter = (
        ("brand_category", ChoicesDropdownFilter),
    )
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}
    paginator = InfinitePaginator
    show_full_result_count = False
    ordering = ("name",)


# -------------------------
# Category
# -------------------------
@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


# -------------------------
# Perfume Note
# -------------------------
@admin.register(PerfumeNote)
class PerfumeNoteAdmin(ModelAdmin):
    list_display = ("name", "notes_category")
    list_filter = (
        ("notes_category", ChoicesDropdownFilter),
    )
    search_fields = ("name",)


# -------------------------
# Edition Note Inline
# -------------------------
class EditionNoteInline(TabularInline):
    model = EditionNote
    extra = 1
    autocomplete_fields = ("note",)


# -------------------------
# Product Edition Inline (inside Product)
# -------------------------
class ProductEditionInline(admin.StackedInline):
    model = ProductEdition
    extra = 0
    show_change_link = True
    fields = (
        "name",
        "slug",
        "gender",
        "concentration",
        "is_best_seller",
        "is_new_arrival",
    )
    prepopulated_fields = {"slug": ("name",)}


# -------------------------
# Product
# -------------------------
@admin.register(Product)
class ProductAdmin(ModelAdmin):
    list_display = ("display_name", "brand", "category")
    list_filter = ("brand", "category")
    search_fields = ("name", "brand__name")
    prepopulated_fields = {"slug": ("name",)}

    inlines = [
        ProductEditionInline,
    ]

    def display_name(self, obj):
        return f"{obj.brand.name} {obj.name}"

    display_name.short_description = "Product"


# -------------------------
# Product Variant
# -------------------------
@admin.register(ProductVariant)
class ProductVariantAdmin(ModelAdmin):
    list_display = (
        "display_name_formatted",
        "price_label",
        "decant_badge",
    )
    list_filter = ("is_decant", "edition__product__brand")
    list_select_related = ("edition__product__brand",)
    autocomplete_fields = ("edition",)
    search_fields = (
        "edition__product__name",
        "edition__name",
        "edition__product__brand__name",
    )

    @display(description="Variant Product", header=True)
    def display_name_formatted(self, obj):
        return [obj.display_name(), f"SKU: {obj.id}"]

    @display(description="Selling Price", header=True)
    def price_label(self, obj):
        return [f"₹{obj.selling_price}", "Current Rate"]

    @display(description="Type", boolean=True)
    def decant_badge(self, obj):
        return obj.is_decant


# -------------------------
# Product Edition (Main Notes View)
# -------------------------
@admin.register(ProductEdition)
class ProductEditionAdmin(ModelAdmin):
    list_display = (
        "display_name",
        "notes_summary",
        "gender",
        "concentration",
    )

    search_fields = (
        "product__name",
        "product__brand__name",
        "name",
    )

    inlines = [EditionNoteInline]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("edition_notes__note")
        )

    def notes_summary(self, obj):
        notes = (
            obj.edition_notes
            .annotate(
                position_order=Case(
                    When(position="top", then=Value(1)),
                    When(position="heart", then=Value(2)),
                    When(position="base", then=Value(3)),
                    default=Value(99),
                    output_field=IntegerField(),
                )
            )
            .order_by("position_order", "note__name")
        )

        grouped = {"top": [], "heart": [], "base": []}
        for en in notes:
            grouped[en.position].append(en.note.name)

        parts = []
        if grouped["top"]:
            parts.append("<strong>Top:</strong> " + ", ".join(grouped["top"]))
        if grouped["heart"]:
            parts.append("<strong>Heart:</strong> " + ", ".join(grouped["heart"]))
        if grouped["base"]:
            parts.append("<strong>Base:</strong> " + ", ".join(grouped["base"]))

        return mark_safe("<br>".join(parts) if parts else "—")

    notes_summary.short_description = "Notes"



