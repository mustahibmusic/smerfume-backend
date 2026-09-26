"""
Purchase order workflow. The only legitimate way to change
PurchaseOrder.status. Every operation locks the PO row, checks the move
against PurchaseOrder.ALLOWED_TRANSITIONS and stamps who/when.

Nothing here touches inventory: stock only moves when a goods receipt is
posted. Receipt-derived statuses (partially_received, received) and
closing are added with goods receipts; their services must lock the PO
row first, then its lines, in the same order as below.
"""

from django.db import transaction
from django.utils import timezone

from .models import PurchaseOrder


class PurchaseOrderError(Exception):
    """Raised when a purchase order operation is not allowed."""


def _lock(po):
    return PurchaseOrder.objects.select_for_update().get(pk=po.pk)


def _check_transition(po, new_status):
    if not po.can_transition_to(new_status):
        raise PurchaseOrderError(
            f"{po.po_number} cannot move from {po.get_status_display()} to "
            f"{dict(PurchaseOrder.STATUS_CHOICES)[new_status]}."
        )


def _issue_problems(po, lines):
    """Everything that blocks issuing, as readable messages."""
    problems = []
    if not lines:
        problems.append("Add at least one line before issuing.")
    if not po.supplier.is_active:
        problems.append(f"Vendor {po.supplier} is inactive.")
    if not po.warehouse.is_active:
        problems.append(f"Warehouse {po.warehouse} is inactive.")
    if po.expected_date and po.expected_date < po.order_date:
        problems.append("Expected date cannot be before the order date.")
    for number, line in enumerate(lines, start=1):
        label = f"Line {number} ({line.variant.sku})"
        if not line.variant.is_active:
            problems.append(f"{label}: variant is inactive.")
        if line.variant.is_decant:
            problems.append(f"{label}: decants cannot be purchased.")
        if po.amounts_include_tax and line.tax_rate is None:
            problems.append(f"{label}: tax rate is required because prices include tax.")
    return problems


@transaction.atomic
def issue_purchase_order(po, user):
    """Draft -> issued. Validates every line and the vendor/warehouse at
    this moment; later deactivation never invalidates an issued PO."""
    po = _lock(po)
    _check_transition(po, PurchaseOrder.STATUS_ISSUED)
    lines = list(po.lines.select_for_update().select_related("variant"))
    problems = _issue_problems(po, lines)
    if problems:
        raise PurchaseOrderError(" ".join(problems))

    po.status = PurchaseOrder.STATUS_ISSUED
    po.issued_by = user
    po.issued_at = timezone.now()
    po.save(update_fields=["status", "issued_by", "issued_at", "updated_at"])
    return po


@transaction.atomic
def cancel_purchase_order(po, user, reason):
    """Draft or issued -> cancelled. A reason is required. Goods receipts
    will add a check that nothing has been received yet."""
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseOrderError("A cancellation reason is required.")
    po = _lock(po)
    _check_transition(po, PurchaseOrder.STATUS_CANCELLED)

    po.status = PurchaseOrder.STATUS_CANCELLED
    po.cancelled_by = user
    po.cancelled_at = timezone.now()
    po.cancel_reason = reason
    po.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return po
