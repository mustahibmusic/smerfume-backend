"""
Tests for the Parfumly brand importer (apps.catalog.parfumly and the
import_parfumly_brand management command).

No network access: the HTTP client is exercised with a fake opener and the
importer with an in-memory FakeClient. Payloads mirror real
api.parfumly.in responses, including seller offers, prices, stock and
descriptions, so the tests can prove none of that is ever persisted.
"""

import datetime
import io
import json
import urllib.error
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.catalog.models import (
    Brand,
    Category,
    EditionNote,
    PerfumeNote,
    Product,
    ProductEdition,
    ProductVariant,
)
from apps.catalog.parfumly.client import ParfumlyClient, ParfumlyError
from apps.catalog.parfumly.importer import ImportAbort, ParfumlyBrandImporter
from apps.catalog.parfumly.normalize import (
    is_possible_duplicate,
    normalize_product,
    normalize_release_year,
)

BRAND = {"name": "Ahmed Al Maghribi", "slug": "ahmed-al-maghribi", "tier": "Arabian"}
SELLER_NAME = "FridayCharm"
DESCRIPTION = "SELLER MARKETING COPY that must never be stored."


def offer(price=2400):
    return {
        "listingId": "listing-1",
        "seller": {"id": "seller-1", "name": SELLER_NAME, "slug": "fridaycharm"},
        "price": price,
        "currency": "INR",
        "inStock": True,
        "stockQty": 7,
        "stockState": "IN_STOCK",
        "productUrl": "https://fridaycharm.example/products/x",
    }


def variant(concentration="EDP", size=100, form="BOTTLE"):
    return {
        "id": f"v-{concentration}-{size}-{form}",
        "slug": f"x-{size}-{concentration.lower()}-{form.lower()}",
        "sizeMl": size,
        "concentration": concentration,
        "form": form,
        "mrp": 4000,
        "lowest": 2400,
        "highest": 2800,
        "sellerCount": 1,
        "offers": [offer()],
    }


def make_detail(name, slug=None, gender="UNISEX", year=None, variants=None, notes=None, brand=None):
    slug = slug or "ahmed-al-maghribi-" + name.lower().replace(" ", "-")
    if variants is None:
        variants = [
            variant("EDP", 100, "BOTTLE"),
            variant("EXTRAIT", 10, "DECANT"),  # seller decant: not evidence of an edition
            variant("EDP", 2, "SAMPLE"),
            variant("EDP", 2, "GIFT_SET"),
        ]
    if notes is None:
        notes = {
            "top": [{"name": "Bergamot", "slug": "bergamot"}],
            "heart": [{"name": "Rose", "slug": "rose"}],
            "base": [{"name": "Musk", "slug": "musk"}],
        }
    return {
        "id": f"id-{slug}",
        "name": name,
        "slug": slug,
        "gender": gender,
        "year": year,
        "description": DESCRIPTION,
        "accords": ["Woody"],
        "tags": ["tag"],
        "notes": notes,
        "brand": dict(brand or BRAND),
        "imageUrl": "/images/abc",
        "images": ["/images/abc"],
        "imageCredits": [SELLER_NAME],
        "variants": variants,
        "editions": [],
    }


def summary(detail):
    return {
        "id": detail["id"],
        "name": detail["name"],
        "slug": detail["slug"],
        "gender": detail["gender"],
        "brand": dict(detail["brand"]),
        "notes": ["Bergamot"],
        "variants": [
            {"concentration": v["concentration"], "sizeMl": v["sizeMl"], "form": v["form"],
             "bestPrice": 2400, "inStock": True}
            for v in detail["variants"]
        ],
        "bestPrice": 2400,
        "mrp": 4000,
        "sellerCount": 3,
    }


class FakeClient:
    def __init__(self, details, failing_slugs=()):
        self.details = {d["slug"]: d for d in details}
        self.failing_slugs = set(failing_slugs)
        self.searched = []

    def search_brand(self, brand_slug):
        self.searched.append(brand_slug)
        return [summary(d) for d in self.details.values()]

    def get_product(self, slug):
        if slug in self.failing_slugs:
            raise ParfumlyError(f"HTTP 503 for {slug}")
        return self.details[slug]


# ── Normalization ────────────────────────────────────────────────────────


class NormalizeTests(SimpleTestCase):
    def test_release_year_rules(self):
        this_year = datetime.date.today().year
        self.assertEqual(normalize_release_year(2019), 2019)
        self.assertEqual(normalize_release_year("2019"), 2019)
        for bad in (None, 0, "0", "abc", 1850, this_year + 5, True, 2019.5, -3):
            self.assertIsNone(normalize_release_year(bad), bad)

    def test_gender_and_concentration_mapping(self):
        source = normalize_product(make_detail(
            "Mix", gender="FEMININE",
            variants=[variant("ATTAR", 12, "BOTTLE"), variant("EDP", 100, "TESTER")],
        ))
        self.assertEqual(source.gender, "women")
        self.assertEqual(source.concentrations, ["attar", "edp"])
        self.assertEqual(
            [str(s) for s in source.retail_sizes], ["ATTAR 12ml BOTTLE", "EDP 100ml TESTER"]
        )

    def test_only_bottle_and_tester_forms_count(self):
        source = normalize_product(make_detail("Aqua Oud"))
        # EXTRAIT appears only as a seller decant; samples/gift sets ignored.
        self.assertEqual(source.concentrations, ["edp"])
        self.assertEqual([str(s) for s in source.retail_sizes], ["EDP 100ml BOTTLE"])

    def test_unknown_concentration_is_skipped_with_warning(self):
        source = normalize_product(make_detail(
            "Odd", variants=[variant("PERFUME_OIL", 30, "BOTTLE")],
        ))
        self.assertEqual(source.concentrations, [])
        self.assertTrue(any("PERFUME_OIL" in w for w in source.warnings))

    def test_notes_keep_positions_and_first_position_wins(self):
        source = normalize_product(make_detail("N", notes={
            "top": [{"name": " Bergamot "}],
            "heart": [{"name": "bergamot"}, {"name": "Rose"}],
            "base": [{"name": "Musk"}],
            "general": [{"name": "Oud"}],
        }))
        self.assertEqual(
            [(n.name, n.position) for n in source.notes],
            [("Bergamot", "top"), ("Rose", "heart"), ("Musk", "base")],
        )
        self.assertTrue(any("general" in w for w in source.warnings))

    def test_normalized_product_carries_no_commercial_or_description_data(self):
        source = normalize_product(make_detail("Aqua Oud"))
        dumped = repr(source)
        for forbidden in (SELLER_NAME, DESCRIPTION, "2400", "4000", "INR", "/images/"):
            self.assertNotIn(forbidden, dumped)

    def test_possible_duplicate_heuristic(self):
        for a, b in [
            ("Blu", "Blue"),
            ("Blu Oud", "Blue Oud"),
            ("Blush Noir", "Blush Noire"),
            ("Anaab", "Anab"),
            ("Bin Shaikh", "Bin Shaikh Made In Uae"),
            ("Oak Moss", "Oakmoss"),
        ]:
            self.assertTrue(is_possible_duplicate(a, b), (a, b))
        for a, b in [("Blu", "Blu Oud"), ("Aqua Oud", "Azure Royal"), ("Rose", "rose ")]:
            self.assertFalse(is_possible_duplicate(a, b), (a, b))


# ── HTTP client ──────────────────────────────────────────────────────────


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParfumlyClientTests(SimpleTestCase):
    def make_client(self, responses):
        self.urls = []
        self.sleeps = []
        queue = list(responses)

        def opener(request, timeout):
            self.urls.append(request.full_url)
            result = queue.pop(0)
            if isinstance(result, Exception):
                raise result
            return FakeResponse(json.dumps(result).encode())

        return ParfumlyClient(
            base_url="https://api.test", delay_seconds=0, opener=opener, sleep=self.sleeps.append
        )

    def http_error(self, code):
        return urllib.error.HTTPError("https://api.test", code, "err", {}, None)

    def test_search_follows_pagination_with_max_page_size(self):
        client = self.make_client([
            {"items": [{"slug": "a"}, {"slug": "b"}], "total": 3, "page": 1},
            {"items": [{"slug": "c"}], "total": 3, "page": 2},
        ])
        items = client.search_brand("ahmed-al-maghribi")
        self.assertEqual([i["slug"] for i in items], ["a", "b", "c"])
        self.assertEqual(len(self.urls), 2)
        self.assertIn("brand=ahmed-al-maghribi", self.urls[0])
        self.assertIn("pageSize=48", self.urls[0])
        self.assertIn("page=2", self.urls[1])

    def test_search_stops_on_empty_page(self):
        client = self.make_client([{"items": [], "total": 5}])
        self.assertEqual(client.search_brand("x"), [])

    def test_retries_rate_limit_then_succeeds(self):
        client = self.make_client([self.http_error(429), {"slug": "aqua"}])
        with self.assertLogs("apps.catalog.parfumly.client", "WARNING"):
            self.assertEqual(client.get_product("aqua"), {"slug": "aqua"})
        self.assertEqual(len(self.urls), 2)
        self.assertTrue(self.sleeps)

    def test_client_error_is_not_retried(self):
        client = self.make_client([self.http_error(404)])
        with self.assertRaises(ParfumlyError), self.assertLogs("apps.catalog.parfumly.client"):
            client.get_product("missing")
        self.assertEqual(len(self.urls), 1)

    def test_gives_up_after_max_attempts(self):
        client = self.make_client([self.http_error(503)] * 3)
        with self.assertRaises(ParfumlyError), self.assertLogs("apps.catalog.parfumly.client"):
            client.get_product("aqua")
        self.assertEqual(len(self.urls), 3)

    def test_product_slug_is_url_quoted(self):
        client = self.make_client([{}])
        client.get_product("a/b")
        self.assertTrue(self.urls[0].endswith("/products/a%2Fb"))


# ── Importer ─────────────────────────────────────────────────────────────


class ImporterTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Fragrance", slug="fragrance")

    def run_import(self, details, dry_run=False, **kwargs):
        importer = ParfumlyBrandImporter(FakeClient(details, **kwargs), self.category)
        plan = importer.plan("Ahmed Al Maghribi")
        if not dry_run:
            importer.apply(plan)
        return importer.report

    def counts(self):
        return {
            model.__name__: model.objects.count()
            for model in (Brand, Product, ProductEdition, PerfumeNote, EditionNote, ProductVariant)
        }


class ImporterCreateTests(ImporterTestBase):
    def test_creates_brand_product_edition_and_notes(self):
        report = self.run_import([make_detail("Aqua Oud", gender="MASCULINE", year=2019)])

        brand = Brand.objects.get()
        self.assertEqual((brand.name, brand.slug, brand.brand_category),
                         ("Ahmed Al Maghribi", "ahmed-al-maghribi", "middle eastern"))
        product = Product.objects.get()
        self.assertEqual(product.slug, "ahmed-al-maghribi-aqua-oud")
        self.assertEqual(product.category, self.category)
        edition = product.editions.get()
        self.assertEqual((edition.gender, edition.concentration, edition.release_year),
                         ("men", "edp", 2019))
        self.assertIsNone(edition.name)
        self.assertEqual(
            sorted(edition.edition_notes.values_list("note__name", "position")),
            [("Bergamot", "top"), ("Musk", "base"), ("Rose", "heart")],
        )
        self.assertEqual(ProductVariant.objects.count(), 0)
        self.assertEqual(report.retail_sizes, {"Aqua Oud": ["EDP 100ml BOTTLE"]})
        self.assertEqual(report.errors, [])

    def test_attar_maps_to_attar_edition(self):
        report = self.run_import([make_detail(
            "Aswad Attar", variants=[variant("ATTAR", 12, "BOTTLE")],
        )])
        self.assertEqual(ProductEdition.objects.get().concentration, "attar")
        self.assertEqual(report.attars, ["Aswad Attar"])

    def test_multiple_concentrations_get_named_editions(self):
        self.run_import([make_detail(
            "Azure Royal",
            variants=[variant("EDP", 100, "BOTTLE"), variant("EXTRAIT", 50, "BOTTLE")],
        )])
        editions = ProductEdition.objects.order_by("concentration")
        self.assertEqual(
            [(e.concentration, e.name, e.slug) for e in editions],
            [("edp", "Eau de Parfum", "edp"), ("extrait", "Extrait de Parfum", "extrait")],
        )

    def test_unknown_concentration_creates_no_product(self):
        report = self.run_import([make_detail(
            "Odd", variants=[variant("PERFUME_OIL", 30, "BOTTLE"), variant("EDP", 10, "DECANT")],
        )])
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(ProductEdition.objects.count(), 0)
        self.assertEqual(report.skipped[0]["reason"], "no supported bottle/tester concentration")
        self.assertTrue(any("PERFUME_OIL" in w for w in report.warnings))

    def test_no_product_left_behind_when_edition_creation_fails(self):
        with mock.patch.object(
            ProductEdition.objects, "create", side_effect=RuntimeError("boom")
        ), self.assertLogs("apps.catalog.parfumly.importer", "ERROR"):
            report = self.run_import([make_detail("Aqua Oud")])
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(PerfumeNote.objects.count(), 0)
        self.assertEqual(report.errors, ["ahmed-al-maghribi-aqua-oud: RuntimeError"])

    def test_fetch_failure_skips_product(self):
        good, bad = make_detail("Aqua Oud"), make_detail("Azure Royal")
        report = self.run_import([good, bad], failing_slugs=[bad["slug"]])
        self.assertEqual(list(Product.objects.values_list("name", flat=True)), ["Aqua Oud"])
        self.assertIn("fetch failed", report.skipped[0]["reason"])

    def test_seller_and_description_data_never_persisted(self):
        self.run_import([make_detail("Aqua Oud")])
        self.assertEqual(ProductVariant.objects.count(), 0)
        self.assertFalse(hasattr(ProductEdition, "description"))
        for model in (Brand, Product, ProductEdition, PerfumeNote, EditionNote):
            for row in model.objects.values():
                dumped = repr(row)
                for forbidden in (SELLER_NAME, "SELLER MARKETING COPY", "fridaycharm", "/images/"):
                    self.assertNotIn(forbidden, dumped, model.__name__)
        edition = ProductEdition.objects.get()
        self.assertFalse(edition.image)

    def test_unknown_brand_tier_aborts_when_brand_missing(self):
        detail = make_detail("Aqua Oud", brand={**BRAND, "tier": "Mystery"})
        with self.assertRaises(ImportAbort):
            self.run_import([detail])
        self.assertEqual(Brand.objects.count(), 0)


class ImporterIdempotencyTests(ImporterTestBase):
    def test_second_run_creates_nothing(self):
        details = [
            make_detail("Aqua Oud", year=2019),
            make_detail("Azure Royal", variants=[variant("EDP", 100), variant("EXTRAIT", 50)]),
        ]
        self.run_import(details)
        before = self.counts()
        report = self.run_import(details)
        self.assertEqual(self.counts(), before)
        self.assertEqual(report.products_to_create, [])
        self.assertEqual(report.editions_to_create, [])
        self.assertEqual(report.notes_to_create, [])
        self.assertEqual(report.note_links_to_create, 0)
        self.assertEqual(len(report.existing_products), 2)
        self.assertEqual(len(report.existing_editions), 3)

    def test_existing_brand_matched_by_name_case_insensitively(self):
        brand = Brand.objects.create(name="AHMED AL MAGHRIBI", slug="aam", brand_category="niche")
        self.run_import([make_detail("Aqua Oud")])
        self.assertEqual(Brand.objects.count(), 1)
        self.assertEqual(Product.objects.get().brand, brand)
        brand.refresh_from_db()
        self.assertEqual(brand.brand_category, "niche")

    def test_existing_product_matched_by_normalized_name(self):
        brand = Brand.objects.create(name="Ahmed Al Maghribi", slug="ahmed-al-maghribi")
        other_category = Category.objects.create(name="Gifts", slug="gifts")
        product = Product.objects.create(
            name="  AQUA   oud", slug="aqua-oud", brand=brand, category=other_category
        )
        report = self.run_import([make_detail("Aqua Oud")])
        self.assertEqual(Product.objects.count(), 1)
        product.refresh_from_db()
        self.assertEqual((product.name, product.slug, product.category),
                         ("  AQUA   oud", "aqua-oud", other_category))
        self.assertEqual(report.editions_to_create, ["  AQUA   oud [edp]"])

    def test_existing_values_never_overwritten(self):
        brand = Brand.objects.create(name="Ahmed Al Maghribi", slug="ahmed-al-maghribi")
        product = Product.objects.create(
            name="Aqua Oud", slug="aqua-oud", brand=brand, category=self.category
        )
        dated = ProductEdition.objects.create(
            product=product, concentration="edp", gender="men", release_year=2010
        )
        undated = ProductEdition.objects.create(
            product=Product.objects.create(
                name="Azure Royal", slug="azure-royal", brand=brand, category=self.category
            ),
            concentration="edp", gender="women",
        )
        bergamot = PerfumeNote.objects.create(name="Bergamot")
        EditionNote.objects.create(edition=dated, note=bergamot, position="base")

        report = self.run_import([
            make_detail("Aqua Oud", gender="UNISEX", year=2015),
            make_detail("Azure Royal", gender="UNISEX", year=2018),
        ])

        dated.refresh_from_db()
        undated.refresh_from_db()
        self.assertEqual((dated.gender, dated.release_year), ("men", 2010))
        self.assertEqual((undated.gender, undated.release_year), ("women", 2018))
        self.assertEqual(report.release_years_to_fill, ["Azure Royal [edp]"])
        # Existing link keeps its position and is not duplicated.
        self.assertEqual(
            list(dated.edition_notes.filter(note=bergamot).values_list("position", flat=True)),
            ["base"],
        )
        self.assertEqual(dated.edition_notes.count(), 3)

    def test_note_matched_case_insensitively_with_trimmed_whitespace(self):
        existing = PerfumeNote.objects.create(name="bergamot")
        self.run_import([make_detail("Aqua Oud", notes={"top": [{"name": "  Bergamot "}]})])
        self.assertEqual(PerfumeNote.objects.count(), 1)
        self.assertEqual(EditionNote.objects.get().note, existing)

    def test_ambiguous_existing_editions_are_skipped(self):
        brand = Brand.objects.create(name="Ahmed Al Maghribi", slug="ahmed-al-maghribi")
        product = Product.objects.create(
            name="Aqua Oud", slug="aqua-oud", brand=brand, category=self.category
        )
        ProductEdition.objects.create(product=product, concentration="edp", name="A", slug="a")
        ProductEdition.objects.create(product=product, concentration="edp", name="B", slug="b")
        report = self.run_import([make_detail("Aqua Oud")])
        self.assertEqual(ProductEdition.objects.count(), 2)
        self.assertEqual(EditionNote.objects.count(), 0)
        self.assertIn("no edition can be created or matched", report.skipped[0]["reason"])


class ImporterDuplicateTests(ImporterTestBase):
    def test_near_duplicate_in_same_import_is_not_created(self):
        report = self.run_import([make_detail("Blu"), make_detail("Blue")])
        self.assertEqual(list(Product.objects.values_list("name", flat=True)), ["Blu"])
        self.assertEqual(report.possible_duplicates, [{
            "skipped_name": "Blue",
            "skipped_parfumly_slug": "ahmed-al-maghribi-blue",
            "matched_name": "Blu",
            "matched_slug": "ahmed-al-maghribi-blu",
            "matched_source": "parfumly",
        }])

    def test_near_duplicate_of_existing_product_is_not_created(self):
        brand = Brand.objects.create(name="Ahmed Al Maghribi", slug="ahmed-al-maghribi")
        Product.objects.create(
            name="Blush Noir", slug="blush-noir", brand=brand, category=self.category
        )
        report = self.run_import([make_detail("Blush Noire")])
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(report.possible_duplicates[0]["matched_source"], "smerfume")
        self.assertEqual(report.possible_duplicates[0]["matched_slug"], "blush-noir")
        self.assertEqual(report.possible_duplicates[0]["skipped_parfumly_slug"],
                         "ahmed-al-maghribi-blush-noire")

    def test_near_duplicate_note_is_reported_not_merged(self):
        oak_moss = PerfumeNote.objects.create(name="Oak Moss")
        report = self.run_import([make_detail("Aqua Oud", notes={"base": [{"name": "Oakmoss"}]})])
        self.assertEqual(
            sorted(PerfumeNote.objects.values_list("name", flat=True)), ["Oak Moss", "Oakmoss"]
        )
        self.assertNotEqual(EditionNote.objects.get().note, oak_moss)
        self.assertEqual(report.possible_note_duplicates,
                         [{"new_note": "Oakmoss", "similar_to": "Oak Moss"}])


class ImporterDryRunTests(ImporterTestBase):
    def test_dry_run_performs_no_writes(self):
        PerfumeNote.objects.create(name="Oak Moss")
        before = self.counts()
        details = [
            make_detail("Aqua Oud", year=2019, notes={"base": [{"name": "Oakmoss"}]}),
            make_detail("Aqua Ouds"),
            make_detail("Aswad Attar", variants=[variant("ATTAR", 12)]),
            make_detail("Odd", variants=[variant("PERFUME_OIL", 30)]),
        ]
        with CaptureQueriesContext(connection) as queries:
            report = self.run_import(details, dry_run=True)

        self.assertEqual(self.counts(), before)
        for query in queries.captured_queries:
            sql = query["sql"].lstrip().upper()
            self.assertTrue(sql.startswith("SELECT"), sql)

        self.assertEqual(report.brand_action, "create")
        self.assertEqual(report.fetched, 4)
        self.assertEqual(report.valid_products, 3)
        self.assertEqual(len(report.products_to_create), 2)
        self.assertEqual(len(report.editions_to_create), 2)
        self.assertEqual(report.notes_to_create, ["Bergamot", "Musk", "Oakmoss", "Rose"])
        self.assertEqual(report.attars, ["Aswad Attar"])
        self.assertEqual(len(report.possible_duplicates), 1)
        self.assertEqual(len(report.possible_note_duplicates), 1)
        self.assertEqual(len(report.skipped), 2)


# ── Management command ───────────────────────────────────────────────────


class ImportCommandTests(ImporterTestBase):
    def call(self, *args, details=None):
        out = io.StringIO()
        client = FakeClient(details or [make_detail("Aqua Oud")])
        with mock.patch(
            "apps.catalog.management.commands.import_parfumly_brand.ParfumlyClient",
            return_value=client,
        ):
            call_command("import_parfumly_brand", "Ahmed Al Maghribi", *args, stdout=out)
        return out.getvalue()

    def test_category_is_required(self):
        with self.assertRaises(CommandError):
            self.call()

    def test_unknown_category_is_rejected(self):
        with self.assertRaises(CommandError):
            self.call("--category", "nope")
        self.assertEqual(Product.objects.count(), 0)

    def test_dry_run_reports_and_writes_nothing(self):
        output = self.call("--category", "fragrance", "--dry-run")
        self.assertIn("DRY RUN", output)
        self.assertIn("EDP 100ml BOTTLE", output)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(Brand.objects.count(), 0)

    def test_import_writes(self):
        output = self.call("--category", "fragrance")
        self.assertIn("IMPORT APPLIED", output)
        self.assertEqual(Product.objects.count(), 1)
