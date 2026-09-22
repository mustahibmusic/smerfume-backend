"""
Pure normalization of raw Parfumly JSON into Smerfume-shaped values.

No database access and no network access. Everything the importer is allowed
to persist passes through here; everything else (seller offers, prices,
stock, availability, MRP, descriptions, images, tags, accords, decant /
sample / gift-set listings) is dropped by simply never being read.
"""

import datetime
import difflib
import re
from dataclasses import dataclass, field

GENDER_MAP = {
    "MASCULINE": "men",
    "FEMININE": "women",
    "UNISEX": "unisex",
}

CONCENTRATION_MAP = {
    "EDC": "edc",
    "EDT": "edt",
    "EDP": "edp",
    "EXTRAIT": "extrait",
    "ATTAR": "attar",
}

# Only real retail units are evidence of a concentration and are reported to
# staff as candidate Smerfume SKUs. Seller decants, samples and gift sets are
# marketplace listings, not fragrance facts.
RETAIL_FORMS = {"BOTTLE", "TESTER"}

NOTE_POSITIONS = ("top", "heart", "base")

MIN_RELEASE_YEAR = 1900

NEAR_DUPLICATE_RATIO = 0.85


@dataclass(frozen=True)
class RetailSize:
    concentration: str  # raw Parfumly value, e.g. "EDP"
    size_ml: int
    form: str  # "BOTTLE" or "TESTER"

    def __str__(self):
        return f"{self.concentration} {self.size_ml}ml {self.form}"


@dataclass(frozen=True)
class NormalizedNote:
    name: str
    position: str  # "top" | "heart" | "base"


@dataclass(frozen=True)
class NotePositionConflict:
    """One note listed by Parfumly in more than one pyramid position."""

    name: str
    positions: tuple  # every source position, in source order
    kept: str  # position actually imported (the first one)


@dataclass
class NormalizedProduct:
    external_slug: str
    name: str
    brand_slug: str
    gender: str | None
    release_year: int | None
    concentrations: list[str]  # Smerfume codes, in first-seen order
    notes: list[NormalizedNote]
    retail_sizes: list[RetailSize]
    warnings: list[str] = field(default_factory=list)
    note_position_conflicts: list[NotePositionConflict] = field(default_factory=list)


def clean_name(value):
    """Trim and collapse internal whitespace. No other changes."""
    return " ".join(str(value or "").split())


def match_key(value):
    """Exact, case-insensitive, whitespace-normalized comparison key."""
    return clean_name(value).casefold()


def normalize_release_year(value, current_year=None):
    """Return a plausible year, or None for missing/0/invalid/implausible values."""
    if isinstance(value, bool):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != year:
        return None
    current_year = current_year or datetime.date.today().year
    if MIN_RELEASE_YEAR <= year <= current_year + 1:
        return year
    return None


def compact_key(value):
    """match_key() with spaces and punctuation removed."""
    return re.sub(r"[^0-9a-z]", "", match_key(value))


def is_strong_duplicate(a, b):
    """
    Strong alias evidence: not an exact key match, but identical once
    spaces and punctuation are ignored ("Oud Rose" / "Oud-Rose",
    "Oak Moss" / "Oakmoss").
    """
    key_a, key_b = match_key(a), match_key(b)
    if not key_a or not key_b or key_a == key_b:
        return False
    return bool(compact_key(a)) and compact_key(a) == compact_key(b)


def is_possible_duplicate(a, b, threshold=NEAR_DUPLICATE_RATIO):
    """
    Heuristic similarity flag for staff review — never used to merge or
    block records.

    True for strong duplicates (see is_strong_duplicate) and for names that
    are merely similar ("Blu" / "Blue", "Blush Noir" / "Blush Noire") or
    where one multi-word name is a whole-word prefix of the other
    ("Bin Shaikh" / "Bin Shaikh Made In Uae").
    """
    key_a, key_b = match_key(a), match_key(b)
    if not key_a or not key_b or key_a == key_b:
        return False
    if is_strong_duplicate(a, b):
        return True
    if difflib.SequenceMatcher(None, key_a, key_b).ratio() >= threshold:
        return True
    words_a, words_b = key_a.split(), key_b.split()
    shorter, longer = sorted((words_a, words_b), key=len)
    return len(shorter) >= 2 and longer[: len(shorter)] == shorter


def normalize_product(detail):
    """Normalize one GET /products/{slug} document."""
    warnings = []
    name = clean_name(detail.get("name"))
    external_slug = clean_name(detail.get("slug"))
    brand_slug = clean_name((detail.get("brand") or {}).get("slug"))

    raw_gender = detail.get("gender")
    gender = GENDER_MAP.get(raw_gender)
    if gender is None:
        warnings.append(f"unknown gender {raw_gender!r}")

    raw_year = detail.get("year")
    release_year = normalize_release_year(raw_year)
    if raw_year not in (None, 0) and release_year is None:
        warnings.append(f"implausible release year {raw_year!r} ignored")

    concentrations = []
    retail_sizes = []
    unknown = set()
    for variant in detail.get("variants") or []:
        if variant.get("form") not in RETAIL_FORMS:
            continue
        raw_conc = clean_name(variant.get("concentration")).upper()
        code = CONCENTRATION_MAP.get(raw_conc)
        if code is None:
            unknown.add(raw_conc or "<missing>")
            continue
        if code not in concentrations:
            concentrations.append(code)
        size = variant.get("sizeMl")
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            retail = RetailSize(raw_conc, size, variant["form"])
            if retail not in retail_sizes:
                retail_sizes.append(retail)
    for raw_conc in sorted(unknown):
        warnings.append(f"unsupported concentration {raw_conc!r} skipped")

    notes = []
    seen = {}  # note key -> (display name, [positions in source order])
    raw_notes = detail.get("notes")
    if isinstance(raw_notes, dict):
        for position, entries in raw_notes.items():
            if position not in NOTE_POSITIONS:
                warnings.append(f"unknown note position {position!r} skipped")
                continue
            for entry in entries or []:
                note_name = clean_name((entry or {}).get("name"))
                if not note_name:
                    continue
                key = match_key(note_name)
                if key in seen:
                    seen[key][1].append(position)
                    continue
                seen[key] = (note_name, [position])
                notes.append(NormalizedNote(note_name, position))
    elif raw_notes:
        warnings.append("notes without positions skipped")

    # EditionNote allows one position per note: the first position is kept
    # and every conflict is surfaced rather than silently dropped.
    note_position_conflicts = [
        NotePositionConflict(note_name, tuple(dict.fromkeys(positions)), positions[0])
        for note_name, positions in seen.values()
        if len(set(positions)) > 1
    ]

    return NormalizedProduct(
        external_slug=external_slug,
        name=name,
        brand_slug=brand_slug,
        gender=gender,
        release_year=release_year,
        concentrations=concentrations,
        notes=notes,
        retail_sizes=retail_sizes,
        warnings=warnings,
        note_position_conflicts=note_position_conflicts,
    )
