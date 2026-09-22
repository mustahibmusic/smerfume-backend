"""
Import one brand's fragrance metadata from Parfumly.

    python manage.py import_parfumly_brand "Ahmed Al Maghribi" \
        --category perfume --attar-category attar --dry-run

Creates/links Brand, Product, ProductEdition, PerfumeNote and EditionNote
only. Never creates ProductVariant records and never stores seller prices,
stock, availability, descriptions or images. Bottle/tester sizes found on
Parfumly are reported so staff can create the real Smerfume SKUs.

New products go to --category (perfume concentrations) or --attar-category
(ATTAR-only products). Without --attar-category, ATTAR-only products are
skipped. Products with both ATTAR and perfume concentrations are always
skipped and reported for manual review.

--dry-run performs no database writes at all.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.catalog.models import Category
from apps.catalog.parfumly.client import ParfumlyClient
from apps.catalog.parfumly.importer import ImportAbort, ParfumlyBrandImporter


class Command(BaseCommand):
    help = "Import a brand's fragrance metadata (no variants/prices) from Parfumly."

    def add_arguments(self, parser):
        parser.add_argument("brand", help='Brand name as listed on Parfumly, e.g. "Ahmed Al Maghribi".')
        parser.add_argument(
            "--category",
            required=True,
            help="Slug of the existing Category for new perfume (EDC/EDT/EDP/EXTRAIT) products.",
        )
        parser.add_argument(
            "--attar-category",
            default=None,
            help="Slug of the existing Category for new ATTAR-only products.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--limit", type=int, default=None, help="Only process the first N products.")

    def handle(self, *args, brand, category, attar_category, dry_run, limit, **options):
        category_obj = self._get_category(category)
        attar_category_obj = self._get_category(attar_category) if attar_category else None
        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive integer.")

        importer = ParfumlyBrandImporter(
            ParfumlyClient(), category_obj, attar_category=attar_category_obj, limit=limit
        )
        try:
            plan = importer.plan(brand)
        except ImportAbort as exc:
            raise CommandError(str(exc))

        if not dry_run:
            importer.apply(plan)

        self._print_report(importer.report, dry_run)
        if importer.report.errors:
            raise CommandError(f"{len(importer.report.errors)} product(s) failed; see log.")

    @staticmethod
    def _get_category(slug):
        try:
            return Category.objects.get(slug=slug)
        except Category.DoesNotExist:
            raise CommandError(f"Category with slug {slug!r} does not exist.")

    def _print_report(self, report, dry_run):
        out = self.stdout
        mode = "DRY RUN - nothing written" if dry_run else "IMPORT APPLIED"
        out.write(self.style.MIGRATE_HEADING(f"Parfumly import: {report.brand} ({mode})"))
        out.write(f"brand: {report.brand_action}")

        counts = [
            ("fetched", report.fetched),
            ("valid products", report.valid_products),
            ("products to create", len(report.products_to_create)),
            ("existing products", len(report.existing_products)),
            ("editions to create", len(report.editions_to_create)),
            ("existing editions", len(report.existing_editions)),
            ("release years to fill", len(report.release_years_to_fill)),
            ("notes to create", len(report.notes_to_create)),
            ("note links to create", report.note_links_to_create),
            ("existing note links", report.existing_note_links),
            ("perfumes", len(report.perfumes)),
            ("attars", len(report.attars)),
            ("mixed (skipped)", len(report.mixed_concentration_category)),
            ("skipped", len(report.skipped)),
            ("duplicates skipped", len(report.duplicates_skipped)),
            ("possible duplicates", len(report.possible_duplicates)),
            ("possible note duplicates", len(report.possible_note_duplicates)),
            ("note position conflicts", len(report.note_position_conflicts)),
            ("warnings", len(report.warnings)),
            ("errors", len(report.errors)),
        ]
        for label, value in counts:
            out.write(f"  {label:<26}{value}")

        self._section("Products to create", report.products_to_create)
        self._section("Existing products", report.existing_products)
        self._section("Editions to create", report.editions_to_create)
        self._section("Existing editions", report.existing_editions)
        self._section("Release years to fill", report.release_years_to_fill)
        self._section("Notes to create", report.notes_to_create)
        self._section("Perfumes", report.perfumes)
        self._section("Attars", report.attars)
        self._section(
            "Bottle/tester sizes discovered (create real Smerfume variants manually)",
            [f"{name}: {', '.join(sizes)}" for name, sizes in sorted(report.retail_sizes.items())],
        )
        self._section(
            "Skipped",
            [f"{s['name']} ({s['parfumly_slug']}): {s['reason']}" for s in report.skipped],
        )
        self._section(
            "Mixed ATTAR + perfume products (not created - review manually)",
            [
                f"{m['name']} ({m['parfumly_slug']}): {', '.join(m['concentrations'])}"
                for m in report.mixed_concentration_category
            ],
        )
        self._section(
            "Duplicates skipped (exact/strong - not created)",
            [
                f"[{d['rule']}] {d['name']} (parfumly: {d['parfumly_slug']}) = "
                f"{d['matched_name']} ({d['matched_source']}: {d['matched_slug']})"
                for d in report.duplicates_skipped
            ],
        )
        self._section(
            "Possible duplicates (fuzzy - created, review manually)",
            [
                f"{d['name']} (parfumly: {d['parfumly_slug']}) ~ "
                f"{d['similar_name']} ({d['similar_source']}: {d['similar_slug']})"
                for d in report.possible_duplicates
            ],
        )
        self._section(
            "Possible note duplicates (created separately - review manually)",
            [f"{d['new_note']} ~ {d['similar_to']}" for d in report.possible_note_duplicates],
        )
        self._section(
            "Note position conflicts (first position kept)",
            [
                f"{c['product']} ({c['parfumly_slug']}): {c['note']} "
                f"listed as {'/'.join(c['positions'])}; kept {c['kept']}"
                for c in report.note_position_conflicts
            ],
        )
        self._section("Warnings", report.warnings)
        self._section("Errors", report.errors)

    def _section(self, title, lines):
        if not lines:
            return
        self.stdout.write(self.style.MIGRATE_LABEL(f"\n{title} ({len(lines)}):"))
        for line in lines:
            self.stdout.write(f"  - {line}")
