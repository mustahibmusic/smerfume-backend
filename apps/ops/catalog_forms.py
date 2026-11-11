"""Forms for the Smerfume Panel Catalog pages.

GET filter forms validate the query string; only cleaned values reach the
query. Gallery forms only parse input: every gallery write goes through
apps.catalog.image_services, which validates and processes the images.
"""

from django import forms

from apps.catalog.image_services import ALT_TEXT_MAX_LENGTH, MAX_FILES_PER_UPLOAD
from apps.catalog.models import Brand, Category, PerfumeNote, ProductEdition, ProductVariantImage

STATE_CHOICES = [("", "All"), ("active", "Active"), ("inactive", "Inactive")]


def _with_all(choices):
    return [("", "All"), *choices]


def filter_state(queryset, state):
    if state == "active":
        return queryset.filter(is_active=True)
    if state == "inactive":
        return queryset.filter(is_active=False)
    return queryset


class BrandFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    brand_category = forms.ChoiceField(
        required=False, choices=_with_all(Brand.BRAND_CATEGORY_CHOICES), label="Category",
    )
    state = forms.ChoiceField(required=False, choices=STATE_CHOICES)


class PerfumeNoteFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    notes_category = forms.ChoiceField(
        required=False, choices=_with_all(PerfumeNote.PERFUME_NOTE_CATEGORY_CHOICES), label="Family",
    )


class ProductFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Product or brand")
    brand = forms.ModelChoiceField(
        queryset=Brand.objects.order_by("name"), to_field_name="slug", required=False, empty_label="All",
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.order_by("name"), to_field_name="slug", required=False, empty_label="All",
    )
    state = forms.ChoiceField(required=False, choices=STATE_CHOICES)


class EditionFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Product, edition or brand")
    gender = forms.ChoiceField(required=False, choices=_with_all(ProductEdition.GENDER_CHOICES))
    concentration = forms.ChoiceField(
        required=False, choices=_with_all(ProductEdition.CONCENTRATION_CHOICES),
    )
    state = forms.ChoiceField(required=False, choices=STATE_CHOICES)


class VariantFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="SKU, barcode or name")
    brand = forms.ModelChoiceField(
        queryset=Brand.objects.order_by("name"), to_field_name="slug", required=False, empty_label="All",
    )
    kind = forms.ChoiceField(
        required=False, label="Type", choices=[("", "All"), ("bottle", "Full bottle"), ("decant", "Decant")],
    )
    state = forms.ChoiceField(required=False, choices=STATE_CHOICES)


class _MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class GalleryUploadForm(forms.Form):
    images = forms.FileField(
        widget=_MultipleFileInput(attrs={"multiple": True, "accept": "image/*"}), required=False,
        help_text=f"Up to {MAX_FILES_PER_UPLOAD} files at a time.",
    )


class GalleryRoleForm(forms.Form):
    role = forms.ChoiceField(choices=ProductVariantImage.ROLE_CHOICES)


class GalleryAltTextForm(forms.Form):
    alt_text = forms.CharField(required=False, max_length=ALT_TEXT_MAX_LENGTH, strip=True, label="Alt text")
