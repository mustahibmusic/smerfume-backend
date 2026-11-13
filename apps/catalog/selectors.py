"""Read-only catalogue lookups shared by the Smerfume Panel workflows."""

from apps.catalog.models import ProductVariant

SCAN_CODE_MAX_LENGTH = 64


def resolve_scan(code, queryset=None):
    """The variant a scanned or typed code identifies, or None.

    Exact matches only, in this order: barcode, SKU as typed, SKU in upper
    case (canonical SKUs are upper case). Never a partial or
    case-insensitive match, so the unique indexes on barcode and SKU serve
    every step.

    This only identifies the variant: inactive and decant variants are
    returned too. Each workflow decides, and re-checks on submit, whether
    the variant may be sold, ordered or received.
    """
    code = (code or "").strip()
    if not code or len(code) > SCAN_CODE_MAX_LENGTH:
        return None
    variants = ProductVariant.objects.all() if queryset is None else queryset
    variants = variants.select_related("edition__product__brand")
    lookups = [{"barcode": code}, {"sku": code}]
    if code.upper() != code:
        lookups.append({"sku": code.upper()})
    for lookup in lookups:
        variant = variants.filter(**lookup).first()
        if variant is not None:
            return variant
    return None
