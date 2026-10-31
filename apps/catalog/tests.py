"""
Test suite for the Product Search API.

Fixture layout
──────────────
Brands      : Chanel, Dior
Categories  : Fragrance, Body Care
Products    : Chance (Chanel/Fragrance), Sauvage (Dior/Fragrance), Hidden (inactive)
Editions
  chance-pour-homme : men / edp  / is_best_seller=True
  chance-pour-femme : women / edt / is_new_arrival=True
  chance-inactive   : is_active=False
  sauvage-edp       : men / edp
Variants (active unless noted)
  chance-pour-homme : 100 ml (₹4500), 50 ml (₹2700), 10 ml decant (₹500), 200 ml (inactive ₹8000)
  chance-pour-femme : 75 ml (₹3500)
  sauvage-edp       : 100 ml (₹5500)
Notes
  chance-pour-homme : Rose (top), Oud (base), Sandalwood (base)
  chance-pour-femme : Rose (top), Vanilla (heart)
  sauvage-edp       : Oud (top), Musk (base)

Coverage
────────
  ProductListTests              basic list, pagination
  ProductSearchTests            ?search=
  ProductBrandCategoryFilterTests  ?brand= / ?category=
  ProductEditionFilterTests     ?gender= / ?concentration= / ?is_best_seller= / ?is_new_arrival=
  ProductCrossEditionTests      cross-edition contamination guard
  ProductNoteFilterTests        ?note= (AND logic, whitespace, limits)
  ProductActiveFilterTests      inactive product / edition / variant exclusion
  ProductOrderingTests          ?ordering=
  ProductDetailTests            GET /products/{slug}/
  ProductSerializerFieldTests   serializer field contract & computed values
"""

import tempfile
from decimal import Decimal

from django.contrib import admin as django_admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from apps.inventory.models import InventoryStock, Warehouse

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

LIST_URL = "/api/catalog/products/"


def detail_url(slug):
    return f"/api/catalog/products/{slug}/"


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture
# ─────────────────────────────────────────────────────────────────────────────

class CatalogTestBase(TestCase):
    """Creates the shared DB fixture once per class via setUpTestData."""

    @classmethod
    def setUpTestData(cls):
        # Brands
        cls.chanel = Brand.objects.create(name="Chanel", slug="chanel")
        cls.dior   = Brand.objects.create(name="Dior",   slug="dior")

        # Categories
        cls.fragrance = Category.objects.create(name="Fragrance", slug="fragrance")
        cls.bodycare  = Category.objects.create(name="Body Care", slug="bodycare")

        # Perfume notes
        cls.note_rose      = PerfumeNote.objects.create(name="Rose",      notes_category="floral")
        cls.note_oud       = PerfumeNote.objects.create(name="Oud",       notes_category="woody")
        cls.note_sandalwood= PerfumeNote.objects.create(name="Sandalwood",notes_category="woody")
        cls.note_vanilla   = PerfumeNote.objects.create(name="Vanilla",   notes_category="sweet")
        cls.note_musk      = PerfumeNote.objects.create(name="Musk",      notes_category="musky")

        # Products
        cls.product_chance = Product.objects.create(
            name="Chance", slug="chance",
            brand=cls.chanel, category=cls.fragrance,
        )
        cls.product_sauvage = Product.objects.create(
            name="Sauvage", slug="sauvage",
            brand=cls.dior, category=cls.fragrance,
        )
        cls.product_inactive = Product.objects.create(
            name="Hidden", slug="hidden",
            brand=cls.chanel, category=cls.fragrance,
            is_active=False,
        )

        # Editions
        cls.edition_chance_men = ProductEdition.objects.create(
            product=cls.product_chance, name="Pour Homme",
            slug="chance-pour-homme", gender="men",
            concentration="edp", is_best_seller=True,
        )
        cls.edition_chance_women = ProductEdition.objects.create(
            product=cls.product_chance, name="Pour Femme",
            slug="chance-pour-femme", gender="women",
            concentration="edt", is_new_arrival=True,
        )
        cls.edition_chance_inactive = ProductEdition.objects.create(
            product=cls.product_chance, name="Inactive Edition",
            slug="chance-inactive", gender="unisex",
            concentration="edp", is_active=False,
        )
        cls.edition_sauvage = ProductEdition.objects.create(
            product=cls.product_sauvage, name="EDP",
            slug="sauvage-edp", gender="men",
            concentration="edp",
        )

        # Variants — chance pour homme (3 active + 1 inactive)
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=100,
            mrp=Decimal("5000.00"), selling_price=Decimal("4500.00"),
            sku="TEST-CHANCE-MEN-100ML",
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=50,
            mrp=Decimal("3000.00"), selling_price=Decimal("2700.00"),
            sku="TEST-CHANCE-MEN-50ML",
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=10, is_decant=True,
            mrp=Decimal("1000.00"), selling_price=Decimal("500.00"),
            sku="TEST-CHANCE-MEN-10ML-DECANT",
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=200,
            mrp=Decimal("9000.00"), selling_price=Decimal("8000.00"),
            is_active=False,
            sku="TEST-CHANCE-MEN-200ML-INACTIVE",
        )
        # Variants — chance pour femme
        ProductVariant.objects.create(
            edition=cls.edition_chance_women, size_ml=75,
            mrp=Decimal("4000.00"), selling_price=Decimal("3500.00"),
            sku="TEST-CHANCE-WOMEN-75ML",
        )
        # Variants — sauvage
        ProductVariant.objects.create(
            edition=cls.edition_sauvage, size_ml=100,
            mrp=Decimal("6000.00"), selling_price=Decimal("5500.00"),
            sku="TEST-SAUVAGE-100ML",
        )

        # Edition notes — chance pour homme: Rose (top), Oud (base), Sandalwood (base)
        EditionNote.objects.create(edition=cls.edition_chance_men, note=cls.note_rose,       position="top")
        EditionNote.objects.create(edition=cls.edition_chance_men, note=cls.note_oud,        position="base")
        EditionNote.objects.create(edition=cls.edition_chance_men, note=cls.note_sandalwood, position="base")
        # Edition notes — chance pour femme: Rose (top), Vanilla (heart)
        EditionNote.objects.create(edition=cls.edition_chance_women, note=cls.note_rose,    position="top")
        EditionNote.objects.create(edition=cls.edition_chance_women, note=cls.note_vanilla, position="heart")
        # Edition notes — sauvage: Oud (top), Musk (base)
        EditionNote.objects.create(edition=cls.edition_sauvage, note=cls.note_oud,  position="top")
        EditionNote.objects.create(edition=cls.edition_sauvage, note=cls.note_musk, position="base")

    def setUp(self):
        self.client = APIClient()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _slugs(self, response):
        """Return the set of product slugs from a paginated list response."""
        return {p["slug"] for p in response.data["results"]}

    def _edition(self, response, edition_slug):
        """Return an edition dict from a detail response by its slug."""
        return next(e for e in response.data["editions"] if e["slug"] == edition_slug)

    def _list_edition(self, response, product_slug, edition_slug):
        """Return an edition dict from a list response."""
        product = next(p for p in response.data["results"] if p["slug"] == product_slug)
        return next(e for e in product["editions"] if e["slug"] == edition_slug)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic list behaviour
# ─────────────────────────────────────────────────────────────────────────────

class ProductListTests(CatalogTestBase):

    def test_list_returns_200(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_list_returns_only_active_products(self):
        response = self.client.get(LIST_URL)
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertIn("sauvage", slugs)
        self.assertNotIn("hidden", slugs)

    def test_list_count_excludes_inactive_product(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.data["count"], 2)

    def test_list_response_is_paginated(self):
        response = self.client.get(LIST_URL)
        for key in ("count", "next", "previous", "results"):
            self.assertIn(key, response.data)

    def test_list_product_has_expected_top_level_fields(self):
        response = self.client.get(LIST_URL)
        product = next(p for p in response.data["results"] if p["slug"] == "chance")
        for field in ("public_id", "name", "slug", "brand", "category", "editions"):
            self.assertIn(field, product)

    def test_list_product_does_not_expose_internal_fields(self):
        response = self.client.get(LIST_URL)
        product = next(p for p in response.data["results"] if p["slug"] == "chance")
        for field in ("id", "is_active", "created_at", "updated_at"):
            self.assertNotIn(field, product)

    def test_list_invalid_page_number_returns_404(self):
        response = self.client.get(LIST_URL, {"page": "abc"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_page_beyond_total_returns_404(self):
        response = self.client.get(LIST_URL, {"page": 9999})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Search (?search=)
# ─────────────────────────────────────────────────────────────────────────────

class ProductSearchTests(CatalogTestBase):

    def test_search_by_product_name(self):
        response = self.client.get(LIST_URL, {"search": "Chance"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertNotIn("sauvage", slugs)

    def test_search_by_brand_name(self):
        response = self.client.get(LIST_URL, {"search": "Dior"})
        slugs = self._slugs(response)
        self.assertIn("sauvage", slugs)
        self.assertNotIn("chance", slugs)

    def test_search_by_edition_name(self):
        response = self.client.get(LIST_URL, {"search": "Pour Homme"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertNotIn("sauvage", slugs)

    def test_search_is_case_insensitive_lowercase(self):
        response = self.client.get(LIST_URL, {"search": "chance"})
        self.assertIn("chance", self._slugs(response))

    def test_search_is_case_insensitive_uppercase(self):
        response = self.client.get(LIST_URL, {"search": "CHANCE"})
        self.assertIn("chance", self._slugs(response))

    def test_search_partial_match(self):
        response = self.client.get(LIST_URL, {"search": "Sau"})
        self.assertIn("sauvage", self._slugs(response))

    def test_search_no_match_returns_empty_results(self):
        response = self.client.get(LIST_URL, {"search": "xyznonexistent"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_search_empty_string_returns_all_active(self):
        response = self.client.get(LIST_URL, {"search": ""})
        self.assertEqual(response.data["count"], 2)

    def test_search_whitespace_only_returns_all_active(self):
        response = self.client.get(LIST_URL, {"search": "   "})
        self.assertEqual(response.data["count"], 2)

    def test_search_single_char_returns_empty(self):
        # Min length is 2; single-char returns none rather than doing expensive icontains join.
        response = self.client.get(LIST_URL, {"search": "C"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_search_two_chars_is_allowed(self):
        response = self.client.get(LIST_URL, {"search": "Ch"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_search_special_characters_does_not_error(self):
        response = self.client.get(LIST_URL, {"search": "'; DROP TABLE--"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_search_does_not_surface_inactive_product(self):
        response = self.client.get(LIST_URL, {"search": "Hidden"})
        self.assertNotIn("hidden", self._slugs(response))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Brand and category filters
# ─────────────────────────────────────────────────────────────────────────────

class ProductBrandCategoryFilterTests(CatalogTestBase):

    def test_filter_by_brand_slug_chanel(self):
        response = self.client.get(LIST_URL, {"brand": "chanel"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertNotIn("sauvage", slugs)

    def test_filter_by_brand_slug_dior(self):
        response = self.client.get(LIST_URL, {"brand": "dior"})
        slugs = self._slugs(response)
        self.assertIn("sauvage", slugs)
        self.assertNotIn("chance", slugs)

    def test_filter_by_nonexistent_brand_returns_empty(self):
        response = self.client.get(LIST_URL, {"brand": "unknown-brand"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_filter_by_category_fragrance(self):
        response = self.client.get(LIST_URL, {"category": "fragrance"})
        self.assertEqual(response.data["count"], 2)

    def test_filter_by_category_with_no_products_returns_empty(self):
        response = self.client.get(LIST_URL, {"category": "bodycare"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_brand_and_category_combined(self):
        response = self.client.get(LIST_URL, {"brand": "chanel", "category": "fragrance"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertNotIn("sauvage", slugs)

    def test_filter_brand_and_wrong_category_returns_empty(self):
        response = self.client.get(LIST_URL, {"brand": "chanel", "category": "bodycare"})
        self.assertEqual(response.data["count"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Edition attribute filters
# ─────────────────────────────────────────────────────────────────────────────

class ProductEditionFilterTests(CatalogTestBase):

    def test_filter_gender_men(self):
        response = self.client.get(LIST_URL, {"gender": "men"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)  # has men edition
        self.assertIn("sauvage", slugs)  # has men edition

    def test_filter_gender_women(self):
        response = self.client.get(LIST_URL, {"gender": "women"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)   # has women edition
        self.assertNotIn("sauvage", slugs)  # no women edition

    def test_filter_gender_unisex_returns_empty(self):
        # No active unisex editions in fixture (inactive one is excluded)
        response = self.client.get(LIST_URL, {"gender": "unisex"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_concentration_edp(self):
        response = self.client.get(LIST_URL, {"concentration": "edp"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertIn("sauvage", slugs)

    def test_filter_concentration_edt(self):
        response = self.client.get(LIST_URL, {"concentration": "edt"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)   # has edt edition (pour femme)
        self.assertNotIn("sauvage", slugs)  # no edt edition

    def test_filter_concentration_edc_returns_empty(self):
        response = self.client.get(LIST_URL, {"concentration": "edc"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_is_best_seller_true(self):
        response = self.client.get(LIST_URL, {"is_best_seller": "true"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_filter_is_new_arrival_true(self):
        response = self.client.get(LIST_URL, {"is_new_arrival": "true"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_is_best_seller_false_does_not_filter(self):
        # "false" is valid but applies no filter — all active products returned.
        response = self.client.get(LIST_URL, {"is_best_seller": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_is_new_arrival_false_does_not_filter(self):
        response = self.client.get(LIST_URL, {"is_new_arrival": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_gender_invalid_value_returns_400(self):
        response = self.client.get(LIST_URL, {"gender": "attack"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gender", response.data)

    def test_concentration_invalid_value_returns_400(self):
        response = self.client.get(LIST_URL, {"concentration": "parfum"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("concentration", response.data)

    def test_is_best_seller_non_boolean_returns_400(self):
        response = self.client.get(LIST_URL, {"is_best_seller": "maybe"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_best_seller", response.data)

    def test_is_new_arrival_non_boolean_returns_400(self):
        response = self.client.get(LIST_URL, {"is_new_arrival": "yes"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_new_arrival", response.data)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Cross-edition contamination guard
# ─────────────────────────────────────────────────────────────────────────────

class ProductCrossEditionTests(CatalogTestBase):
    """
    Verify that combining multiple edition filters only returns products where
    a SINGLE edition satisfies ALL constraints simultaneously.

    Fixture:
      chance  → Edition A (men/edp) + Edition B (women/edt)
      sauvage → Edition A (men/edp)
    """

    def test_gender_men_and_concentration_edp_matches_correct_products(self):
        # Both chance and sauvage have a men/edp edition.
        response = self.client.get(LIST_URL, {"gender": "men", "concentration": "edp"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertIn("sauvage", slugs)

    def test_gender_men_and_concentration_edt_returns_empty(self):
        # chance has men/edp and women/edt — but NO single edition is men AND edt.
        # Without the fix this would have incorrectly returned chance.
        response = self.client.get(LIST_URL, {"gender": "men", "concentration": "edt"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)

    def test_gender_women_and_concentration_edp_returns_empty(self):
        # chance has women/edt — no single edition is women AND edp.
        response = self.client.get(LIST_URL, {"gender": "women", "concentration": "edp"})
        self.assertEqual(response.data["count"], 0)

    def test_gender_women_and_concentration_edt_returns_chance_only(self):
        response = self.client.get(LIST_URL, {"gender": "women", "concentration": "edt"})
        # Actually this is empty because women/edp returns 0 — but women/edt should return chance.
        # Let's be precise: chance-pour-femme is women/edt → chance matches.
        response = self.client.get(LIST_URL, {"gender": "women", "concentration": "edt"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_best_seller_and_gender_men_uses_same_edition(self):
        # chance-pour-homme is men AND is_best_seller → chance matches.
        # If cross-edition contamination existed, chance-pour-femme (women) + is_best_seller
        # from pour-homme could have given a false positive — this test confirms correctness.
        response = self.client.get(LIST_URL, {"gender": "men", "is_best_seller": "true"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_is_best_seller_and_gender_women_returns_empty(self):
        # chance-pour-femme is women/new_arrival (not best_seller).
        # chance-pour-homme is best_seller but men, not women.
        # No single edition is women AND is_best_seller.
        response = self.client.get(LIST_URL, {"gender": "women", "is_best_seller": "true"})
        self.assertEqual(response.data["count"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Note filter (?note=)
# ─────────────────────────────────────────────────────────────────────────────

class ProductNoteFilterTests(CatalogTestBase):

    def test_filter_single_note_rose(self):
        # Both chance editions have Rose; sauvage does not.
        response = self.client.get(LIST_URL, {"note": "Rose"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_filter_single_note_oud(self):
        # chance-pour-homme and sauvage-edp both have Oud.
        response = self.client.get(LIST_URL, {"note": "Oud"})
        slugs = self._slugs(response)
        self.assertIn("chance",  slugs)
        self.assertIn("sauvage", slugs)

    def test_filter_single_note_case_insensitive(self):
        response = self.client.get(LIST_URL, {"note": "rose"})
        self.assertIn("chance", self._slugs(response))

    def test_filter_two_notes_and_logic_same_edition(self):
        # chance-pour-homme has Rose AND Oud in the same edition → chance matches.
        # sauvage has Oud but not Rose → sauvage does not match.
        response = self.client.get(LIST_URL, {"note": "Rose,Oud"})
        slugs = self._slugs(response)
        self.assertIn("chance",    slugs)
        self.assertNotIn("sauvage", slugs)

    def test_filter_notes_split_across_editions_does_not_match(self):
        # chance-pour-homme has Oud; chance-pour-femme has Vanilla.
        # No single edition has BOTH Oud and Vanilla.
        response = self.client.get(LIST_URL, {"note": "Oud,Vanilla"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_three_notes_no_edition_has_all(self):
        # No edition in the fixture has Rose, Oud, AND Vanilla simultaneously.
        response = self.client.get(LIST_URL, {"note": "Rose,Oud,Vanilla"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_note_whitespace_around_commas_trimmed(self):
        response = self.client.get(LIST_URL, {"note": " Rose , Oud "})
        self.assertIn("chance", self._slugs(response))

    def test_filter_nonexistent_note_returns_empty(self):
        response = self.client.get(LIST_URL, {"note": "Ambergris"})
        self.assertEqual(response.data["count"], 0)

    def test_filter_exactly_5_notes_is_accepted(self):
        response = self.client.get(LIST_URL, {"note": "Rose,Oud,Musk,Sandalwood,Vanilla"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filter_more_than_5_notes_returns_400(self):
        response = self.client.get(LIST_URL, {"note": "Rose,Oud,Musk,Sandalwood,Vanilla,Cedar"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("note", response.data)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Active / inactive exclusion
# ─────────────────────────────────────────────────────────────────────────────

class ProductActiveFilterTests(CatalogTestBase):

    def test_inactive_product_absent_from_list(self):
        response = self.client.get(LIST_URL)
        self.assertNotIn("hidden", self._slugs(response))

    def test_inactive_edition_absent_from_detail_response(self):
        response = self.client.get(detail_url("chance"))
        edition_slugs = {e["slug"] for e in response.data["editions"]}
        self.assertNotIn("chance-inactive", edition_slugs)

    def test_inactive_variant_absent_from_detail_response(self):
        # Inactive 200 ml variant must not appear under chance-pour-homme.
        response = self.client.get(detail_url("chance"))
        chance_men = self._edition(response, "chance-pour-homme")
        variant_sizes = [v["size_ml"] for v in chance_men["variants"]]
        self.assertNotIn(200, variant_sizes)

    def test_inactive_variant_absent_from_list_response(self):
        # Inactive 200 ml variant must not appear in the list endpoint either.
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        sizes = [v["size_ml"] for v in chance_men["variants"]]
        self.assertNotIn(200, sizes)

    def test_only_active_variants_counted_in_list(self):
        # 3 active (100 ml, 50 ml, 10 ml decant); inactive 200 ml excluded.
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        self.assertEqual(len(chance_men["variants"]), 3)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Ordering
# ─────────────────────────────────────────────────────────────────────────────

class ProductOrderingTests(CatalogTestBase):

    def test_default_ordering_is_name_ascending(self):
        response = self.client.get(LIST_URL)
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names))

    def test_ordering_by_name_descending(self):
        response = self.client.get(LIST_URL, {"ordering": "-name"})
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_ordering_by_brand_name_ascending(self):
        response = self.client.get(LIST_URL, {"ordering": "brand__name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        brand_names = [p["brand"]["name"] for p in response.data["results"]]
        self.assertEqual(brand_names, sorted(brand_names))

    def test_ordering_by_brand_name_descending(self):
        response = self.client.get(LIST_URL, {"ordering": "-brand__name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        brand_names = [p["brand"]["name"] for p in response.data["results"]]
        self.assertEqual(brand_names, sorted(brand_names, reverse=True))

    def test_invalid_ordering_field_falls_back_to_default(self):
        # Unknown field is silently ignored by OrderingFilter; default applied.
        response = self.client.get(LIST_URL, {"ordering": "malicious_field"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [p["name"] for p in response.data["results"]]
        self.assertEqual(names, sorted(names))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Detail endpoint
# ─────────────────────────────────────────────────────────────────────────────

class ProductDetailTests(CatalogTestBase):

    def test_detail_active_product_returns_200(self):
        response = self.client.get(detail_url("chance"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_detail_nonexistent_slug_returns_404(self):
        response = self.client.get(detail_url("nonexistent-xyz"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_inactive_product_returns_404(self):
        response = self.client.get(detail_url("hidden"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_contains_only_active_editions(self):
        response = self.client.get(detail_url("chance"))
        edition_slugs = {e["slug"] for e in response.data["editions"]}
        self.assertIn("chance-pour-homme", edition_slugs)
        self.assertIn("chance-pour-femme", edition_slugs)
        self.assertNotIn("chance-inactive", edition_slugs)

    def test_detail_editions_have_grouped_notes(self):
        response = self.client.get(detail_url("chance"))
        chance_men = self._edition(response, "chance-pour-homme")
        notes = chance_men["notes"]
        self.assertIn("Rose", notes["top"])
        self.assertIn("Oud",        notes["base"])
        self.assertIn("Sandalwood", notes["base"])
        self.assertEqual(notes["heart"], [])

    def test_detail_notes_sorted_alphabetically_within_position(self):
        # base notes for chance-pour-homme: Oud, Sandalwood (O < S)
        response = self.client.get(detail_url("chance"))
        base_notes = self._edition(response, "chance-pour-homme")["notes"]["base"]
        self.assertEqual(base_notes, sorted(base_notes))

    def test_detail_notes_structure_has_top_heart_base_keys(self):
        response = self.client.get(detail_url("chance"))
        notes = self._edition(response, "chance-pour-homme")["notes"]
        for key in ("top", "heart", "base"):
            self.assertIn(key, notes)

    def test_detail_variants_contain_only_active_sizes(self):
        response = self.client.get(detail_url("chance"))
        chance_men = self._edition(response, "chance-pour-homme")
        sizes = {v["size_ml"] for v in chance_men["variants"]}
        self.assertIn(100, sizes)
        self.assertIn(50,  sizes)
        self.assertIn(10,  sizes)
        self.assertNotIn(200, sizes)  # inactive variant

    def test_detail_decant_variant_flagged_correctly(self):
        response = self.client.get(detail_url("chance"))
        chance_men = self._edition(response, "chance-pour-homme")
        decant = next(v for v in chance_men["variants"] if v["size_ml"] == 10)
        self.assertTrue(decant["is_decant"])

    def test_detail_does_not_expose_internal_product_fields(self):
        response = self.client.get(detail_url("chance"))
        for field in ("id", "is_active", "created_at", "updated_at"):
            self.assertNotIn(field, response.data)

    def test_detail_does_not_expose_internal_variant_fields(self):
        response = self.client.get(detail_url("chance"))
        variant = self._edition(response, "chance-pour-homme")["variants"][0]
        for field in ("id", "is_active", "created_at", "updated_at", "edition"):
            self.assertNotIn(field, variant)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Serializer field contract & computed values
# ─────────────────────────────────────────────────────────────────────────────

class ProductSerializerFieldTests(CatalogTestBase):

    # ── List serializer ───────────────────────────────────────────────────────

    def test_list_edition_includes_variants_array(self):
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        self.assertIn("variants", chance_men)
        self.assertIsInstance(chance_men["variants"], list)

    def test_list_edition_variant_objects_have_correct_fields(self):
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        variant = chance_men["variants"][0]
        for field in ("public_id", "size_ml", "is_decant", "mrp", "selling_price"):
            self.assertIn(field, variant)

    def test_list_edition_variant_exposes_mrp(self):
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        for variant in chance_men["variants"]:
            self.assertIn("mrp", variant)

    def test_list_edition_all_active_variants_present(self):
        # 3 active variants: 100 ml, 50 ml, 10 ml decant
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        sizes = {v["size_ml"] for v in chance_men["variants"]}
        self.assertEqual(sizes, {100, 50, 10})

    def test_list_edition_selling_prices_correct(self):
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        prices = {Decimal(str(v["selling_price"])) for v in chance_men["variants"]}
        self.assertEqual(prices, {Decimal("4500.00"), Decimal("2700.00"), Decimal("500.00")})

    def test_list_edition_does_not_include_notes(self):
        # Notes are only in the detail serializer.
        response = self.client.get(LIST_URL)
        chance = next(p for p in response.data["results"] if p["slug"] == "chance")
        self.assertNotIn("notes", chance["editions"][0])

    def test_list_edition_does_not_expose_scalar_price_fields(self):
        # min_price / max_price / variants_count replaced by the variants array.
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        for field in ("min_price", "max_price", "variants_count"):
            self.assertNotIn(field, chance_men)

    # ── Detail serializer ─────────────────────────────────────────────────────

    def test_detail_edition_includes_notes_and_variants(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertIn("notes",    edition)
        self.assertIn("variants", edition)

    def test_detail_edition_does_not_include_scalar_price_fields(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        for field in ("min_price", "max_price", "variants_count"):
            self.assertNotIn(field, edition)

    def test_variant_exposes_mrp(self):
        # mrp is exposed alongside selling_price so the frontend can show savings.
        for url in (LIST_URL, detail_url("chance")):
            response = self.client.get(url)
            if "results" in response.data:
                editions = next(
                    p for p in response.data["results"] if p["slug"] == "chance"
                )["editions"]
            else:
                editions = response.data["editions"]
            for edition in editions:
                for variant in edition["variants"]:
                    self.assertIn("mrp", variant, msg=f"mrp missing in {url}")

    def test_detail_variant_exposes_required_fields(self):
        response = self.client.get(detail_url("chance"))
        variant = self._edition(response, "chance-pour-homme")["variants"][0]
        for field in ("public_id", "size_ml", "is_decant", "mrp", "selling_price"):
            self.assertIn(field, variant)

    # ── Edition with no variants ──────────────────────────────────────────────

    def test_edition_with_no_variants_returns_empty_array(self):
        empty_edition = ProductEdition.objects.create(
            product=self.product_sauvage, name="No Variants",
            slug="sauvage-no-variants", gender="unisex", concentration="edt",
        )
        response = self.client.get(LIST_URL)
        sauvage = next(p for p in response.data["results"] if p["slug"] == "sauvage")
        empty_ed = next(e for e in sauvage["editions"] if e["slug"] == "sauvage-no-variants")
        self.assertEqual(empty_ed["variants"], [])


def _fake_image(name="test.jpg"):
    """A file with .jpg extension but no real image bytes. Valid for these
    tests because ProductVariantImage is only ever written through the
    admin/ORM, never through a DRF write endpoint — Pillow's "is this a
    real image" validation only runs behind forms.ImageField (ModelForm),
    which none of these tests exercise."""
    return SimpleUploadedFile(name, b"not-real-image-bytes", content_type="image/jpeg")


# ─────────────────────────────────────────────────────────────────────────────
# 11. ProductVariantImage — model invariants
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProductVariantImageModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        brand = Brand.objects.create(name="TestBrand", slug="testbrand")
        category = Category.objects.create(name="TestCat", slug="testcat")
        product = Product.objects.create(name="TestProduct", slug="testproduct", brand=brand, category=category)
        edition = ProductEdition.objects.create(product=product, gender="unisex", concentration="edp")
        cls.variant = ProductVariant.objects.create(
            edition=edition, size_ml=100, mrp=Decimal("1000.00"),
            selling_price=Decimal("900.00"), sku="TEST-IMG-VARIANT",
        )

    def test_max_images_per_variant_enforced(self):
        for i in range(ProductVariantImage.MAX_IMAGES_PER_VARIANT):
            ProductVariantImage.objects.create(
                variant=self.variant, image=_fake_image(f"g{i}.jpg"), role=ProductVariantImage.ROLE_GALLERY,
            )
        eleventh = ProductVariantImage(
            variant=self.variant, image=_fake_image("g11.jpg"), role=ProductVariantImage.ROLE_GALLERY,
        )
        with self.assertRaises(ValidationError):
            eleventh.full_clean()

    def test_editing_an_existing_image_does_not_count_against_its_own_slot(self):
        images = [
            ProductVariantImage.objects.create(
                variant=self.variant, image=_fake_image(f"g{i}.jpg"), role=ProductVariantImage.ROLE_GALLERY,
            )
            for i in range(ProductVariantImage.MAX_IMAGES_PER_VARIANT)
        ]
        # At exactly 10, re-saving one of the existing 10 (e.g. changing its
        # alt_text) must not be rejected as "would exceed the cap".
        images[0].alt_text = "Updated alt text"
        images[0].full_clean()  # must not raise

    def test_one_primary_per_variant_enforced(self):
        ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("p1.jpg"), role=ProductVariantImage.ROLE_PRIMARY,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductVariantImage.objects.create(
                    variant=self.variant, image=_fake_image("p2.jpg"), role=ProductVariantImage.ROLE_PRIMARY,
                )

    def test_one_secondary_per_variant_enforced(self):
        ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("s1.jpg"), role=ProductVariantImage.ROLE_SECONDARY,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductVariantImage.objects.create(
                    variant=self.variant, image=_fake_image("s2.jpg"), role=ProductVariantImage.ROLE_SECONDARY,
                )

    def test_gallery_role_allows_multiple(self):
        for i in range(3):
            ProductVariantImage.objects.create(
                variant=self.variant, image=_fake_image(f"g{i}.jpg"), role=ProductVariantImage.ROLE_GALLERY,
            )
        self.assertEqual(
            ProductVariantImage.objects.filter(variant=self.variant, role=ProductVariantImage.ROLE_GALLERY).count(),
            3,
        )

    def test_images_ordered_by_sort_order(self):
        # Keyed off alt_text rather than the stored filename — storage
        # auto-suffixes on a filename collision with a leftover file from a
        # prior test run, which would make a filename-based assertion flaky.
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image("c.jpg"), sort_order=2, alt_text="third")
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image("a.jpg"), sort_order=0, alt_text="first")
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image("b.jpg"), sort_order=1, alt_text="second")
        alt_texts = [img.alt_text for img in self.variant.images.all()]
        self.assertEqual(alt_texts, ["first", "second", "third"])

    def test_deleting_primary_does_not_promote_another_image(self):
        primary = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("p.jpg"), role=ProductVariantImage.ROLE_PRIMARY,
        )
        ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("g.jpg"), role=ProductVariantImage.ROLE_GALLERY,
        )
        primary.delete()
        self.assertFalse(
            ProductVariantImage.objects.filter(variant=self.variant, role=ProductVariantImage.ROLE_PRIMARY).exists()
        )
        # The gallery image must still be gallery, not silently promoted.
        remaining = ProductVariantImage.objects.get(variant=self.variant)
        self.assertEqual(remaining.role, ProductVariantImage.ROLE_GALLERY)

    def test_alt_text_persisted(self):
        img = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("a.jpg"), alt_text="Rasasi Hawas 100ml bottle front view",
        )
        img.refresh_from_db()
        self.assertEqual(img.alt_text, "Rasasi Hawas 100ml bottle front view")


# ─────────────────────────────────────────────────────────────────────────────
# 12. ProductVariantImage — admin wiring
# ─────────────────────────────────────────────────────────────────────────────

class ProductVariantImageAdminTests(TestCase):
    def test_variant_admin_has_no_image_inline(self):
        # Gallery images are written only through apps.catalog.image_services
        # (Manage Images page); the old inline write path is gone.
        from apps.catalog.admin import ProductVariantAdmin

        self.assertEqual(list(ProductVariantAdmin.inlines), [])


class ProductVariantAdminListTests(CatalogTestBase):
    def test_list_shows_real_sku_not_database_id(self):
        from apps.catalog.admin import ProductVariantAdmin

        variant = self.edition_sauvage.variants.get()
        model_admin = ProductVariantAdmin(ProductVariant, django_admin.site)
        title, subtitle = model_admin.display_name_formatted(variant)
        self.assertEqual(subtitle, f"SKU: {variant.sku}")
        self.assertNotEqual(variant.sku, str(variant.pk))


# ─────────────────────────────────────────────────────────────────────────────
# 13. ProductVariantImage — API representation
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProductVariantImageAPITests(CatalogTestBase):
    def setUp(self):
        super().setUp()
        self.variant = self.edition_sauvage.variants.get()
        self.primary = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("primary.jpg"),
            role=ProductVariantImage.ROLE_PRIMARY, alt_text="Sauvage EDP 100ml front", sort_order=0,
        )
        self.secondary = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("secondary.jpg"),
            role=ProductVariantImage.ROLE_SECONDARY, sort_order=1,
        )
        self.gallery = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("gallery.jpg"),
            role=ProductVariantImage.ROLE_GALLERY, sort_order=2,
        )

    def _variant_payload(self):
        response = self.client.get(detail_url("sauvage"))
        return self._edition(response, "sauvage-edp")["variants"][0]

    def test_images_array_includes_all_roles(self):
        variant = self._variant_payload()
        self.assertEqual(len(variant["images"]), 3)

    def test_primary_image_field_matches_primary_role(self):
        variant = self._variant_payload()
        self.assertIsNotNone(variant["primary_image"])
        self.assertEqual(variant["primary_image"]["role"], "primary")
        self.assertEqual(variant["primary_image"]["alt_text"], "Sauvage EDP 100ml front")

    def test_primary_image_is_none_when_no_primary_set(self):
        self.primary.delete()
        variant = self._variant_payload()
        self.assertIsNone(variant["primary_image"])
        # The remaining images are still returned, just not as primary_image.
        self.assertEqual(len(variant["images"]), 2)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Catalogue SEO — ProductEdition metadata
# ─────────────────────────────────────────────────────────────────────────────

class CatalogSEOTests(CatalogTestBase):
    def test_seo_title_falls_back_to_display_name_when_unset(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["title"], self.edition_chance_men.display_name())

    def test_seo_title_uses_explicit_override(self):
        self.edition_chance_men.seo_title = "Buy Chance Pour Homme Online | Smerfume"
        self.edition_chance_men.save(update_fields=["seo_title"])
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["title"], "Buy Chance Pour Homme Online | Smerfume")

    def test_meta_description_is_null_when_unset(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertIsNone(edition["seo"]["meta_description"])

    def test_meta_description_uses_explicit_value(self):
        self.edition_chance_men.meta_description = "A woody floral for men."
        self.edition_chance_men.save(update_fields=["meta_description"])
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["meta_description"], "A woody floral for men.")

    def test_og_title_falls_back_to_seo_title_then_display_name(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["og_title"], self.edition_chance_men.display_name())

        self.edition_chance_men.seo_title = "SEO Title"
        self.edition_chance_men.save(update_fields=["seo_title"])
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["og_title"], "SEO Title")

        self.edition_chance_men.og_title = "OG Title"
        self.edition_chance_men.save(update_fields=["og_title"])
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertEqual(edition["seo"]["og_title"], "OG Title")

    def test_is_indexable_defaults_true(self):
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertTrue(edition["seo"]["is_indexable"])

    def test_is_indexable_false_is_respected(self):
        self.edition_chance_men.is_indexable = False
        self.edition_chance_men.save(update_fields=["is_indexable"])
        response = self.client.get(detail_url("chance"))
        edition = self._edition(response, "chance-pour-homme")
        self.assertFalse(edition["seo"]["is_indexable"])

    def test_list_serializer_does_not_include_seo_block(self):
        # SEO metadata is only meaningful for the single canonical page being
        # rendered — keeping it off the list/search payload keeps that
        # response lean, per the search-vs-SEO separation.
        response = self.client.get(LIST_URL)
        chance_men = self._list_edition(response, "chance", "chance-pour-homme")
        self.assertNotIn("seo", chance_men)

    def test_duplicate_edition_slug_within_same_product_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductEdition.objects.create(
                    product=self.product_chance, name="Duplicate",
                    slug="chance-pour-homme",  # already used by edition_chance_men
                    gender="unisex", concentration="edp",
                )

    def test_same_slug_allowed_across_different_products(self):
        # Scoped uniqueness: "original" under Chance does not collide with
        # "original" under Sauvage.
        ProductEdition.objects.create(
            product=self.product_chance, name="Original",
            slug="original", gender="unisex", concentration="edp",
        )
        edition = ProductEdition.objects.create(
            product=self.product_sauvage, name="Original",
            slug="original", gender="unisex", concentration="edp",
        )
        self.assertIsNotNone(edition.pk)

    def test_null_slugs_do_not_collide(self):
        # Single-edition products intentionally leave slug null — multiple
        # of those under the same product must not violate the constraint.
        ProductEdition.objects.create(product=self.product_sauvage, gender="unisex", concentration="edt")
        second = ProductEdition.objects.create(product=self.product_sauvage, gender="unisex", concentration="extrait")
        self.assertIsNotNone(second.pk)


# ─────────────────────────────────────────────────────────────────────────────
# 15. Catalogue availability & SKU exposure
# ─────────────────────────────────────────────────────────────────────────────

class CatalogAvailabilityTests(CatalogTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A default warehouse is already seeded by inventory's
        # 0003_seed_default_warehouse data migration and persists across
        # TestCase tests — reuse it rather than creating a second
        # is_default=True row (unique_default_warehouse would reject that).
        cls.warehouse = Warehouse.objects.filter(is_default=True).first() or Warehouse.objects.create(
            name="Test Warehouse", is_default=True,
        )
        cls.sauvage_variant = cls.edition_sauvage.variants.get()

    def test_variant_with_retail_stock_is_available(self):
        InventoryStock.objects.create(
            variant=self.sauvage_variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )
        response = self.client.get(detail_url("sauvage"))
        variant = self._edition(response, "sauvage-edp")["variants"][0]
        self.assertTrue(variant["is_available"])

    def test_variant_without_stock_is_not_available(self):
        response = self.client.get(detail_url("sauvage"))
        variant = self._edition(response, "sauvage-edp")["variants"][0]
        self.assertFalse(variant["is_available"])

    def test_fully_reserved_stock_is_not_available(self):
        InventoryStock.objects.create(
            variant=self.sauvage_variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            quantity=Decimal("5"), quantity_reserved=Decimal("5"),
        )
        response = self.client.get(detail_url("sauvage"))
        variant = self._edition(response, "sauvage-edp")["variants"][0]
        self.assertFalse(variant["is_available"])

    def test_tester_stock_does_not_count_as_available(self):
        # Only retail-type stock is purchasable/reservable — matches
        # apps.inventory.services.reservation.
        InventoryStock.objects.create(
            variant=self.sauvage_variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_TESTER, quantity=Decimal("5"),
        )
        response = self.client.get(detail_url("sauvage"))
        variant = self._edition(response, "sauvage-edp")["variants"][0]
        self.assertFalse(variant["is_available"])

    def test_variant_sku_exposed_in_api(self):
        response = self.client.get(detail_url("sauvage"))
        variant = self._edition(response, "sauvage-edp")["variants"][0]
        self.assertEqual(variant["sku"], "TEST-SAUVAGE-100ML")


# ─────────────────────────────────────────────────────────────────────────────
# 16. Booked incoming availability (P2B)
# ─────────────────────────────────────────────────────────────────────────────

class CatalogBookedIncomingAvailabilityTests(CatalogTestBase):
    """is_available goes through the sellable-quantity selector while
    BOOKED_INCOMING_SALES_ENABLED is on, for the default (online-sale)
    warehouse only. Response shape never changes."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.contrib.auth import get_user_model
        from apps.inventory.models import Supplier

        cls.warehouse = Warehouse.objects.get(is_default=True)
        cls.other_warehouse = Warehouse.objects.create(name="Catalog Other Warehouse")
        cls.sauvage_variant = cls.edition_sauvage.variants.get()
        cls.buyer = get_user_model().objects.create_user(username="catalog-buyer", password="x")
        cls.supplier = Supplier.objects.create(name="Catalog Vendor")

    def _book(self, variant, confirmed=3, warehouse=None):
        from apps.purchases.models import PurchaseOrder, PurchaseOrderLine

        po = PurchaseOrder.objects.create(
            supplier=self.supplier, warehouse=warehouse or self.warehouse,
            created_by=self.buyer, status=PurchaseOrder.STATUS_ISSUED,
        )
        return PurchaseOrderLine.objects.create(
            purchase_order=po, variant=variant, quantity_ordered=confirmed,
            unit_price=Decimal("100.00"), confirmed_booked_quantity=confirmed,
        )

    def _sauvage_variant_data(self):
        response = self.client.get(detail_url("sauvage"))
        return self._edition(response, "sauvage-edp")["variants"][0]

    def _list_variant_data(self):
        response = self.client.get(LIST_URL)
        product = next(p for p in response.data["results"] if p["slug"] == "sauvage")
        edition = next(e for e in product["editions"] if e["slug"] == "sauvage-edp")
        return edition["variants"][0]

    def test_flag_off_ignores_booked_stock_without_purchase_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._book(self.sauvage_variant)
        with CaptureQueriesContext(connection) as ctx:
            data = self._sauvage_variant_data()
            self.client.get(LIST_URL)
        self.assertFalse(data["is_available"])
        self.assertFalse([q for q in ctx.captured_queries if "purchases_" in q["sql"]])

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_flag_on_booked_stock_makes_variant_available(self):
        self._book(self.sauvage_variant)
        self.assertTrue(self._sauvage_variant_data()["is_available"])
        self.assertTrue(self._list_variant_data()["is_available"])

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_flag_on_physical_stock_still_available(self):
        InventoryStock.objects.create(
            variant=self.sauvage_variant, warehouse=self.warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("1"),
        )
        self.assertTrue(self._sauvage_variant_data()["is_available"])

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_flag_on_uses_only_the_online_sale_warehouse(self):
        InventoryStock.objects.create(
            variant=self.sauvage_variant, warehouse=self.other_warehouse,
            stock_type=InventoryStock.STOCK_TYPE_RETAIL, quantity=Decimal("5"),
        )
        self._book(self.sauvage_variant, warehouse=self.other_warehouse)
        self.assertFalse(self._sauvage_variant_data()["is_available"])

    def test_response_shape_unchanged_by_flag(self):
        self._book(self.sauvage_variant)
        off = self._sauvage_variant_data()
        with override_settings(BOOKED_INCOMING_SALES_ENABLED=True):
            on = self._sauvage_variant_data()
        self.assertEqual(set(off), set(on))
        self.assertEqual((off["is_available"], on["is_available"]), (False, True))

    @override_settings(BOOKED_INCOMING_SALES_ENABLED=True)
    def test_flag_on_list_query_count_independent_of_variant_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._book(self.sauvage_variant)
        with CaptureQueriesContext(connection) as before:
            self.client.get(LIST_URL)
        for variant in ProductVariant.objects.exclude(pk=self.sauvage_variant.pk):
            self._book(variant)
            InventoryStock.objects.get_or_create(
                variant=variant, warehouse=self.warehouse,
                stock_type=InventoryStock.STOCK_TYPE_RETAIL, defaults={"quantity": Decimal("2")},
            )
        with CaptureQueriesContext(connection) as after:
            self.client.get(LIST_URL)
        self.assertEqual(len(before.captured_queries), len(after.captured_queries))


# ── Admin ERP Phase 2: changelist query counts ───────────────────────────────

from apps.core.test_utils import AdminQueryCountMixin  # noqa: E402


class CatalogAdminQueryCountTests(AdminQueryCountMixin, TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser(
            username="catquery", email="catquery@example.com", password="testpass123",
        ))
        self.category = Category.objects.create(name="QCat", slug="qcat")

    def _product(self, i):
        brand = Brand.objects.create(name=f"QBrand{i}", slug=f"qbrand{i}")
        return Product.objects.create(
            name=f"QProduct{i}", slug=f"qproduct{i}", brand=brand, category=self.category,
        )

    def test_product_changelist(self):
        self.assertChangelistQueriesFlat("/admin/catalog/product/", self._product)

    def test_edition_changelist(self):
        def make(i):
            ProductEdition.objects.create(
                product=self._product(i), name="EDP", slug=f"q-edp-{i}",
                concentration="edp", gender="unisex",
            )
        self.assertChangelistQueriesFlat("/admin/catalog/productedition/", make)


# ─────────────────────────────────────────────────────────────────────────────
# 15. Variant image gallery — image validation and normalisation
# ─────────────────────────────────────────────────────────────────────────────

import io  # noqa: E402
import hashlib  # noqa: E402
from unittest import mock  # noqa: E402

from PIL import Image  # noqa: E402

from . import image_processing  # noqa: E402
from .image_processing import ImageRejected, process_image  # noqa: E402


def _real_image(size=(1600, 1200), fmt="JPEG", mode="RGB", color=(200, 30, 30), name=None, exif=None, **save_kwargs):
    """A genuine encoded image generated with Pillow."""
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    if exif is not None:
        save_kwargs["exif"] = exif.tobytes()
    image.save(buffer, fmt, **save_kwargs)
    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}[fmt]
    return SimpleUploadedFile(name or f"photo.{extension}", buffer.getvalue(), content_type=f"image/{extension}")


def _decode(processed):
    return Image.open(io.BytesIO(processed.content))


class ImageProcessingTests(TestCase):
    def test_jpeg_png_webp_accepted_and_stored_as_webp(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                processed = process_image(_real_image(fmt=fmt))
                self.assertEqual(processed.extension, "webp")
                self.assertEqual(_decode(processed).format, "WEBP")
                self.assertEqual((processed.width, processed.height), (1600, 1200))
                self.assertEqual(processed.sha256, hashlib.sha256(processed.content).hexdigest())

    def test_resized_to_2000_longest_side_keeping_aspect(self):
        processed = process_image(_real_image(size=(3000, 1500)))
        self.assertEqual((processed.width, processed.height), (2000, 1000))
        self.assertEqual(_decode(processed).size, (2000, 1000))

    def test_never_upscaled(self):
        processed = process_image(_real_image(size=(1300, 1250)))
        self.assertEqual((processed.width, processed.height), (1300, 1250))

    def test_shortest_side_below_600_rejected(self):
        with self.assertRaisesMessage(ImageRejected, "600"):
            process_image(_real_image(size=(599, 1200)))

    def test_low_resolution_warns_but_saves(self):
        processed = process_image(_real_image(size=(900, 900)))
        self.assertEqual(len(processed.warnings), 1)
        self.assertIn("1200", processed.warnings[0])
        self.assertEqual(process_image(_real_image(size=(1200, 1300))).warnings, ())

    def test_oversized_input_rejected_before_decoding(self):
        with self.assertRaisesMessage(ImageRejected, "6000"):
            process_image(_real_image(size=(6001, 700)))

    def test_upload_over_15_mb_rejected(self):
        big = SimpleUploadedFile("big.jpg", b"\0" * (15 * 1024 * 1024 + 1), content_type="image/jpeg")
        with self.assertRaisesMessage(ImageRejected, "15 MB"):
            process_image(big)

    def test_gif_svg_heic_and_garbage_rejected(self):
        svg = SimpleUploadedFile("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', content_type="image/svg+xml")
        garbage = SimpleUploadedFile("photo.jpg", b"not-an-image", content_type="image/jpeg")
        for upload in (_real_image(fmt="GIF", mode="P", color=1), svg, garbage):
            with self.subTest(name=upload.name):
                with self.assertRaises(ImageRejected):
                    process_image(upload)

    def test_animated_webp_rejected(self):
        frames = [Image.new("RGB", (800, 800), color) for color in ((255, 0, 0), (0, 255, 0))]
        buffer = io.BytesIO()
        frames[0].save(buffer, "WEBP", save_all=True, append_images=frames[1:], duration=100)
        upload = SimpleUploadedFile("anim.webp", buffer.getvalue(), content_type="image/webp")
        with self.assertRaisesMessage(ImageRejected, "nimated"):
            process_image(upload)

    def test_truncated_image_rejected(self):
        whole = _real_image().read()
        upload = SimpleUploadedFile("cut.jpg", whole[: len(whole) // 2], content_type="image/jpeg")
        with self.assertRaises(ImageRejected):
            process_image(upload)

    def test_exif_orientation_applied_and_metadata_removed(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise when displayed
        exif[0x010F] = "SecretCam"
        processed = process_image(_real_image(size=(1600, 1000), exif=exif))
        self.assertEqual((processed.width, processed.height), (1000, 1600))
        decoded = _decode(processed)
        self.assertEqual(dict(decoded.getexif()), {})
        self.assertNotIn(b"SecretCam", processed.content)

    def test_cmyk_converted_to_rgb(self):
        processed = process_image(_real_image(mode="CMYK", color=(0, 200, 200, 0)))
        self.assertEqual(_decode(processed).mode, "RGB")

    def test_transparency_kept(self):
        processed = process_image(_real_image(fmt="PNG", mode="RGBA", color=(10, 20, 30, 0)))
        self.assertEqual(_decode(processed).mode, "RGBA")
        opaque = process_image(_real_image(fmt="PNG", mode="RGBA", color=(10, 20, 30, 255)))
        self.assertEqual(_decode(opaque).mode, "RGB")

    def test_same_input_gives_same_hash(self):
        self.assertEqual(process_image(_real_image()).sha256, process_image(_real_image()).sha256)

    def test_fallback_formats_without_webp(self):
        with mock.patch.object(image_processing.features, "check", return_value=False):
            transparent = process_image(_real_image(fmt="PNG", mode="RGBA", color=(1, 2, 3, 0)))
            opaque = process_image(_real_image())
        self.assertEqual((transparent.extension, _decode(transparent).format), ("png", "PNG"))
        self.assertEqual((opaque.extension, _decode(opaque).format), ("jpg", "JPEG"))

    def test_final_file_over_limit_rejected(self):
        with mock.patch.object(image_processing, "MAX_OUTPUT_BYTES", 100):
            with self.assertRaisesMessage(ImageRejected, "5 MB"):
                process_image(_real_image())


# ─────────────────────────────────────────────────────────────────────────────
# 16. Variant image gallery — metadata fields, storage names, backfill
# ─────────────────────────────────────────────────────────────────────────────

from io import StringIO  # noqa: E402

from django.core.management import call_command  # noqa: E402

from .models import variant_image_upload_to  # noqa: E402


def _gallery_variant(sku="GALLERY-SKU"):
    brand, _ = Brand.objects.get_or_create(name="GalleryBrand", slug="gallerybrand")
    category, _ = Category.objects.get_or_create(name="GalleryCat", slug="gallerycat")
    product, _ = Product.objects.get_or_create(
        name="GalleryProduct", slug="galleryproduct", brand=brand, category=category,
    )
    edition, _ = ProductEdition.objects.get_or_create(product=product, gender="unisex", concentration="edp")
    return ProductVariant.objects.create(
        edition=edition, size_ml=100, mrp=Decimal("1000.00"), selling_price=Decimal("900.00"), sku=sku,
    )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantImageMetadataTests(TestCase):
    def setUp(self):
        self.variant = _gallery_variant()

    def test_limit_is_20(self):
        self.assertEqual(ProductVariantImage.MAX_IMAGES_PER_VARIANT, 20)

    def test_upload_name_is_unique_per_variant_folder(self):
        image = ProductVariantImage(variant=self.variant)
        first = variant_image_upload_to(image, "anything.webp")
        second = variant_image_upload_to(image, "anything.webp")
        self.assertTrue(first.startswith(f"catalog/variant_images/{self.variant.public_id}/"))
        self.assertTrue(first.endswith(".webp"))
        self.assertNotEqual(first, second)
        self.assertNotIn("anything", first)

    def test_dimensions_and_hash_default_empty(self):
        image = ProductVariantImage.objects.create(variant=self.variant, image=_fake_image())
        self.assertEqual((image.width, image.height, image.content_hash), (None, None, ""))

    def test_hash_unique_per_variant_when_set(self):
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image(), content_hash="a" * 64)
        ProductVariantImage.objects.create(
            variant=_gallery_variant("OTHER-SKU"), image=_fake_image(), content_hash="a" * 64,
        )
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image())
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductVariantImage.objects.create(variant=self.variant, image=_fake_image(), content_hash="a" * 64)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class BackfillVariantImageDimensionsTests(TestCase):
    def setUp(self):
        self.variant = _gallery_variant()

    def _run(self):
        out = StringIO()
        call_command("backfill_variant_image_dimensions", stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_fills_missing_dimensions_and_is_rerunnable(self):
        image = ProductVariantImage.objects.create(variant=self.variant, image=_real_image(size=(1500, 900)))
        output = self._run()
        image.refresh_from_db()
        self.assertEqual((image.width, image.height), (1500, 900))
        self.assertIn("1 updated", output)
        self.assertIn("0 updated", self._run())

    def test_broken_or_missing_files_reported_not_fatal(self):
        broken = ProductVariantImage.objects.create(variant=self.variant, image=_fake_image("broken.jpg"))
        missing = ProductVariantImage.objects.create(variant=self.variant, image=_real_image())
        missing.image.storage.delete(missing.image.name)
        output = self._run()
        self.assertIn("2 failed", output)
        broken.refresh_from_db()
        self.assertIsNone(broken.width)


# ─────────────────────────────────────────────────────────────────────────────
# 17. Variant image gallery — services
# ─────────────────────────────────────────────────────────────────────────────

import threading  # noqa: E402

from django.contrib.admin.models import LogEntry  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import TransactionTestCase  # noqa: E402

from . import image_services  # noqa: E402
from .image_services import VariantImageError  # noqa: E402

PRIMARY = ProductVariantImage.ROLE_PRIMARY
SECONDARY = ProductVariantImage.ROLE_SECONDARY
GALLERY = ProductVariantImage.ROLE_GALLERY


def _photo(shade, size=(1400, 1400), fmt="JPEG"):
    return _real_image(size=size, fmt=fmt, color=(shade, 40, 90))


def _staff(username="imagestaff"):
    return get_user_model().objects.create_user(
        username=username, email=f"{username}@example.com", password="testpass123", is_staff=True,
    )


def _image_storage():
    return ProductVariantImage._meta.get_field("image").storage


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantImageServiceTests(TestCase):
    def setUp(self):
        self.variant = _gallery_variant()
        self.user = _staff()

    def _add(self, *files):
        return image_services.add_variant_images(self.variant, list(files), self.user)

    def test_add_creates_gallery_images_with_metadata(self):
        result = self._add(_photo(10), _photo(20, size=(3000, 1500)))
        self.assertEqual(result.rejected, [])
        first, second = result.created
        self.assertEqual([first.role, second.role], [GALLERY, GALLERY])
        self.assertEqual([first.sort_order, second.sort_order], [0, 1])
        self.assertEqual((second.width, second.height), (2000, 1000))
        self.assertEqual(len(first.content_hash), 64)
        self.assertTrue(first.image.name.startswith(f"catalog/variant_images/{self.variant.public_id}/"))
        self.assertTrue(first.image.name.endswith(".webp"))
        self.assertTrue(_image_storage().exists(first.image.name))
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.variant.pk), user=self.user).exists())

    def test_bad_files_rejected_others_saved(self):
        garbage = SimpleUploadedFile("broken.jpg", b"nope", content_type="image/jpeg")
        result = self._add(_photo(10), garbage)
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.rejected[0][0], "broken.jpg")

    def test_low_resolution_warning_returned(self):
        result = self._add(_photo(10, size=(800, 800)))
        self.assertEqual(len(result.created), 1)
        self.assertEqual(result.warnings[0][0], "photo.jpg")

    def test_duplicates_rejected_in_batch_and_against_gallery(self):
        self._add(_photo(10))
        result = self._add(_photo(10), _photo(30), _photo(30))
        self.assertEqual(len(result.created), 1)
        self.assertEqual(len(result.rejected), 2)
        self.assertIn("already", result.rejected[0][1])

    def test_new_images_appended_after_existing(self):
        ProductVariantImage.objects.create(variant=self.variant, image=_fake_image(), sort_order=7)
        created = self._add(_photo(10)).created[0]
        self.assertEqual(created.sort_order, 8)

    def test_image_limit_enforced(self):
        for i in range(ProductVariantImage.MAX_IMAGES_PER_VARIANT - 1):
            ProductVariantImage.objects.create(variant=self.variant, image=_fake_image(f"g{i}.jpg"))
        result = self._add(_photo(10), _photo(20))
        self.assertEqual(len(result.created), 1)
        self.assertIn("20", result.rejected[0][1])

    def test_too_many_files_in_one_request(self):
        with self.assertRaisesMessage(VariantImageError, "20"):
            self._add(*[_photo(i) for i in range(21)])

    def test_role_change_demotes_current_holder_first(self):
        a, b = self._add(_photo(10), _photo(20)).created
        image_services.set_variant_image_role(a, PRIMARY, self.user)
        image_services.set_variant_image_role(b, PRIMARY, self.user)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.role, b.role), (GALLERY, PRIMARY))
        image_services.set_variant_image_role(a, SECONDARY, self.user)
        image_services.set_variant_image_role(b, SECONDARY, self.user)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.role, b.role), (GALLERY, SECONDARY))
        self.assertFalse(self.variant.images.filter(role=PRIMARY).exists())

    def test_set_gallery_and_invalid_role(self):
        image = self._add(_photo(10)).created[0]
        image_services.set_variant_image_role(image, PRIMARY, self.user)
        image_services.set_variant_image_role(image, GALLERY, self.user)
        image.refresh_from_db()
        self.assertEqual(image.role, GALLERY)
        with self.assertRaises(VariantImageError):
            image_services.set_variant_image_role(image, "hero", self.user)

    def test_remove_primary_never_promotes_another(self):
        a, b = self._add(_photo(10), _photo(20)).created
        image_services.set_variant_image_role(a, PRIMARY, self.user)
        name = a.image.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            image_services.remove_variant_image(a, self.user)
        self.assertTrue(_image_storage().exists(name))  # kept until commit
        for callback in callbacks:
            callback()
        self.assertFalse(_image_storage().exists(name))
        self.assertFalse(ProductVariantImage.objects.filter(pk=a.pk).exists())
        b.refresh_from_db()
        self.assertEqual(b.role, GALLERY)

    def test_reorder(self):
        a, b, c = self._add(_photo(10), _photo(20), _photo(30)).created
        image_services.reorder_variant_images(self.variant, [c.public_id, a.public_id, b.public_id], self.user)
        orders = dict(self.variant.images.values_list("pk", "sort_order"))
        self.assertEqual((orders[c.pk], orders[a.pk], orders[b.pk]), (0, 1, 2))
        for bad in ([a.public_id, b.public_id], [a.public_id, a.public_id, b.public_id], ["not-a-uuid"]):
            with self.assertRaises(VariantImageError):
                image_services.reorder_variant_images(self.variant, bad, self.user)

    def test_alt_text(self):
        image = self._add(_photo(10)).created[0]
        image_services.update_variant_image_alt_text(image, "  Front view  ", self.user)
        image.refresh_from_db()
        self.assertEqual(image.alt_text, "Front view")
        with self.assertRaises(VariantImageError):
            image_services.update_variant_image_alt_text(image, "x" * 256, self.user)

    def test_replace_writes_new_file_and_deletes_old_after_commit(self):
        image = self._add(_photo(10)).created[0]
        old_name = image.image.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            image_services.replace_variant_image(image, _photo(99, size=(1800, 1200)), self.user)
        image.refresh_from_db()
        self.assertNotEqual(image.image.name, old_name)
        self.assertEqual((image.width, image.height), (1800, 1200))
        self.assertTrue(_image_storage().exists(old_name))
        for callback in callbacks:
            callback()
        self.assertFalse(_image_storage().exists(old_name))
        self.assertTrue(_image_storage().exists(image.image.name))

    def test_replace_with_duplicate_of_another_image_refused(self):
        a, b = self._add(_photo(10), _photo(20)).created
        with self.assertRaisesMessage(VariantImageError, "already"):
            image_services.replace_variant_image(b, _photo(10), self.user)

    def test_failed_replace_removes_new_file(self):
        image = self._add(_photo(10)).created[0]
        folder = f"catalog/variant_images/{self.variant.public_id}"
        before = set(_image_storage().listdir(folder)[1])
        with mock.patch.object(ProductVariantImage, "save", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                image_services.replace_variant_image(image, _photo(55), self.user)
        self.assertEqual(set(_image_storage().listdir(folder)[1]), before)

    def test_display_order(self):
        a, b, c = self._add(_photo(10), _photo(20), _photo(30)).created
        image_services.set_variant_image_role(c, PRIMARY, self.user)
        image_services.set_variant_image_role(b, SECONDARY, self.user)
        ordered = list(image_services.display_ordered_images(self.variant.images.all()))
        self.assertEqual(ordered, [c, b, a])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantImageRoleConcurrencyTests(TransactionTestCase):
    def test_concurrent_primary_changes_keep_one_primary(self):
        variant = _gallery_variant()
        user = _staff()
        a, b = image_services.add_variant_images(variant, [_photo(10), _photo(20)], user).created
        barrier = threading.Barrier(2)
        errors = []

        def promote(image):
            try:
                barrier.wait()
                image_services.set_variant_image_role(image, PRIMARY, user)
            except Exception as exc:  # reported by the assertion below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=promote, args=(image,)) for image in (a, b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(variant.images.filter(role=PRIMARY).count(), 1)


# ─────────────────────────────────────────────────────────────────────────────
# 18. Variant image gallery — API additions (order unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantImageGalleryAPITests(CatalogTestBase):
    def setUp(self):
        super().setUp()
        self.variant = self.edition_sauvage.variants.get()
        # Gallery first by sort_order on purpose: the API order must stay
        # sort_order, id (the role-first order is gated on a storefront check).
        self.gallery = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("gallery.jpg"), role=GALLERY, sort_order=0,
            width=2000, height=1500,
        )
        self.secondary = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("secondary.jpg"), role=SECONDARY, sort_order=1,
        )
        self.primary = ProductVariantImage.objects.create(
            variant=self.variant, image=_fake_image("primary.jpg"), role=PRIMARY, sort_order=2,
        )

    def _variant_payload(self):
        response = self.client.get(detail_url("sauvage"))
        return self._edition(response, "sauvage-edp")["variants"][0]

    def test_images_order_is_still_sort_order_then_id(self):
        roles = [image["role"] for image in self._variant_payload()["images"]]
        self.assertEqual(roles, [GALLERY, SECONDARY, PRIMARY])

    def test_secondary_image_field(self):
        variant = self._variant_payload()
        self.assertEqual(variant["secondary_image"]["public_id"], str(self.secondary.public_id))
        self.assertEqual(variant["primary_image"]["public_id"], str(self.primary.public_id))
        self.secondary.delete()
        self.assertIsNone(self._variant_payload()["secondary_image"])

    def test_width_height_on_each_image(self):
        images = {image["role"]: image for image in self._variant_payload()["images"]}
        self.assertEqual((images[GALLERY]["width"], images[GALLERY]["height"]), (2000, 1500))
        self.assertIsNone(images[PRIMARY]["width"])

    def test_existing_fields_unchanged(self):
        variant = self._variant_payload()
        for key in ("public_id", "sku", "image", "size_ml", "is_decant", "mrp", "selling_price",
                    "images", "primary_image", "is_available"):
            self.assertIn(key, variant)
        for key in ("public_id", "image", "role", "alt_text", "sort_order"):
            self.assertIn(key, variant["images"][0])


# ─────────────────────────────────────────────────────────────────────────────
# 19. Variant image gallery — Manage Images admin page
# ─────────────────────────────────────────────────────────────────────────────

import json  # noqa: E402

from django.contrib.auth.models import Permission  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.core.test_utils import AdminQueryCountMixin  # noqa: E402


def _catalog_staff(username, *codenames, superuser=False):
    model = get_user_model()
    if superuser:
        return model.objects.create_superuser(username=username, email=f"{username}@example.com", password="x")
    user = _staff(username)
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="catalog", codename__in=codenames))
    return user


VIEW_PERMS = ("view_productvariant", "view_productvariantimage")
ALL_IMAGE_PERMS = VIEW_PERMS + (
    "change_productvariant", "add_productvariantimage", "change_productvariantimage", "delete_productvariantimage",
)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ManageImagesAdminTests(TestCase):
    def setUp(self):
        self.variant = _gallery_variant()
        self.manager = _catalog_staff("catmanager", *ALL_IMAGE_PERMS)
        self.viewer = _catalog_staff("catviewer", *VIEW_PERMS)
        self.image = image_services.add_variant_images(self.variant, [_photo(10)], self.manager).created[0]

    def _url(self, name, *extra):
        return reverse(f"admin:catalog_productvariant_{name}", args=[self.variant.pk, *extra])

    def _json(self, response):
        return json.loads(response.content)

    def test_manage_page_renders_grid_for_manager(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._url("images"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.image.image.url)
        self.assertContains(response, "1 of 20")
        self.assertContains(response, 'name="images"')
        self.assertContains(response, "Make primary")

    def test_viewer_sees_grid_without_controls(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self._url("images"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.image.image.url)
        self.assertNotContains(response, 'name="images"')
        self.assertNotContains(response, "Make primary")

    def test_editor_loaded_only_for_change_permission(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._url("images"))
        self.assertContains(response, "vendor/cropperjs/1.7.0/cropper.min.js")
        self.assertContains(response, 'id="vg-editor"')
        self.assertContains(response, self._url("image_replace", self.image.public_id))
        self.client.force_login(self.viewer)
        response = self.client.get(self._url("images"))
        self.assertNotContains(response, "cropper.min.js")
        self.assertNotContains(response, 'id="vg-editor"')

    def test_page_needs_view_permissions(self):
        self.client.force_login(_catalog_staff("variantonly", "view_productvariant"))
        self.assertEqual(self.client.get(self._url("images")).status_code, 403)

    def test_mutations_refused_without_permission(self):
        self.client.force_login(self.viewer)
        posts = [
            (self._url("images_upload"), {"images": _photo(20)}),
            (self._url("images_reorder"), {"order": json.dumps([str(self.image.public_id)])}),
            (self._url("image_role", self.image.public_id), {"role": PRIMARY}),
            (self._url("image_alt_text", self.image.public_id), {"alt_text": "x"}),
            (self._url("image_replace", self.image.public_id), {"image": _photo(30)}),
            (self._url("image_remove", self.image.public_id), {}),
        ]
        for url, data in posts:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.image.refresh_from_db()
        self.assertEqual((self.image.role, self.image.alt_text), (GALLERY, ""))
        self.assertEqual(self.variant.images.count(), 1)

    def test_get_never_changes_state(self):
        self.client.force_login(self.manager)
        for name in ("images_upload", "images_reorder"):
            self.assertEqual(self.client.get(self._url(name)).status_code, 405)
        for name in ("image_role", "image_alt_text", "image_replace"):
            self.assertEqual(self.client.get(self._url(name, self.image.public_id)).status_code, 405)
        response = self.client.get(self._url("image_remove", self.image.public_id))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Remove image")
        self.assertTrue(ProductVariantImage.objects.filter(pk=self.image.pk).exists())

    def test_upload_json_and_form_fallback(self):
        self.client.force_login(self.manager)
        garbage = SimpleUploadedFile("bad.jpg", b"nope", content_type="image/jpeg")
        response = self.client.post(
            self._url("images_upload"), {"images": [_photo(20), garbage]}, HTTP_ACCEPT="application/json",
        )
        payload = self._json(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(payload["created"]), 1)
        self.assertEqual(payload["rejected"][0]["name"], "bad.jpg")
        response = self.client.post(self._url("images_upload"), {"images": [_photo(30)]})
        self.assertRedirects(response, self._url("images"), fetch_redirect_response=False)
        self.assertEqual(self.variant.images.count(), 3)

    def test_role_alt_text_reorder_remove(self):
        self.client.force_login(self.manager)
        other = image_services.add_variant_images(self.variant, [_photo(40)], self.manager).created[0]
        self.client.post(self._url("image_role", other.public_id), {"role": PRIMARY})
        self.client.post(self._url("image_alt_text", other.public_id), {"alt_text": "Front"})
        self.client.post(
            self._url("images_reorder"), {"order": json.dumps([str(other.public_id), str(self.image.public_id)])},
        )
        other.refresh_from_db()
        self.assertEqual((other.role, other.alt_text, other.sort_order), (PRIMARY, "Front", 0))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self._url("image_remove", other.public_id))
        self.assertRedirects(response, self._url("images"), fetch_redirect_response=False)
        self.assertFalse(ProductVariantImage.objects.filter(pk=other.pk).exists())

    def test_service_errors_are_reported(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            self._url("image_role", self.image.public_id), {"role": "hero"}, HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("primary", self._json(response)["error"])

    def test_replace_via_admin(self):
        self.client.force_login(self.manager)
        old_name = self.image.image.name
        response = self.client.post(
            self._url("image_replace", self.image.public_id), {"image": _photo(77, size=(1500, 1000))},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.image.refresh_from_db()
        self.assertNotEqual(self.image.image.name, old_name)
        self.assertEqual(self._json(response)["image"]["width"], 1500)

    def test_image_of_another_variant_is_404(self):
        foreign = image_services.add_variant_images(_gallery_variant("FOREIGN"), [_photo(50)], self.manager).created[0]
        self.client.force_login(self.manager)
        response = self.client.post(self._url("image_role", foreign.public_id), {"role": PRIMARY})
        self.assertEqual(response.status_code, 404)

    def test_csrf_enforced(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        response = client.post(self._url("image_role", self.image.public_id), {"role": PRIMARY})
        self.assertEqual(response.status_code, 403)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantChangePageGalleryTests(TestCase):
    def setUp(self):
        self.variant = _gallery_variant()
        self.manager = _catalog_staff("catmanager2", *ALL_IMAGE_PERMS)
        self.client.force_login(self.manager)

    def _change_page(self):
        return self.client.get(reverse("admin:catalog_productvariant_change", args=[self.variant.pk]))

    def test_inline_and_legacy_upload_removed(self):
        response = self._change_page()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="images-TOTAL_FORMS"')
        self.assertNotContains(response, 'name="image"')
        self.assertNotContains(response, "Legacy image")
        self.assertContains(response, "Manage images")
        self.assertContains(response, reverse("admin:catalog_productvariant_images", args=[self.variant.pk]))

    def test_add_page_has_no_image_upload(self):
        self.client.force_login(_catalog_staff("catsuper2", superuser=True))
        response = self.client.get(reverse("admin:catalog_productvariant_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="image"')
        self.assertNotContains(response, 'name="images-TOTAL_FORMS"')

    def test_preview_lists_gallery_and_count(self):
        image = image_services.add_variant_images(self.variant, [_photo(10)], self.manager).created[0]
        image_services.set_variant_image_role(image, PRIMARY, self.manager)
        response = self._change_page()
        self.assertContains(response, image.image.url)
        self.assertContains(response, "1 of 20")
        self.assertContains(response, "Primary")

    def test_legacy_image_shown_read_only_when_present(self):
        ProductVariant.objects.filter(pk=self.variant.pk).update(image="catalog/variants/legacy.jpg")
        response = self._change_page()
        self.assertContains(response, "Legacy image (not used by the gallery)")
        self.assertContains(response, "legacy.jpg")
        self.assertNotContains(response, 'name="image"')

    def test_saving_variant_keeps_legacy_image(self):
        ProductVariant.objects.filter(pk=self.variant.pk).update(image="catalog/variants/legacy.jpg")
        data = {
            "is_active": "on", "edition": self.variant.edition_id, "sku": self.variant.sku, "size_ml": 100,
            "mrp": "1000.00", "selling_price": "950.00", "shipping_surcharge": "0",
        }
        response = self.client.post(reverse("admin:catalog_productvariant_change", args=[self.variant.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.variant.refresh_from_db()
        self.assertEqual((self.variant.selling_price, self.variant.image.name), (Decimal("950.00"), "catalog/variants/legacy.jpg"))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VariantListGalleryColumnTests(AdminQueryCountMixin, TestCase):
    def setUp(self):
        self.user = _catalog_staff("catsuper", superuser=True)
        self.client.force_login(self.user)

    def test_list_shows_primary_then_legacy_fallback(self):
        from apps.catalog.admin import ProductVariantAdmin

        with_primary = _gallery_variant("LIST-1")
        image = image_services.add_variant_images(with_primary, [_photo(10)], self.user).created[0]
        image_services.set_variant_image_role(image, PRIMARY, self.user)
        legacy_only = _gallery_variant("LIST-2")
        ProductVariant.objects.filter(pk=legacy_only.pk).update(image="catalog/variants/legacy.jpg")
        model_admin = ProductVariantAdmin(ProductVariant, django_admin.site)
        request = RequestFactory().get("/")
        request.user = self.user
        rows = {v.pk: v for v in model_admin.get_queryset(request)}
        self.assertIn(image.image.url, model_admin.image_preview(rows[with_primary.pk]))
        self.assertIn("legacy.jpg", model_admin.image_preview(rows[legacy_only.pk]))
        self.assertEqual(model_admin.image_count(rows[with_primary.pk]), "1 / 20")

    def test_list_query_count_is_flat(self):
        def make(i):
            variant = _gallery_variant(f"FLAT-{i}")
            ProductVariantImage.objects.create(variant=variant, image=_fake_image(), role=PRIMARY)
        self.assertChangelistQueriesFlat(reverse("admin:catalog_productvariant_changelist"), make)
