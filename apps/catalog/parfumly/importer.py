"""
Idempotent Parfumly brand import.

Two phases:

    plan()   Read-only. Fetches from Parfumly, normalizes, and compares
             against the catalogue. Produces an ImportPlan + ImportReport.
             A dry run stops here, so it performs zero database writes.
    apply()  Writes the plan, one transaction per product.

Imported: Brand, Product, ProductEdition, PerfumeNote, EditionNote.
Never imported: ProductVariant (no legitimate SKU/MRP/selling price exists
yet), seller offers/prices/stock/availability, descriptions, images.

Idempotency / matching rules:
    Brand        existing slug, then case-insensitive name.
    Product      exact case-insensitive, whitespace-normalized name within
                 the brand matches an existing product. Otherwise:
                   exact same name in this import -> skipped (duplicate)
                   strong alias evidence          -> skipped (duplicate):
                     name equal ignoring spaces/punctuation, or the
                     Parfumly slug already used by a product of the brand
                   fuzzy name similarity only     -> created, and reported
                     as a possible duplicate for review
                 Nothing is ever merged.
    Edition      (product, concentration) — see match_edition().
    PerfumeNote  exact case-insensitive, whitespace-trimmed name. Look-alike
                 names ("Oak Moss" / "Oakmoss") are NOT merged; the new note
                 is created and the pair is reported for review.
    EditionNote  only missing (edition, note) links are created; existing
                 links are never deleted or moved.
    Existing values are never overwritten; release_year is only filled
    where it is currently NULL.

Category routing (new products only; an existing product's category is
never changed): ATTAR-only products go to the attar category, perfume
concentrations (EDC/EDT/EDP/EXTRAIT) to the perfume category. A product
with both ATTAR and perfume concentrations is skipped for manual review,
because one Product has one Category.
"""

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils.text import slugify

from apps.catalog.models import Brand, EditionNote, PerfumeNote, Product, ProductEdition

from .client import ParfumlyError
from .normalize import (
    clean_name,
    is_possible_duplicate,
    is_strong_duplicate,
    match_key,
    normalize_product,
)

logger = logging.getLogger(__name__)

# Parfumly brand tier -> Brand.brand_category, used only when creating a brand.
BRAND_TIER_MAP = {
    "arabian": "middle eastern",
    "designer": "designer",
    "niche": "niche",
}

CONCENTRATION_LABELS = dict(ProductEdition.CONCENTRATION_CHOICES)


class ImportAbort(Exception):
    """The import cannot proceed at all (e.g. brand cannot be resolved)."""


@dataclass
class ImportReport:
    brand: str = ""
    brand_action: str = ""  # "existing" | "create"
    fetched: int = 0
    valid_products: int = 0
    products_to_create: list = field(default_factory=list)
    existing_products: list = field(default_factory=list)
    editions_to_create: list = field(default_factory=list)
    existing_editions: list = field(default_factory=list)
    release_years_to_fill: list = field(default_factory=list)
    notes_to_create: list = field(default_factory=list)
    note_links_to_create: int = 0
    existing_note_links: int = 0
    retail_sizes: dict = field(default_factory=dict)
    perfumes: list = field(default_factory=list)
    attars: list = field(default_factory=list)
    mixed_concentration_category: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    duplicates_skipped: list = field(default_factory=list)
    possible_duplicates: list = field(default_factory=list)
    possible_note_duplicates: list = field(default_factory=list)
    note_position_conflicts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


@dataclass
class EditionPlan:
    concentration: str
    edition: ProductEdition | None  # None -> create
    name: str | None = None
    slug: str | None = None
    fill_release_year: bool = False
    note_links: list = field(default_factory=list)  # [(note_key, position)]


@dataclass
class ProductPlan:
    source: object  # NormalizedProduct
    product: Product | None  # None -> create
    category: object = None  # Category for a new product
    editions: list = field(default_factory=list)


@dataclass
class ImportPlan:
    brand: Brand | None
    brand_values: dict
    products: list = field(default_factory=list)
    new_notes: dict = field(default_factory=dict)  # note_key -> display name


def match_edition(product, concentration):
    """
    Resolve the existing edition for (product, concentration).

    Returns (edition_or_None, ambiguous). Kept separate so the matching rule
    can evolve (e.g. to an external reference) without touching the importer.
    """
    if product is None:
        return None, False
    matches = list(product.editions.filter(concentration=concentration).order_by("id")[:2])
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


class ParfumlyBrandImporter:
    def __init__(self, client, category, attar_category=None, limit=None):
        self.client = client
        self.category = category
        self.attar_category = attar_category
        self.limit = limit
        self.report = ImportReport()

    # ── Planning (read-only) ────────────────────────────────────────────

    def plan(self, brand_name):
        report = self.report
        brand_name = clean_name(brand_name)
        brand_slug = slugify(brand_name)
        report.brand = brand_name
        if not brand_slug:
            raise ImportAbort("Brand name is empty.")

        try:
            items = self.client.search_brand(brand_slug)
        except ParfumlyError as exc:
            raise ImportAbort(f"Parfumly search failed: {exc}") from exc

        summaries = []
        for item in items:
            item_brand = (item.get("brand") or {}).get("slug")
            if item_brand != brand_slug:
                report.warnings.append(
                    f"{item.get('slug')}: brand {item_brand!r} is not {brand_slug!r}; ignored"
                )
                continue
            summaries.append(item)
        report.fetched = len(summaries)
        if not summaries:
            raise ImportAbort(f"Parfumly returned no products for brand slug {brand_slug!r}.")

        brand, brand_values = self._resolve_brand(brand_name, brand_slug, summaries[0])
        plan = ImportPlan(brand=brand, brand_values=brand_values)

        summaries.sort(key=lambda s: (match_key(s.get("name")), s.get("slug") or ""))
        if self.limit:
            summaries = summaries[: self.limit]

        existing_products = {}
        if brand is not None:
            for product in Product.objects.filter(brand=brand).order_by("id"):
                existing_products.setdefault(match_key(product.name), product)
        # Names accepted in this run: key -> (display name, reference, source)
        accepted = {
            key: (p.name, p.slug, "smerfume") for key, p in existing_products.items()
        }

        notes_by_key = self._load_notes()

        for summary in summaries:
            slug = summary.get("slug") or ""
            try:
                detail = self.client.get_product(slug)
            except ParfumlyError as exc:
                report.skipped.append(self._skip(summary.get("name"), slug, f"fetch failed: {exc}"))
                continue

            source = normalize_product(detail)
            for warning in source.warnings:
                report.warnings.append(f"{source.external_slug}: {warning}")
            for conflict in source.note_position_conflicts:
                report.note_position_conflicts.append({
                    "product": source.name,
                    "parfumly_slug": source.external_slug,
                    "note": conflict.name,
                    "positions": list(conflict.positions),
                    "kept": conflict.kept,
                })

            reason = self._invalid_reason(source, brand_slug)
            if reason:
                report.skipped.append(self._skip(source.name, source.external_slug, reason))
                continue
            report.valid_products += 1

            if self._is_mixed(source):
                report.mixed_concentration_category.append({
                    "name": source.name,
                    "parfumly_slug": source.external_slug,
                    "concentrations": list(source.concentrations),
                })
                report.skipped.append(self._skip(
                    source.name, source.external_slug,
                    "mixed ATTAR and perfume concentrations; manual review",
                ))
                continue
            is_attar = source.concentrations == ["attar"]
            routed = self.attar_category if is_attar else self.category

            key = match_key(source.name)
            product = existing_products.get(key)
            if product is None:
                if not self._check_new_product(source, key, accepted, brand):
                    continue
                if routed is None:
                    report.skipped.append(self._skip(
                        source.name, source.external_slug,
                        "ATTAR-only product needs --attar-category",
                    ))
                    continue
            elif routed is not None and product.category_id != routed.pk:
                report.warnings.append(
                    f"{product.name}: existing category kept; routing would give "
                    f"{routed.slug!r}"
                )

            product_plan = self._plan_product(source, product, notes_by_key, plan)
            product_plan.category = routed if product is None else None
            if not product_plan.editions:
                report.skipped.append(self._skip(
                    source.name, source.external_slug, "no edition can be created or matched",
                ))
                continue

            if product is None:
                accepted[key] = (source.name, source.external_slug, "parfumly")
                report.products_to_create.append(f"{source.name} ({source.external_slug})")
            else:
                report.existing_products.append(f"{product.name} ({product.slug})")
            if source.retail_sizes:
                report.retail_sizes[source.name] = [str(s) for s in source.retail_sizes]
            (report.attars if is_attar else report.perfumes).append(source.name)
            plan.products.append(product_plan)

        report.notes_to_create = sorted(plan.new_notes.values())
        return plan

    def _resolve_brand(self, brand_name, brand_slug, summary):
        brand = (
            Brand.objects.filter(slug=brand_slug).first()
            or Brand.objects.filter(name__iexact=brand_name).first()
        )
        if brand is not None:
            self.report.brand_action = "existing"
            return brand, {}

        tier = clean_name((summary.get("brand") or {}).get("tier")).casefold()
        category = BRAND_TIER_MAP.get(tier)
        if category is None:
            raise ImportAbort(
                f"Brand {brand_name!r} does not exist and Parfumly tier {tier!r} has no "
                "known brand category. Create the brand in admin first."
            )
        self.report.brand_action = "create"
        name = clean_name((summary.get("brand") or {}).get("name")) or brand_name
        return None, {"name": name, "slug": brand_slug, "brand_category": category}

    def _invalid_reason(self, source, brand_slug):
        if not source.name or not source.external_slug:
            return "missing name or slug"
        if source.brand_slug != brand_slug:
            return f"brand {source.brand_slug!r} does not match"
        if source.gender is None:
            return "unknown gender"
        if not source.concentrations:
            return "no supported bottle/tester concentration"
        return None

    @staticmethod
    def _is_mixed(source):
        concentrations = set(source.concentrations)
        return "attar" in concentrations and len(concentrations) > 1

    def _check_new_product(self, source, key, accepted, brand):
        """
        Duplicate policy for a product that does not exist yet.

        Returns False (and reports) when the product must not be created:
        an exact or strongly aliased duplicate, or a slug conflict. Fuzzy
        similarity alone is reported but never blocks creation.
        """
        report = self.report

        def skip_duplicate(rule, name, reference, origin):
            report.duplicates_skipped.append({
                "name": source.name,
                "parfumly_slug": source.external_slug,
                "rule": rule,
                "matched_name": name,
                "matched_slug": reference,
                "matched_source": origin,
            })
            report.skipped.append(self._skip(
                source.name, source.external_slug, f"{rule} duplicate of {name!r}",
            ))
            return False

        if key in accepted:
            return skip_duplicate("exact", *accepted[key])
        for name, reference, origin in accepted.values():
            if is_strong_duplicate(source.name, name):
                return skip_duplicate("strong", name, reference, origin)

        slug_owner = Product.objects.filter(slug=source.external_slug).first()
        if slug_owner is not None:
            if brand is not None and slug_owner.brand_id == brand.pk:
                return skip_duplicate("same slug", slug_owner.name, slug_owner.slug, "smerfume")
            report.skipped.append(self._skip(
                source.name, source.external_slug,
                "product slug already used by a product of another brand",
            ))
            return False

        for name, reference, origin in accepted.values():
            if is_possible_duplicate(source.name, name):
                report.possible_duplicates.append({
                    "name": source.name,
                    "parfumly_slug": source.external_slug,
                    "similar_name": name,
                    "similar_slug": reference,
                    "similar_source": origin,
                })
        return True

    def _plan_product(self, source, product, notes_by_key, plan):
        report = self.report
        product_plan = ProductPlan(source=source, product=product)
        label = product.name if product else source.name

        new_concentrations = []
        for concentration in source.concentrations:
            edition, ambiguous = match_edition(product, concentration)
            if ambiguous:
                report.warnings.append(
                    f"{label}: several existing {concentration} editions; edition skipped"
                )
                continue
            edition_plan = EditionPlan(concentration=concentration, edition=edition)
            if edition is None:
                new_concentrations.append(edition_plan)
            else:
                report.existing_editions.append(f"{label} [{concentration}]")
                if edition.release_year is None and source.release_year is not None:
                    edition_plan.fill_release_year = True
                    report.release_years_to_fill.append(f"{label} [{concentration}]")
            product_plan.editions.append(edition_plan)

        if new_concentrations:
            existing_count = product.editions.count() if product else 0
            needs_names = existing_count + len(new_concentrations) > 1
            existing_slugs = set(
                product.editions.exclude(slug=None).values_list("slug", flat=True)
            ) if product else set()
            for edition_plan in list(new_concentrations):
                if needs_names:
                    edition_plan.name = CONCENTRATION_LABELS[edition_plan.concentration]
                    edition_plan.slug = edition_plan.concentration
                    if edition_plan.slug in existing_slugs:
                        report.warnings.append(
                            f"{label}: edition slug {edition_plan.slug!r} already used; edition skipped"
                        )
                        product_plan.editions.remove(edition_plan)
                        continue
                report.editions_to_create.append(f"{label} [{edition_plan.concentration}]")

        for edition_plan in product_plan.editions:
            linked = {}
            if edition_plan.edition is not None:
                linked = {
                    match_key(name): position
                    for name, position in edition_plan.edition.edition_notes.values_list(
                        "note__name", "position"
                    )
                }
            for note in source.notes:
                key = match_key(note.name)
                if key in linked:
                    report.existing_note_links += 1
                    if linked[key] != note.position:
                        report.warnings.append(
                            f"{label} [{edition_plan.concentration}]: existing note "
                            f"{note.name!r} kept as {linked[key]}; Parfumly lists {note.position}"
                        )
                    continue
                if key not in notes_by_key and key not in plan.new_notes:
                    self._report_note_duplicates(note.name, notes_by_key, plan.new_notes)
                    plan.new_notes[key] = note.name
                edition_plan.note_links.append((key, note.position))
                report.note_links_to_create += 1
        return product_plan

    def _load_notes(self, report_anomalies=True):
        """
        Map note key -> existing PerfumeNote.

        When several stored notes share a key (e.g. an accidental trailing
        space), the one whose stored name is already clean is canonical,
        else the oldest. Stored notes are never modified; the anomaly is
        reported instead.
        """
        notes_by_key = {}
        groups = {}
        for note in PerfumeNote.objects.order_by("id"):
            key = match_key(note.name)
            current = notes_by_key.get(key)
            if current is None:
                notes_by_key[key] = note
                continue
            groups.setdefault(key, [current]).append(note)
            if current.name != clean_name(current.name) and note.name == clean_name(note.name):
                notes_by_key[key] = note
        if report_anomalies:
            for key, group in groups.items():
                names = ", ".join(repr(n.name) for n in group)
                self.report.warnings.append(
                    f"stored notes {names} differ only by case/whitespace; using "
                    f"{notes_by_key[key].name!r} (stored data not modified)"
                )
        return notes_by_key

    def _report_note_duplicates(self, name, notes_by_key, new_notes):
        candidates = [n.name for n in notes_by_key.values()] + list(new_notes.values())
        for other in candidates:
            if is_possible_duplicate(name, other):
                self.report.possible_note_duplicates.append(
                    {"new_note": name, "similar_to": other}
                )

    @staticmethod
    def _skip(name, slug, reason):
        return {"name": clean_name(name), "parfumly_slug": slug, "reason": reason}

    # ── Applying (writes) ───────────────────────────────────────────────

    def apply(self, plan):
        brand = plan.brand
        if brand is None:
            brand = Brand.objects.create(**plan.brand_values)

        notes_by_key = self._load_notes(report_anomalies=False)
        for product_plan in plan.products:
            source = product_plan.source
            try:
                with transaction.atomic():
                    self._apply_product(brand, product_plan, plan.new_notes, notes_by_key)
            except Exception as exc:
                logger.exception("Parfumly import failed for %s", source.external_slug)
                self.report.errors.append(f"{source.external_slug}: {type(exc).__name__}")
                # Notes created inside the rolled-back transaction no longer exist.
                notes_by_key = self._load_notes(report_anomalies=False)
        return brand

    def _apply_product(self, brand, product_plan, new_notes, notes_by_key):
        source = product_plan.source
        product = product_plan.product
        if product is None:
            product = Product.objects.create(
                name=source.name,
                slug=source.external_slug,
                brand=brand,
                category=product_plan.category,
            )

        for edition_plan in product_plan.editions:
            edition = edition_plan.edition
            if edition is None:
                edition = ProductEdition.objects.create(
                    product=product,
                    name=edition_plan.name,
                    slug=edition_plan.slug,
                    gender=source.gender,
                    concentration=edition_plan.concentration,
                    release_year=source.release_year,
                )
            elif edition_plan.fill_release_year:
                # Conditional update: never overwrites a value set meanwhile.
                ProductEdition.objects.filter(pk=edition.pk, release_year=None).update(
                    release_year=source.release_year
                )

            for key, position in edition_plan.note_links:
                note = notes_by_key.get(key)
                if note is None:
                    note = PerfumeNote.objects.create(name=new_notes[key])
                    notes_by_key[key] = note
                EditionNote.objects.get_or_create(
                    edition=edition, note=note, defaults={"position": position}
                )
