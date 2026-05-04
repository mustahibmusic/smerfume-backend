from django.db import models
from apps.core.models import BaseModel
from django.db.models import Case, When, Value, IntegerField
from typing import TYPE_CHECKING


class Brand(BaseModel):

    BRAND_CATEGORY_CHOICES = [
        ("designer", "Designer"),
        ("middle eastern", "Middle Eastern"),
        ("niche", "Niche"),
    ]

    name = models.CharField(max_length=100, unique=True)
    brand_category = models.CharField(max_length=100, null=False, choices=BRAND_CATEGORY_CHOICES, db_index=True, default="designer")
    slug = models.SlugField(unique=True, db_index=True)

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

    def __str__(self):
        return self.name


class Product(BaseModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, db_index=True)

    brand = models.ForeignKey(Brand, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)

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

    is_best_seller = models.BooleanField(default=False)
    is_new_arrival = models.BooleanField(default=False)

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
        if self.name:
            return f"{self.product.brand.name} {self.product.name} {self.name}"
        return self.product.name

    def __str__(self):
        # This is the line that fixes the "Object (1)" issue
        return f"{self.product.brand.name} {self.product.name} {self.name}"



class ProductVariant(BaseModel):
    edition = models.ForeignKey(
        ProductEdition,
        on_delete=models.CASCADE,
        related_name="variants"
    )

    size_ml = models.PositiveIntegerField()
    is_decant = models.BooleanField(default=False)

    mrp = models.DecimalField(max_digits=10, decimal_places=2)
    selling_price = models.DecimalField(max_digits=10, decimal_places=2)

    def display_name(self):
        brand = self.edition.product.brand.name
        product_and_edition = self.edition.display_name()

        variant = f"{self.size_ml}ml"
        if self.is_decant:
            variant += " (Decant)"

        return f"{product_and_edition} {variant}"

    def __str__(self):
        return self.display_name()


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
