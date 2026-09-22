"""
Import one brand's fragrance metadata from Parfumly.

    python manage.py import_parfumly_brand "Ahmed Al Maghribi" --category <slug> --dry-run

Creates/links Brand, Product, ProductEdition, PerfumeNote and EditionNote
only. Never creates ProductVariant records and never stores seller prices,
stock, availability, descriptions or images. Bottle/tester sizes found on
Parfumly are reported so staff can create the real Smerfume SKUs.

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
            help="Slug of the existing Smerfume Category assigned to newly created products.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--limit", type=int, default=None, help="Only process the first N products.")

    def handle(self, *args, brand, category, dry_run, limit, **options):
        try:
            category_obj = Category.objects.get(slug=category)
        except Category.DoesNotExist:
            raise CommandError(f"Category with slug {category!r} does not exist.")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive integer.")

        importer = ParfumlyBrandImporter(ParfumlyClient(), category_obj, limit=limit)
        try:
            plan = importer.plan(brand)
        except ImportAbort as exc:
            raise CommandError(str(exc))

        if not dry_run:
            importer.apply(plan)

        self._print_report(importer.report, dry_run)
        if importer.report.errors:
            raise CommandError(f"{len(importer.report.errors)} product(s) failed; see log.")

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
            ("attars", len(report.attars)),
            ("skipped", len(report.skipped)),
            ("possible duplicates", len(report.possible_duplicates)),
            ("possible note duplicates", len(report.possible_note_duplicates)),
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
            "Possible duplicates (not created - review manually)",
            [
                f"{d['skipped_name']} (parfumly: {d['skipped_parfumly_slug']}) ~ "
                f"{d['matched_name']} ({d['matched_source']}: {d['matched_slug']})"
                for d in report.possible_duplicates
            ],
        )
        self._section(
            "Possible note duplicates (created separately - review manually)",
            [f"{d['new_note']} ~ {d['similar_to']}" for d in report.possible_note_duplicates],
        )
        self._section("Warnings", report.warnings)
        self._section("Errors", report.errors)

    def _section(self, title, lines):
        if not lines:
            return
        self.stdout.write(self.style.MIGRATE_LABEL(f"\n{title} ({len(lines)}):"))
        for line in lines:
            self.stdout.write(f"  - {line}")
