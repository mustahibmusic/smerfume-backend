from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case, IntegerField, Value, When

from apps.core.models import BaseModel


class Brand(BaseModel):

    BRAND_CATEGORY_CHOICES = [
        ("designer", "Designer"),
        ("middle eastern", "Middle Eastern"),
        ("niche", "Niche"),
    ]

    name = models.CharField(max_length=100, unique=True)
    brand_category = models.CharField(max_length=100, choices=BRAND_CATEGORY_CHOICES, db_index=True, default="designer")
    slug = models.SlugField(unique=True, db_index=True)

    class Meta:
        indexes = [
            # Backs icontains search on Brand.name (ProductViewSet's
            # search_fields includes "brand__name") with a trigram GIN index
            # instead of a full table scan.
            GinIndex(name="brand_name_trgm_idx", fields=["name"], opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self):
        return self.name


class Category(BaseModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True, db_index=True)

    def __str__(self):
        return self.name


class PerfumeNote(BaseModel):
    PERFUME_NOTE_CATEGORY_CHOICES = [
        ("fresh", "Fresh"),
        ("citrus", "Citrus"),
        ("fruity", "Fruity"),
        ("floral", "Floral"),
        ("sweet", "Sweet / Gourmand"),
        ("spicy", "Spicy"),
        ("woody", "Woody"),
        ("ambery", "Ambery / Resinous"),
        ("musky", "Musky / Animalic"),
        ("leathery", "Leather / Suede"),
        ("smoky", "Smoky / Incense"),
        ("aquatic", "Aquatic / Ozonic"),
        ("aromatic", "Aromatic / Green"),
    ]


    name = models.CharField(max_length=100, unique=True, db_index=True)
    notes_category = models.CharField(max_length=100, blank=True, choices=PERFUME_NOTE_CATEGORY_CHOICES, db_index=True)

    class Meta:
        indexes = [
            # Backs icontains search on note name — ProductViewSet's ?search=
            # and ?note= traverse editions__edition_notes__note__name.
            GinIndex(name="perfumenote_name_trgm_idx", fields=["name"], opclasses=["gin_trgm_ops"]),
        ]

    def __str__(self):
        return self.name


class Product(BaseModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, db_index=True)

    brand = models.ForeignKey(Brand, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)

    class Meta:
        indexes = [
            # Backs icontains search on Product.name (ProductViewSet's
            # search_fields includes "name") with a trigram GIN index.
            GinIndex(name="product_name_trgm_idx", fields=["name"], opclasses=["gin_trgm_ops"]),
        ]

    def display_name(self):
        if self.name:
            return f"{self.brand.name} {self.name}"
        return self.name

    def __str__(self):
        return f"{self.brand.name} {self.name}"


class ProductEdition(BaseModel):
    GENDER_CHOICES = [
        ("men", "Men"),
        ("women", "Women"),
        ("unisex", "Unisex"),
    ]

    CONCENTRATION_CHOICES = [
        ("edc", "Eau de Cologne"),
        ("edt", "Eau de Toilette"),
        ("edp", "Eau de Parfum"),
        ("extrait", "Extrait de Parfum"),
        ("attar", "Attar"),
    ]

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="editions"
    )

    name = models.CharField(max_length=100, null=True, blank=True)
    slug = models.SlugField(null=True, blank=True, db_index=True)

    gender = models.CharField(
        max_length=10,
        choices=GENDER_CHOICES,
        default="unisex",
        db_index=True
    )

    concentration = models.CharField(
        max_length=20,
        choices=CONCENTRATION_CHOICES,
        default="edp",
        db_index=True
    )

    notes = models.ManyToManyField(
        PerfumeNote,
        through="EditionNote",
        related_name="editions",
        blank=True
    )

    image = models.ImageField(upload_to="catalog/editions/", null=True, blank=True)

    release_year = models.PositiveSmallIntegerField(null=True, blank=True)

    is_best_seller = models.BooleanField(default=False)
    is_new_arrival = models.BooleanField(default=False)

    # ── SEO ──────────────────────────────────────────────────────────────
    # An Edition (e.g. "Rasasi Hawas For Him EDP") is the canonical
    # customer-facing product page — it's the level at which fragrance
    # notes/gender/concentration differ, so it's the right granularity for
    # search-engine indexing. Size/decant Variants are purchase options
    # selected on that same page, not separate indexable pages. All fields
    # below are optional overrides: when blank, the API resolves a sensible
    # fallback (see ProductEditionDetailSerializer) rather than leaving the
    # page without a title/description.
    seo_title = models.CharField(
        max_length=70, blank=True,
        help_text="Overrides the auto-generated <title>. Falls back to the edition's display name if blank.",
    )
    meta_description = models.CharField(
        max_length=300, blank=True,
        help_text="Meta description for search results. No automatic fallback — left blank if unset.",
    )
    og_title = models.CharField(
        max_length=95, blank=True,
        help_text="Open Graph title override. Falls back to seo_title, then the display name.",
    )
    og_description = models.CharField(
        max_length=300, blank=True,
        help_text="Open Graph description override. Falls back to meta_description.",
    )
    is_indexable = models.BooleanField(
        default=True,
        help_text=(
            "Whether this edition's page should be indexable by search engines. "
            "Does not affect whether the page is reachable — only its SEO robots "
            "signal. Use this to noindex a thin/duplicate edition without "
            "deactivating it."
        ),
    )

    class Meta:
        constraints = [
            # Scoped to product, not global — edition slugs are only unique
            # within their parent product (e.g. two different products can
            # each have an edition slugged "original"). Excludes null slugs:
            # single-edition products intentionally leave name/slug unset.
            models.UniqueConstraint(
                fields=["product", "slug"],
                condition=models.Q(slug__isnull=False),
                name="unique_edition_slug_per_product",
            ),
        ]
        indexes = [
            GinIndex(name="edition_name_trgm_idx", fields=["name"], opclasses=["gin_trgm_ops"]),
        ]

    def ordered_notes(self):
        """
        Returns EditionNote queryset ordered as:
        Top -> Heart -> Base
        """
        return (
            self.edition_notes
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

    def display_name(self):
        # Used as the SEO title/OG title fallback when seo_title/og_title are
        # blank — always includes the brand so a single-edition product
        # (name=None) doesn't fall back to a bare, brand-less product name.
        if self.name:
            return f"{self.product.brand.name} {self.product.name} {self.name}"
        return f"{self.product.brand.name} {self.product.name}"

    def __str__(self):
        return self.display_name()



class ProductVariant(BaseModel):
    edition = models.ForeignKey(
        ProductEdition,
        on_delete=models.CASCADE,
        related_name="variants"
    )

    image = models.ImageField(upload_to="catalog/variants/", null=True, blank=True)

    sku = models.CharField(max_length=64, unique=True, db_index=True)
    size_ml = models.PositiveIntegerField()
    is_decant = models.BooleanField(default=False)

    mrp = models.DecimalField(max_digits=10, decimal_places=2)
    selling_price = models.DecimalField(max_digits=10, decimal_places=2)
    shipping_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(shipping_surcharge__gte=0),
                name="productvariant_shipping_surcharge_gte_0",
            ),
        ]

    def display_name(self):
        brand = self.edition.product.brand.name
        product_and_edition = self.edition.display_name()

        variant = f"{self.size_ml}ml"
        if self.is_decant:
            variant += " (Decant)"

        return f"{product_and_edition} {variant}"

    def __str__(self):
        return self.display_name()


class ProductVariantImage(BaseModel):
    """One image for a ProductVariant's gallery. role=primary is the
    canonical image for the product page, structured data, Open Graph,
    Google Images, and product feeds — role=secondary is only ever used for
    the product-card hover effect, never treated as the SEO/canonical image.
    Deleting the primary/secondary image never auto-promotes another image
    into that role; a human has to choose the replacement explicitly."""

    MAX_IMAGES_PER_VARIANT = 10

    ROLE_PRIMARY = "primary"
    ROLE_SECONDARY = "secondary"
    ROLE_GALLERY = "gallery"

    ROLE_CHOICES = (
        (ROLE_PRIMARY, "Primary"),
        (ROLE_SECONDARY, "Secondary / Hover"),
        (ROLE_GALLERY, "Gallery"),
    )

    variant = models.ForeignKey(
        ProductVariant, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(upload_to="catalog/variant_images/")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_GALLERY)
    alt_text = models.CharField(
        max_length=255, blank=True,
        help_text="Accessibility/SEO alt text. Shown to screen readers and used by image search.",
    )
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Deterministic gallery order, lowest first."
    )

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            # At most one primary / one secondary per variant. Gallery has
            # no such limit (besides the shared 10-image cap enforced in
            # clean()). DB-level, so it holds even outside the admin.
            models.UniqueConstraint(
                fields=["variant"], condition=models.Q(role="primary"),
                name="unique_primary_image_per_variant",
            ),
            models.UniqueConstraint(
                fields=["variant"], condition=models.Q(role="secondary"),
                name="unique_secondary_image_per_variant",
            ),
        ]

    def clean(self):
        if self.variant_id is not None:
            existing = ProductVariantImage.objects.filter(variant_id=self.variant_id)
            if self.pk:
                existing = existing.exclude(pk=self.pk)
            if existing.count() >= self.MAX_IMAGES_PER_VARIANT:
                raise ValidationError(
                    f"A variant can have at most {self.MAX_IMAGES_PER_VARIANT} images."
                )

    def __str__(self):
        return f"{self.variant.display_name()} — {self.get_role_display()} #{self.sort_order}"


class EditionNote(BaseModel):
    NOTE_POSITION_CHOICES = [
        ("top", "Top"),
        ("heart", "Heart"),
        ("base", "Base"),
    ]

    edition = models.ForeignKey(
        ProductEdition,
        on_delete=models.CASCADE,
        related_name="edition_notes"
    )

    note = models.ForeignKey(
        PerfumeNote,
        on_delete=models.CASCADE,
        related_name="note_editions"
    )

    position = models.CharField(
        max_length=10,
        choices=NOTE_POSITION_CHOICES
    )

    class Meta:
        unique_together = ("edition", "note")
    

    def __str__(self):
        return f"{self.note.name} ({self.position})"
