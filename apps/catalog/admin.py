import json

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.paginator import InfinitePaginator
from django.utils.safestring import mark_safe

from .image_processing import MAX_UPLOAD_BYTES
from .image_services import (
    VariantImageError,
    add_variant_images,
    display_ordered_images,
    remove_variant_image,
    reorder_variant_images,
    replace_variant_image,
    set_variant_image_role,
    update_variant_image_alt_text,
)


from .models import (
    Brand,
    Category,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    ProductVariantImage,
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
        "image",
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
# Product Variant (+ Manage Images gallery page)
# -------------------------
def _image_payload(image):
    return {
        "public_id": str(image.public_id),
        "url": image.image.url,
        "role": image.role,
        "role_label": image.get_role_display(),
        "alt_text": image.alt_text,
        "sort_order": image.sort_order,
        "width": image.width,
        "height": image.height,
    }


@admin.register(ProductVariant)
class ProductVariantAdmin(ModelAdmin):
    """Gallery images are managed on a dedicated page (manage_images_view)
    through apps.catalog.image_services; there is no image inline. The
    legacy single ProductVariant.image is no longer editable here (still
    stored and served by the API) and is shown read-only when present."""

    list_display = (
        "display_name_formatted",
        "price_label",
        "decant_badge",
        "image_preview",
        "image_count",
    )
    list_filter = ("is_decant", "edition__product__brand")
    list_select_related = ("edition__product__brand",)
    autocomplete_fields = ("edition",)
    search_fields = (
        "edition__product__name",
        "edition__name",
        "edition__product__brand__name",
        "sku",
        "barcode",
    )
    FORM_FIELDS = (
        "is_active", "edition", "sku", "barcode", "size_ml", "is_decant",
        "mrp", "selling_price", "shipping_surcharge",
    )
    readonly_fields = ("gallery_preview", "legacy_image")

    def get_fields(self, request, obj=None):
        fields = list(self.FORM_FIELDS)
        if obj is not None:
            fields.append("gallery_preview")
            if obj.image:
                fields.append("legacy_image")
        return fields

    def get_queryset(self, request):
        primary = ProductVariantImage.objects.filter(role=ProductVariantImage.ROLE_PRIMARY)
        return (
            super().get_queryset(request)
            .annotate(_image_count=Count("images"))
            .prefetch_related(Prefetch("images", queryset=primary, to_attr="primary_images"))
        )

    @display(description="Variant Product", header=True)
    def display_name_formatted(self, obj):
        return [obj.display_name(), f"SKU: {obj.sku}"]

    @display(description="Selling Price", header=True)
    def price_label(self, obj):
        return [f"₹{obj.selling_price}", "Current Rate"]

    @display(description="Type", boolean=True)
    def decant_badge(self, obj):
        return obj.is_decant

    @display(description="Image")
    def image_preview(self, obj):
        """Gallery primary image, falling back to the legacy image."""
        primary = getattr(obj, "primary_images", None)
        source = primary[0].image if primary else obj.image
        if source:
            return format_html('<img src="{}" alt="" style="height:60px;border-radius:4px;">', source.url)
        return "—"

    @display(description="Images")
    def image_count(self, obj):
        return f"{getattr(obj, '_image_count', 0)} / {ProductVariantImage.MAX_IMAGES_PER_VARIANT}"

    @display(description="Gallery")
    def gallery_preview(self, obj):
        if not obj or not obj.pk:
            return "—"
        images = list(display_ordered_images(obj.images.all()))
        return render_to_string("admin/catalog/productvariant/gallery_preview.html", {
            "images": images,
            "count": len(images),
            "max_images": ProductVariantImage.MAX_IMAGES_PER_VARIANT,
            "manage_url": reverse("admin:catalog_productvariant_images", args=[obj.pk]),
        })

    @display(description="Legacy image (not used by the gallery)")
    def legacy_image(self, obj):
        if not obj or not obj.image:
            return "—"
        return format_html(
            '<a href="{0}" target="_blank" rel="noopener"><img src="{0}" alt="" '
            'style="height:60px;border-radius:4px;"></a><div>{1}</div>',
            obj.image.url, obj.image.name,
        )

    # --- Manage Images page ---

    def get_urls(self):
        info = "catalog_productvariant"
        wrap = self.admin_site.admin_view
        urls = [
            path("<path:object_id>/images/", wrap(self.manage_images_view), name=f"{info}_images"),
            path("<path:object_id>/images/upload/", wrap(self.upload_images_view), name=f"{info}_images_upload"),
            path("<path:object_id>/images/reorder/", wrap(self.reorder_images_view), name=f"{info}_images_reorder"),
            path(
                "<path:object_id>/images/<uuid:image_id>/role/",
                wrap(self.image_role_view), name=f"{info}_image_role",
            ),
            path(
                "<path:object_id>/images/<uuid:image_id>/alt-text/",
                wrap(self.image_alt_text_view), name=f"{info}_image_alt_text",
            ),
            path(
                "<path:object_id>/images/<uuid:image_id>/replace/",
                wrap(self.image_replace_view), name=f"{info}_image_replace",
            ),
            path(
                "<path:object_id>/images/<uuid:image_id>/remove/",
                wrap(self.image_remove_view), name=f"{info}_image_remove",
            ),
        ]
        return urls + super().get_urls()

    def _gallery_variant(self, request, object_id):
        """The variant, if the user may see its gallery (else 404/403)."""
        variant = self.get_object(request, unquote(object_id))
        if variant is None:
            raise Http404("Variant not found.")
        if not (self.has_view_permission(request, variant)
                and request.user.has_perm("catalog.view_productvariantimage")):
            raise PermissionDenied
        return variant

    @staticmethod
    def _require(request, codename):
        if not request.user.has_perm(f"catalog.{codename}"):
            raise PermissionDenied

    @staticmethod
    def _gallery_image(variant, image_id):
        return get_object_or_404(ProductVariantImage, public_id=image_id, variant=variant)

    @staticmethod
    def _wants_json(request):
        return "application/json" in request.headers.get("Accept", "")

    def _manage_url(self, variant):
        return reverse("admin:catalog_productvariant_images", args=[variant.pk])

    def _done(self, request, variant, message, **data):
        if self._wants_json(request):
            return JsonResponse({"message": message, **data})
        self.message_user(request, message, level=messages.SUCCESS)
        return redirect(self._manage_url(variant))

    def _failed(self, request, variant, error):
        if self._wants_json(request):
            return JsonResponse({"error": error}, status=400)
        self.message_user(request, error, level=messages.ERROR)
        return redirect(self._manage_url(variant))

    def manage_images_view(self, request, object_id):
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        variant = self._gallery_variant(request, object_id)
        images = list(display_ordered_images(variant.images.all()))
        user = request.user
        context = {
            **self.admin_site.each_context(request),
            "title": "Manage images",
            "context_line": f"{variant.display_name()} · SKU {variant.sku}",
            "opts": self.model._meta,
            "variant": variant,
            "images": images,
            "count": len(images),
            "max_images": ProductVariantImage.MAX_IMAGES_PER_VARIANT,
            "free_slots": max(ProductVariantImage.MAX_IMAGES_PER_VARIANT - len(images), 0),
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "role_choices": ProductVariantImage.ROLE_CHOICES,
            "can_add": user.has_perm("catalog.add_productvariantimage"),
            "can_change": user.has_perm("catalog.change_productvariantimage"),
            "can_delete": user.has_perm("catalog.delete_productvariantimage"),
            "change_url": reverse("admin:catalog_productvariant_change", args=[variant.pk]),
            "upload_url": reverse("admin:catalog_productvariant_images_upload", args=[variant.pk]),
            "reorder_url": reverse("admin:catalog_productvariant_images_reorder", args=[variant.pk]),
        }
        return TemplateResponse(request, "admin/catalog/productvariant/manage_images.html", context)

    def upload_images_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "add_productvariantimage")
        try:
            result = add_variant_images(variant, request.FILES.getlist("images"), request.user)
        except VariantImageError as exc:
            return self._failed(request, variant, str(exc))
        if self._wants_json(request):
            return JsonResponse({
                "created": [_image_payload(image) for image in result.created],
                "rejected": [{"name": name, "message": msg} for name, msg in result.rejected],
                "warnings": [{"name": name, "message": msg} for name, msg in result.warnings],
            })
        for name, msg in result.rejected:
            self.message_user(request, f"{name}: {msg}", level=messages.ERROR)
        for name, msg in result.warnings:
            self.message_user(request, f"{name}: {msg}", level=messages.WARNING)
        if result.created:
            self.message_user(request, f"Added {len(result.created)} image(s).", level=messages.SUCCESS)
        return redirect(self._manage_url(variant))

    def reorder_images_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "change_productvariantimage")
        try:
            order = json.loads(request.POST.get("order", ""))
            if not isinstance(order, list):
                raise ValueError
        except ValueError:
            return self._failed(request, variant, "The new order could not be read.")
        try:
            reorder_variant_images(variant, order, request.user)
        except VariantImageError as exc:
            return self._failed(request, variant, str(exc))
        return self._done(request, variant, "Order saved.")

    def image_role_view(self, request, object_id, image_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "change_productvariantimage")
        image = self._gallery_image(variant, image_id)
        try:
            image = set_variant_image_role(image, request.POST.get("role", ""), request.user)
        except VariantImageError as exc:
            return self._failed(request, variant, str(exc))
        return self._done(request, variant, f"Image set to {image.get_role_display()}.", image=_image_payload(image))

    def image_alt_text_view(self, request, object_id, image_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "change_productvariantimage")
        image = self._gallery_image(variant, image_id)
        try:
            image = update_variant_image_alt_text(image, request.POST.get("alt_text", ""), request.user)
        except VariantImageError as exc:
            return self._failed(request, variant, str(exc))
        return self._done(request, variant, "Alt text saved.", image=_image_payload(image))

    def image_replace_view(self, request, object_id, image_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "change_productvariantimage")
        image = self._gallery_image(variant, image_id)
        upload = request.FILES.get("image")
        if upload is None:
            return self._failed(request, variant, "No edited image was received.")
        try:
            image = replace_variant_image(image, upload, request.user)
        except VariantImageError as exc:
            return self._failed(request, variant, str(exc))
        return self._done(request, variant, "Edited image saved.", image=_image_payload(image))

    def image_remove_view(self, request, object_id, image_id):
        if request.method not in ("GET", "POST"):
            return HttpResponseNotAllowed(["GET", "POST"])
        variant = self._gallery_variant(request, object_id)
        self._require(request, "delete_productvariantimage")
        image = self._gallery_image(variant, image_id)
        if request.method == "POST":
            remove_variant_image(image, request.user)
            return self._done(request, variant, "Image removed.")
        context = {
            **self.admin_site.each_context(request),
            "title": "Remove image",
            "opts": self.model._meta,
            "variant": variant,
            "image": image,
            "back_url": self._manage_url(variant),
        }
        return TemplateResponse(request, "admin/catalog/productvariant/confirm_remove_image.html", context)


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
        "is_indexable",
        "image_preview",
    )
    list_filter = ("is_indexable",)

    search_fields = (
        "product__name",
        "product__brand__name",
        "name",
    )

    fieldsets = (
        (None, {
            "fields": (
                "product", "name", "slug", "image", "image_preview",
                "gender", "concentration", "release_year",
                "is_best_seller", "is_new_arrival",
            ),
        }),
        ("SEO", {
            "fields": (
                "seo_title", "meta_description",
                "og_title", "og_description",
                "is_indexable",
            ),
            "description": (
                "Optional overrides for this edition's canonical product page. "
                "Leave blank to use the auto-generated title/description."
            ),
        }),
    )

    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("image_preview",)
    inlines = [EditionNoteInline]

    list_select_related = ("product__brand",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("edition_notes__note")
        )

    @display(description="Image")
    def image_preview(self, obj):
        if obj.image:
            return mark_safe(f'<img src="{obj.image.url}" style="height:60px;border-radius:4px;">')
        return "—"

    def notes_summary(self, obj):
        # Uses the prefetched notes (get_queryset); re-querying here would
        # cost one query per row.
        grouped = {"top": [], "heart": [], "base": []}
        for en in sorted(obj.edition_notes.all(), key=lambda en: en.note.name):
            if en.position in grouped:
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



