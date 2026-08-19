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

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from .models import (
    Brand,
    Category,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
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
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=50,
            mrp=Decimal("3000.00"), selling_price=Decimal("2700.00"),
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=10, is_decant=True,
            mrp=Decimal("1000.00"), selling_price=Decimal("500.00"),
        )
        ProductVariant.objects.create(
            edition=cls.edition_chance_men, size_ml=200,
            mrp=Decimal("9000.00"), selling_price=Decimal("8000.00"),
            is_active=False,
        )
        # Variants — chance pour femme
        ProductVariant.objects.create(
            edition=cls.edition_chance_women, size_ml=75,
            mrp=Decimal("4000.00"), selling_price=Decimal("3500.00"),
        )
        # Variants — sauvage
        ProductVariant.objects.create(
            edition=cls.edition_sauvage, size_ml=100,
            mrp=Decimal("6000.00"), selling_price=Decimal("5500.00"),
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
