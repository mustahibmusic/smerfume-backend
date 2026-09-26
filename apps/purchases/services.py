"""
Purchase order and goods receipt workflow. The only legitimate way to
change PurchaseOrder.status or GoodsReceipt.status. Every operation locks
its rows, checks the move and stamps who/when.

Lock order, always: PurchaseOrder -> GoodsReceipt -> ReceiptDiscrepancy
-> PurchaseOrderLine (by id) -> InventoryStock (by variant, stock type).
Only posting a goods receipt changes inventory; discrepancies never do.
"""

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.inventory.services.receipt import receive_purchased_stock

from . import selectors
from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptDiscrepancy,
)


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
    # Lock only the line rows, not the joined variants (see posting).
    lines = list(po.lines.select_for_update(of=("self",)).select_related("variant"))
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
    """Draft or issued -> cancelled. A reason is required. Blocked once any
    goods receipt against the PO has been posted."""
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseOrderError("A cancellation reason is required.")
    po = _lock(po)
    _check_transition(po, PurchaseOrder.STATUS_CANCELLED)
    if selectors.po_has_posted_receipts(po):
        raise PurchaseOrderError(
            f"{po.po_number} has posted goods receipts and cannot be cancelled."
        )

    po.status = PurchaseOrder.STATUS_CANCELLED
    po.cancelled_by = user
    po.cancelled_at = timezone.now()
    po.cancel_reason = reason
    po.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return po


# --- Goods receipts ---


class GoodsReceiptError(Exception):
    """Raised when a goods receipt operation is not allowed."""


class GoodsReceiptAlreadyPosted(GoodsReceiptError):
    """Posting a receipt that is already posted (e.g. a double click)."""


def refresh_po_receipt_status(po):
    """Set issued / partially_received / received from posted receipts.
    The caller must hold the PO lock. Closed and cancelled POs are left
    alone. A missing (short) unit never counts as received."""
    if po.status not in PurchaseOrder.RECEIVABLE_STATUSES:
        return po
    lines = list(po.lines.all())
    received = selectors.received_quantities(line.pk for line in lines)
    total_received = sum(received.values())
    if lines and all(received.get(line.pk, 0) >= line.quantity_ordered for line in lines):
        new_status = PurchaseOrder.STATUS_RECEIVED
    elif total_received > 0:
        new_status = PurchaseOrder.STATUS_PARTIALLY_RECEIVED
    else:
        new_status = PurchaseOrder.STATUS_ISSUED
    if new_status != po.status:
        _check_transition(po, new_status)
        po.status = new_status
        po.save(update_fields=["status", "updated_at"])
    return po


@transaction.atomic
def create_receipt_from_po(po, user, received_date=None, vendor_document_reference=""):
    """Start an empty draft receipt for an issued or partially received PO.
    Staff then enter only the quantities that physically arrived; nothing
    is assumed received."""
    po = _lock(po)
    if po.status not in PurchaseOrder.RECEIVABLE_STATUSES:
        raise GoodsReceiptError(
            f"{po.po_number} is {po.get_status_display().lower()}; goods can only be "
            "received against an issued or partially received purchase order."
        )
    receipt = GoodsReceipt(
        receipt_type=GoodsReceipt.TYPE_STANDARD,
        supplier_id=po.supplier_id,
        warehouse_id=po.warehouse_id,
        purchase_order=po,
        vendor_document_reference=(vendor_document_reference or "").strip(),
        created_by=user,
    )
    if received_date:
        receipt.received_date = received_date
    receipt.save()
    return receipt


def _receipt_problems(receipt, po, lines, po_lines):
    problems = []
    if receipt.receipt_type != GoodsReceipt.TYPE_STANDARD:
        problems.append("Only standard purchase order receipts can be posted.")
    if po.status not in PurchaseOrder.RECEIVABLE_STATUSES:
        problems.append(f"{po.po_number} is {po.get_status_display().lower()}.")
    if receipt.supplier_id != po.supplier_id:
        problems.append("The receipt vendor does not match the purchase order.")
    if receipt.warehouse_id != po.warehouse_id:
        problems.append("The receipt warehouse does not match the purchase order.")
    if not lines:
        problems.append("Add at least one received line before posting.")
    allowed_types = {value for value, _ in GoodsReceiptLine.STOCK_TYPE_CHOICES}
    for number, line in enumerate(lines, start=1):
        po_line = po_lines.get(line.po_line_id)
        if po_line is None:
            problems.append(f"Line {number}: choose a line from {po.po_number}.")
            continue
        if line.quantity <= 0:
            problems.append(f"Line {number}: quantity must be more than 0.")
        if line.stock_type not in allowed_types:
            problems.append(f"Line {number}: stock type must be retail or damaged.")
    return problems


def _discrepancy_problems(po, discrepancies, po_lines):
    """Staff-recorded observations on a draft: short / wrong_item / excess.
    Damaged discrepancies are created by posting, never entered."""
    problems = []
    for number, discrepancy in enumerate(discrepancies, start=1):
        label = f"Discrepancy {number}"
        if discrepancy.discrepancy_type not in ReceiptDiscrepancy.STAFF_TYPES:
            problems.append(f"{label}: damaged stock is recorded as a damaged receipt line.")
            continue
        if discrepancy.po_line_id and discrepancy.po_line_id not in po_lines:
            problems.append(f"{label}: choose a line from {po.po_number}.")
            continue
        try:
            discrepancy.clean()
        except ValidationError as exc:
            problems.extend(f"{label}: {message}" for message in exc.messages)
    return problems


def _over_receipt_problems(lines, po_lines, discrepancies=()):
    """Received units may never exceed the ordered quantity. A recorded
    shortage must also fit in what is still outstanding after this
    receipt. Excess and wrong-item records never count as received."""
    incoming = defaultdict(int)
    for line in lines:
        incoming[line.po_line_id] += line.quantity
    short = defaultdict(int)
    for discrepancy in discrepancies:
        if discrepancy.discrepancy_type == ReceiptDiscrepancy.TYPE_SHORT:
            short[discrepancy.po_line_id] += discrepancy.quantity
    already = selectors.received_quantities(set(incoming) | set(short))
    problems = []
    for po_line_id, quantity in incoming.items():
        po_line = po_lines[po_line_id]
        received = already.get(po_line_id, 0)
        if received + quantity > po_line.quantity_ordered:
            problems.append(
                f"{po_line.variant.sku}: ordered {po_line.quantity_ordered}, already received "
                f"{received}, this receipt {quantity}. Over-receipt is not allowed."
            )
    for po_line_id, quantity in short.items():
        po_line = po_lines[po_line_id]
        remaining = (
            po_line.quantity_ordered - already.get(po_line_id, 0) - incoming.get(po_line_id, 0)
        )
        if quantity > remaining:
            problems.append(
                f"{po_line.variant.sku}: short {quantity} recorded but only {max(remaining, 0)} "
                "is still outstanding after this receipt."
            )
    return problems


@transaction.atomic
def post_goods_receipt(receipt, user):
    """Draft -> posted. Freezes each line's cost from its PO line, adds
    the units to stock through one StockTransaction, then updates the PO
    status. All or nothing; posting twice is refused."""
    po_id = GoodsReceipt.objects.values_list("purchase_order_id", flat=True).get(pk=receipt.pk)
    if po_id is None:
        raise GoodsReceiptError("Only purchase order receipts can be posted.")
    po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status == GoodsReceipt.STATUS_POSTED:
        raise GoodsReceiptAlreadyPosted(f"{receipt.grn_number} is already posted.")
    if receipt.status != GoodsReceipt.STATUS_DRAFT or receipt.purchase_order_id != po.pk:
        raise GoodsReceiptError(f"{receipt.grn_number} is not a draft and cannot be posted.")

    lines = list(receipt.lines.order_by("pk"))
    discrepancies = list(receipt.discrepancies.select_for_update(of=("self",)).order_by("pk"))
    po_line_ids = sorted(
        {line.po_line_id for line in lines if line.po_line_id}
        | {d.po_line_id for d in discrepancies if d.po_line_id}
    )
    # of=("self",): lock only the PO line rows. Without it the joined
    # ProductVariant rows are locked FOR UPDATE too, which deadlocks with
    # checkout (it holds the stock row, then inserts rows referencing the
    # variant).
    po_lines = {
        po_line.pk: po_line
        for po_line in PurchaseOrderLine.objects.select_for_update(of=("self",))
        .filter(pk__in=po_line_ids, purchase_order=po)
        .select_related("variant")
        .order_by("pk")
    }
    problems = _receipt_problems(receipt, po, lines, po_lines)
    problems += _discrepancy_problems(po, discrepancies, po_lines)
    if not problems:
        problems = _over_receipt_problems(lines, po_lines, discrepancies)
    if problems:
        raise GoodsReceiptError(" ".join(problems))

    # Freeze the authoritative cost, tax rate and variant from the locked
    # PO line; never trust draft values.
    for line in lines:
        po_line = po_lines[line.po_line_id]
        po_line.purchase_order = po
        line.po_line = po_line
        line.apply_po_line_snapshot()
        if line.unit_cost is None:
            raise GoodsReceiptError(f"{po_line.variant.sku}: unit cost cannot be worked out.")
    now = timezone.now()
    for line in lines:
        line.updated_at = now
    GoodsReceiptLine.objects.bulk_update(lines, ["variant", "unit_cost", "tax_rate", "updated_at"])

    stock_transaction = receive_purchased_stock(
        warehouse=receipt.warehouse,
        supplier=receipt.supplier,
        lines=lines,
        performed_by=user,
        notes=f"Goods receipt {receipt.grn_number} for {po.po_number}",
    )

    receipt.status = GoodsReceipt.STATUS_POSTED
    receipt.stock_transaction = stock_transaction
    receipt.posted_by = user
    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["status", "stock_transaction", "posted_by", "posted_at", "updated_at"])

    # Damaged units arrived and count as received; record them for
    # follow-up. One per damaged line (unique constraint); a receipt posts
    # only once, so retries never duplicate them.
    ReceiptDiscrepancy.objects.bulk_create([
        ReceiptDiscrepancy(
            receipt=receipt, po_line_id=line.po_line_id, receipt_line=line,
            discrepancy_type=ReceiptDiscrepancy.TYPE_DAMAGED,
            quantity=line.quantity, created_by=user,
        )
        for line in lines
        if line.stock_type == "damaged"
    ])

    refresh_po_receipt_status(po)
    return receipt


@transaction.atomic
def cancel_goods_receipt(receipt, user, reason):
    """Draft -> cancelled. Posted receipts are never cancelled."""
    reason = (reason or "").strip()
    if not reason:
        raise GoodsReceiptError("A cancellation reason is required.")
    po_id = GoodsReceipt.objects.values_list("purchase_order_id", flat=True).get(pk=receipt.pk)
    if po_id is not None:
        PurchaseOrder.objects.select_for_update().get(pk=po_id)
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status != GoodsReceipt.STATUS_DRAFT:
        raise GoodsReceiptError(
            f"{receipt.grn_number} is {receipt.get_status_display().lower()} and cannot be cancelled."
        )
    receipt.status = GoodsReceipt.STATUS_CANCELLED
    receipt.cancelled_by = user
    receipt.cancelled_at = timezone.now()
    receipt.cancel_reason = reason
    receipt.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancel_reason", "updated_at"])
    return receipt


@transaction.atomic
def close_purchase_order(po, user, reason):
    """Partially received -> closed: Smerfume stops waiting for the rest.
    The unreceived quantity is derived (ordered minus received), not
    written as another short discrepancy. A closed PO receives nothing
    more and cannot be cancelled."""
    reason = (reason or "").strip()
    if not reason:
        raise PurchaseOrderError("A reason for closing is required.")
    po = _lock(po)
    _check_transition(po, PurchaseOrder.STATUS_CLOSED)
    po.status = PurchaseOrder.STATUS_CLOSED
    po.closed_by = user
    po.closed_at = timezone.now()
    po.close_reason = reason
    po.save(update_fields=["status", "closed_by", "closed_at", "close_reason", "updated_at"])
    return po


# --- Receipt discrepancies ---


class ReceiptDiscrepancyError(Exception):
    """Raised when a discrepancy cannot be resolved."""


@transaction.atomic
def resolve_discrepancy(discrepancy, user, resolution, notes=""):
    """Open -> resolved, once. Operational only: no stock, receipt or
    accounting change."""
    valid = {value for value, _ in ReceiptDiscrepancy.RESOLUTION_CHOICES}
    if resolution not in valid:
        raise ReceiptDiscrepancyError("Choose a valid resolution.")
    discrepancy = (
        ReceiptDiscrepancy.objects.select_for_update(of=("self",))
        .select_related("receipt")
        .get(pk=discrepancy.pk)
    )
    if discrepancy.receipt.status != GoodsReceipt.STATUS_POSTED:
        raise ReceiptDiscrepancyError("Only discrepancies on posted goods receipts can be resolved.")
    if discrepancy.status != ReceiptDiscrepancy.STATUS_OPEN:
        raise ReceiptDiscrepancyError("This discrepancy is already resolved.")
    discrepancy.status = ReceiptDiscrepancy.STATUS_RESOLVED
    discrepancy.resolution = resolution
    discrepancy.resolution_notes = (notes or "").strip()
    discrepancy.resolved_by = user
    discrepancy.resolved_at = timezone.now()
    discrepancy.save(update_fields=[
        "status", "resolution", "resolution_notes", "resolved_by", "resolved_at", "updated_at",
    ])
    return discrepancy
