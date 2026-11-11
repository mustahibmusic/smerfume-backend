"""Vendor (Supplier) master data: the write path for creating and editing
vendors outside Django Admin. Vendors are deactivated, never deleted, so
stock and purchase history keep their links. The accounting sync field
(external_accounting_id) is never set here."""

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import Supplier

EDITABLE_FIELDS = (
    "name", "legal_name", "is_active", "contact_person", "phone", "email",
    "address_line1", "address_line2", "city", "state", "state_code", "pincode", "country",
    "gstin", "pan", "gst_treatment", "payment_terms_days", "notes",
)


class SupplierError(Exception):
    """A vendor cannot be saved (message is user-facing)."""


def _save(supplier):
    for field in ("name", "legal_name", "contact_person", "phone", "email", "city", "state"):
        setattr(supplier, field, (getattr(supplier, field) or "").strip())
    try:
        supplier.full_clean()
    except ValidationError as exc:
        raise SupplierError(" ".join(exc.messages)) from exc
    supplier.save()
    return supplier


def _check_fields(fields):
    extra = set(fields) - set(EDITABLE_FIELDS)
    if extra:
        raise SupplierError(f"Unknown fields: {', '.join(sorted(extra))}.")


@transaction.atomic
def create_supplier(*, user, **fields):
    _check_fields(fields)
    return _save(Supplier(**fields))


@transaction.atomic
def update_supplier(supplier, *, user, **fields):
    _check_fields(fields)
    supplier = Supplier.objects.select_for_update().get(pk=supplier.pk)
    for field, value in fields.items():
        setattr(supplier, field, value)
    return _save(supplier)
