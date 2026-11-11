"""Catalog pages for the Smerfume Panel (/ops/catalog/).

Brands, perfume notes, products, editions and variants are read-only here:
the catalog has no domain services for editing them, so their edits stay
in Django Admin. The Product -> Edition -> Variant hierarchy is shown and
linked both ways. Variant gallery images are managed here through
apps.catalog.image_services, the only write path for gallery images,
with the same model permissions Django Admin's gallery page checks.

URLs use business identifiers: brand and product slugs, variant SKU, and
the public_id of editions and notes (edition slugs are only unique within
their product).
"""

import logging

from django.contrib import messages
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.catalog.image_processing import MAX_UPLOAD_BYTES
from apps.catalog.image_services import (
    VariantImageError,
    add_variant_images,
    display_ordered_images,
    remove_variant_image,
    reorder_variant_images,
    set_variant_image_role,
    update_variant_image_alt_text,
)
from apps.catalog.models import (
    Brand,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
    ProductVariantImage,
)
from apps.ops.catalog_forms import (
    BrandFilterForm,
    EditionFilterForm,
    GalleryAltTextForm,
    GalleryRoleForm,
    GalleryUploadForm,
    PerfumeNoteFilterForm,
    ProductFilterForm,
    VariantFilterForm,
    filter_state,
)
from apps.ops.mixins import FilteredListMixin, OpsAccessMixin, render_forbidden

logger = logging.getLogger(__name__)

VIEW_BRAND = "catalog.view_brand"
VIEW_NOTE = "catalog.view_perfumenote"
VIEW_PRODUCT = "catalog.view_product"
VIEW_EDITION = "catalog.view_productedition"
VIEW_VARIANT = "catalog.view_productvariant"
VIEW_IMAGE = "catalog.view_productvariantimage"
POSITIONS = ("top", "heart", "base")

_PRIMARY = Prefetch(
    "images", queryset=ProductVariantImage.objects.filter(role=ProductVariantImage.ROLE_PRIMARY),
    to_attr="primary_images",
)


def _links(user):
    """Which catalog levels this user may open (hides dead links)."""
    return {
        "can_view_brand": user.has_perm(VIEW_BRAND),
        "can_view_product": user.has_perm(VIEW_PRODUCT),
        "can_view_edition": user.has_perm(VIEW_EDITION),
        "can_view_variant": user.has_perm(VIEW_VARIANT),
        "can_view_note": user.has_perm(VIEW_NOTE),
    }


class _CatalogPage(OpsAccessMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_links(self.request.user))
        return context


# ── Brands ───────────────────────────────────────────────────────────────────


class BrandListView(_CatalogPage, FilteredListMixin, ListView):
    """GET /ops/catalog/brands/?q=&brand_category=&state="""

    permission_required = (VIEW_BRAND,)
    nav_active = "brands"
    template_name = "ops/catalog/brand_list.html"
    filter_form_class = BrandFilterForm

    def base_queryset(self):
        return Brand.objects.annotate(product_count=Count("product")).order_by("name")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(Q(name__icontains=data["q"]) | Q(slug__icontains=data["q"]))
        if data.get("brand_category"):
            queryset = queryset.filter(brand_category=data["brand_category"])
        return filter_state(queryset, data.get("state"))


class BrandDetailView(_CatalogPage, TemplateView):
    """GET /ops/catalog/brands/<slug>/ with the brand's products."""

    permission_required = (VIEW_BRAND,)
    nav_active = "brands"
    template_name = "ops/catalog/brand_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        brand = get_object_or_404(Brand, slug=kwargs["slug"])
        context.update({
            "brand": brand,
            "products": brand.product_set.select_related("category")
            .annotate(edition_count=Count("editions")).order_by("name"),
        })
        return context


# ── Perfume notes ────────────────────────────────────────────────────────────


class PerfumeNoteListView(_CatalogPage, FilteredListMixin, ListView):
    """GET /ops/catalog/notes/?q=&notes_category="""

    permission_required = (VIEW_NOTE,)
    nav_active = "perfume_notes"
    template_name = "ops/catalog/note_list.html"
    filter_form_class = PerfumeNoteFilterForm

    def base_queryset(self):
        return PerfumeNote.objects.annotate(edition_count=Count("note_editions")).order_by("name")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(name__icontains=data["q"])
        if data.get("notes_category"):
            queryset = queryset.filter(notes_category=data["notes_category"])
        return queryset


class PerfumeNoteDetailView(_CatalogPage, TemplateView):
    """GET /ops/catalog/notes/<public_id>/ with the editions using it."""

    permission_required = (VIEW_NOTE,)
    nav_active = "perfume_notes"
    template_name = "ops/catalog/note_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        note = get_object_or_404(PerfumeNote, public_id=kwargs["public_id"])
        context.update({
            "note": note,
            "uses": note.note_editions.select_related("edition__product__brand")
            .order_by("edition__product__brand__name", "edition__product__name", "edition__name"),
        })
        return context


# ── Products ─────────────────────────────────────────────────────────────────


class ProductListView(_CatalogPage, FilteredListMixin, ListView):
    """GET /ops/catalog/products/?q=&brand=&category=&state="""

    permission_required = (VIEW_PRODUCT,)
    nav_active = "products"
    template_name = "ops/catalog/product_list.html"
    filter_form_class = ProductFilterForm

    def base_queryset(self):
        return (
            Product.objects.select_related("brand", "category")
            .annotate(edition_count=Count("editions", distinct=True),
                      variant_count=Count("editions__variants", distinct=True))
            .order_by("brand__name", "name")
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(Q(name__icontains=q) | Q(brand__name__icontains=q) | Q(slug__icontains=q))
        if data.get("brand"):
            queryset = queryset.filter(brand=data["brand"])
        if data.get("category"):
            queryset = queryset.filter(category=data["category"])
        return filter_state(queryset, data.get("state"))


class ProductDetailView(_CatalogPage, TemplateView):
    """GET /ops/catalog/products/<slug>/ with editions and their variants."""

    permission_required = (VIEW_PRODUCT,)
    nav_active = "products"
    template_name = "ops/catalog/product_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = get_object_or_404(Product.objects.select_related("brand", "category"), slug=kwargs["slug"])
        context.update({
            "product": product,
            "editions": product.editions.prefetch_related(
                Prefetch("variants", queryset=ProductVariant.objects.order_by("is_decant", "size_ml", "sku")),
            ).order_by("name", "id"),
        })
        return context


# ── Editions ─────────────────────────────────────────────────────────────────


class EditionListView(_CatalogPage, FilteredListMixin, ListView):
    """GET /ops/catalog/editions/?q=&gender=&concentration=&state="""

    permission_required = (VIEW_EDITION,)
    nav_active = "editions"
    template_name = "ops/catalog/edition_list.html"
    filter_form_class = EditionFilterForm

    def base_queryset(self):
        return (
            ProductEdition.objects.select_related("product__brand")
            .annotate(variant_count=Count("variants"))
            .order_by("product__brand__name", "product__name", "name", "id")
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(name__icontains=q) | Q(product__name__icontains=q) | Q(product__brand__name__icontains=q)
            )
        for field in ("gender", "concentration"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return filter_state(queryset, data.get("state"))


class EditionDetailView(_CatalogPage, TemplateView):
    """GET /ops/catalog/editions/<public_id>/: notes, SEO and variants."""

    permission_required = (VIEW_EDITION,)
    nav_active = "editions"
    template_name = "ops/catalog/edition_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        edition = get_object_or_404(
            ProductEdition.objects.select_related("product__brand"), public_id=kwargs["public_id"],
        )
        grouped = {position: [] for position in POSITIONS}
        for edition_note in EditionNote.objects.filter(edition=edition).select_related("note").order_by("note__name"):
            grouped.setdefault(edition_note.position, []).append(edition_note.note)
        context.update({
            "edition": edition,
            "note_groups": [(label, grouped.get(key, [])) for key, label in EditionNote.NOTE_POSITION_CHOICES],
            "variants": edition.variants.prefetch_related(_PRIMARY).order_by("is_decant", "size_ml", "sku"),
        })
        return context


# ── Variants ─────────────────────────────────────────────────────────────────


class VariantListView(_CatalogPage, FilteredListMixin, ListView):
    """GET /ops/catalog/variants/?q=&brand=&kind=&state="""

    permission_required = (VIEW_VARIANT,)
    nav_active = "variants"
    template_name = "ops/catalog/variant_list.html"
    filter_form_class = VariantFilterForm

    def base_queryset(self):
        return (
            ProductVariant.objects.select_related("edition__product__brand")
            .annotate(image_count=Count("images"))
            .prefetch_related(_PRIMARY)
            .order_by("edition__product__brand__name", "edition__product__name", "edition__name", "size_ml", "sku")
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(sku__icontains=q) | Q(barcode__icontains=q) | Q(edition__name__icontains=q)
                | Q(edition__product__name__icontains=q) | Q(edition__product__brand__name__icontains=q)
            )
        if data.get("brand"):
            queryset = queryset.filter(edition__product__brand=data["brand"])
        if data.get("kind") == "decant":
            queryset = queryset.filter(is_decant=True)
        elif data.get("kind") == "bottle":
            queryset = queryset.filter(is_decant=False)
        return filter_state(queryset, data.get("state"))


def _variant_or_404(sku):
    return get_object_or_404(ProductVariant.objects.select_related("edition__product__brand"), sku=sku)


class VariantDetailView(_CatalogPage, TemplateView):
    """GET /ops/catalog/variants/<sku>/: pricing, hierarchy and gallery.

    Prices are shown as stored; they are edited in Django Admin. The
    gallery is shown with ``catalog.view_productvariantimage``; upload,
    role, alt text, order and remove each need their own image permission.
    """

    permission_required = (VIEW_VARIANT,)
    nav_active = "variants"
    template_name = "ops/catalog/variant_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        variant = _variant_or_404(kwargs["sku"])
        can_view_gallery = user.has_perm(VIEW_IMAGE)
        images = list(display_ordered_images(variant.images.all())) if can_view_gallery else []
        can_change = user.has_perm("catalog.change_productvariantimage")
        for image in images:
            image.role_form = GalleryRoleForm(initial={"role": image.role}, prefix=str(image.public_id))
            image.alt_form = GalleryAltTextForm(initial={"alt_text": image.alt_text}, prefix=str(image.public_id))
        context.update({
            "variant": variant,
            "can_view_gallery": can_view_gallery,
            "images": images,
            "max_images": ProductVariantImage.MAX_IMAGES_PER_VARIANT,
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "can_add_image": user.has_perm("catalog.add_productvariantimage")
            and len(images) < ProductVariantImage.MAX_IMAGES_PER_VARIANT,
            "can_change_image": can_change,
            "can_delete_image": user.has_perm("catalog.delete_productvariantimage"),
            "upload_form": GalleryUploadForm(),
            "can_view_stock": user.has_perm("inventory.view_inventorystock"),
        })
        return context


class _GalleryWriteView(OpsAccessMixin, View):
    """POST-only base: the variant, its gallery permission, then the
    action's own permission. Errors from the service become messages."""

    http_method_names = ["post"]
    nav_active = "variants"
    write_permission = ""

    def dispatch(self, request, *args, **kwargs):
        self.permission_required = (VIEW_VARIANT, VIEW_IMAGE, self.write_permission)
        return super().dispatch(request, *args, **kwargs)

    def back(self, variant):
        return redirect(reverse("ops:variant_detail", args=[variant.sku]) + "#gallery")


class GalleryUploadView(_GalleryWriteView):
    """POST /ops/catalog/variants/<sku>/images/upload/ (``images`` files)."""

    write_permission = "catalog.add_productvariantimage"

    def post(self, request, sku):
        variant = _variant_or_404(sku)
        try:
            result = add_variant_images(variant, request.FILES.getlist("images"), request.user)
        except VariantImageError as exc:
            messages.error(request, str(exc))
            return self.back(variant)
        for name, message in result.rejected:
            messages.error(request, f"{name}: {message}")
        for name, message in result.warnings:
            messages.warning(request, f"{name}: {message}")
        if result.created:
            messages.success(request, f"Added {len(result.created)} image(s).")
        return self.back(variant)


class GalleryImageActionView(_GalleryWriteView):
    """POST /ops/catalog/variants/<sku>/images/<image_id>/<action>/

    ``role`` (field ``<image_id>-role``), ``alt-text`` (``<image_id>-alt_text``),
    ``up`` / ``down`` (reorder through reorder_variant_images) and
    ``remove`` (needs ``delete_productvariantimage``; no other image is
    promoted into its role).
    """

    ACTIONS = {
        "role": "catalog.change_productvariantimage",
        "alt-text": "catalog.change_productvariantimage",
        "up": "catalog.change_productvariantimage",
        "down": "catalog.change_productvariantimage",
        "remove": "catalog.delete_productvariantimage",
    }

    def dispatch(self, request, *args, **kwargs):
        if kwargs.get("action") not in self.ACTIONS:
            if not request.user.is_authenticated:
                return super().dispatch(request, *args, **kwargs)
            return render_forbidden(request)
        self.write_permission = self.ACTIONS[kwargs["action"]]
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, sku, image_id, action):
        variant = _variant_or_404(sku)
        image = get_object_or_404(ProductVariantImage, public_id=image_id, variant=variant)
        prefix = str(image.public_id)
        try:
            if action == "role":
                form = GalleryRoleForm(request.POST, prefix=prefix)
                if not form.is_valid():
                    raise VariantImageError("Choose primary, secondary or gallery.")
                image = set_variant_image_role(image, form.cleaned_data["role"], request.user)
                messages.success(request, f"Image set to {image.get_role_display()}.")
            elif action == "alt-text":
                form = GalleryAltTextForm(request.POST, prefix=prefix)
                if not form.is_valid():
                    raise VariantImageError("; ".join(e for errors in form.errors.values() for e in errors))
                update_variant_image_alt_text(image, form.cleaned_data["alt_text"], request.user)
                messages.success(request, "Alt text saved.")
            elif action in ("up", "down"):
                # Primary and secondary always show first; only gallery
                # images move, among themselves.
                ordered = list(display_ordered_images(variant.images.all()))
                fixed = [str(i.public_id) for i in ordered if i.role != ProductVariantImage.ROLE_GALLERY]
                gallery = [str(i.public_id) for i in ordered if i.role == ProductVariantImage.ROLE_GALLERY]
                if prefix in gallery:
                    index = gallery.index(prefix)
                    swap = index - 1 if action == "up" else index + 1
                    if 0 <= swap < len(gallery):
                        gallery[index], gallery[swap] = gallery[swap], gallery[index]
                        reorder_variant_images(variant, fixed + gallery, request.user)
                        messages.success(request, "Order saved.")
            else:
                remove_variant_image(image, request.user)
                messages.success(request, "Image removed.")
        except VariantImageError as exc:
            messages.error(request, str(exc))
        return self.back(variant)
