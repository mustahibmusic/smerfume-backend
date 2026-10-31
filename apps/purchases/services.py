"""
Purchase order and goods receipt workflow. The only legitimate way to
change PurchaseOrder.status or GoodsReceipt.status. Every operation locks
its rows, checks the move and stamps who/when.

Lock order, always: PurchaseOrder -> GoodsReceipt -> ReceiptDiscrepancy
-> PurchaseOrderLine (by id) -> StockReservation (by id)
-> StockReservationAllocation (by id) -> InventoryStock (by variant, stock type).
Only posting a goods receipt (or its reversal) changes inventory;
discrepancies never do.
"""

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.inventory.models import InventoryStock
from apps.inventory.services import reservation as reservation_service
from apps.inventory.services.receipt import (
    PurchaseReversalStockError,
    receive_opening_stock,
    receive_purchased_stock,
    reverse_purchased_stock,
)

from . import selectors
from .models import (
    BookedQuantityChange,
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentAccount,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderPayment,
    ReceiptDiscrepancy,
)


class PurchaseOrderError(Exception):
    """Raised when a purchase order operation is not allowed."""


def _lock(po):
    return PurchaseOrder.objects.select_for_update().get(pk=po.pk)


def _refuse_active_incoming(po, action):
    """Close/cancel guard (spec §5.7). Locks every line of the PO by id
    after the PO lock: checkout needs a line lock to add an incoming
    allocation and re-reads PO status after it, so either it commits first
    and is seen here, or it sees the new status and skips the line."""
    list(po.lines.select_for_update().order_by("pk").values_list("pk", flat=True))
    count = selectors.active_incoming_allocations(po).count()
    if count:
        raise PurchaseOrderError(
            f"{po.po_number} cannot be {action}: {count} customer order allocation(s) are "
            "awaiting this purchase order. Reallocate them or cancel those orders first."
        )


def _check_transition(po, new_status):
    if not po.can_transition_to(new_status):
        raise PurchaseOrderError(
            f"{po.po_number} cannot move from {po.get_status_display()} to "
            f"{dict(PurchaseOrder.STATUS_CHOICES)[new_status]}."
        )


def _tax_treatment_problems(treatment, amounts_include_tax, lines):
    """DEC-010 line rules for a purchase tax treatment."""
    problems = []
    if treatment == PurchaseOrder.TAX_NO_GST_INVOICE and amounts_include_tax:
        problems.append("A no-GST-invoice purchase cannot include tax: prices must exclude tax.")
    for number, line in enumerate(lines, start=1):
        label = f"Line {number} ({line.variant.sku})"
        if treatment == PurchaseOrder.TAX_GST_INVOICE and line.tax_rate is None:
            problems.append(f"{label}: tax rate is required for a GST-invoice purchase.")
        if treatment == PurchaseOrder.TAX_NO_GST_INVOICE and line.tax_rate is not None:
            problems.append(f"{label}: a no-GST-invoice purchase must have no tax rate.")
    return problems


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
    # DEC-010: the purchase tax treatment is chosen explicitly per PO.
    if po.purchase_tax_treatment is None:
        problems.append("Choose the purchase tax treatment (GST invoice or no GST invoice).")
    for number, line in enumerate(lines, start=1):
        label = f"Line {number} ({line.variant.sku})"
        if not line.variant.is_active:
            problems.append(f"{label}: variant is inactive.")
        if line.variant.is_decant:
            problems.append(f"{label}: decants cannot be purchased.")
    if po.purchase_tax_treatment is not None:
        problems += _tax_treatment_problems(po.purchase_tax_treatment, po.amounts_include_tax, lines)
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
    _refuse_active_incoming(po, "cancelled")
    if po.net_paid:
        raise PurchaseOrderError(
            f"{po.po_number} has supplier payments recorded (net {po.net_paid}). Reverse them or "
            "record the vendor refund so net paid is zero before cancelling."
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
    """Set issued / partially_received / received from posted receipts
    (net of reversals). The caller must hold the PO lock. Closed and
    cancelled POs are left alone; a closed PO is never reopened. A missing
    (short) unit never counts as received."""
    if po.status not in (*PurchaseOrder.RECEIVABLE_STATUSES, PurchaseOrder.STATUS_RECEIVED):
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
        # Backward moves after a reversal are allowed only here.
        if new_status not in PurchaseOrder.RECALCULATION_TRANSITIONS.get(po.status, set()):
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

    # Booked incoming conversion (spec §5.5). Reservations and allocations
    # are locked before receive_purchased_stock locks stock (global lock
    # order). Damaged units never convert.
    retail_lines = [line for line in lines if line.stock_type == InventoryStock.STOCK_TYPE_RETAIL]
    try:
        incoming = reservation_service.lock_active_incoming_for_lines(
            sorted({line.po_line_id for line in retail_lines})
        )
    except reservation_service.InvalidReservationStateError as exc:
        raise GoodsReceiptError(str(exc)) from exc

    stock_transaction = receive_purchased_stock(
        warehouse=receipt.warehouse,
        supplier=receipt.supplier,
        lines=lines,
        performed_by=user,
        notes=f"Goods receipt {receipt.grn_number} for {po.po_number}",
    )
    try:
        reservation_service.convert_incoming_allocations(incoming, retail_lines, receipt.warehouse)
    except reservation_service.InsufficientStockError as exc:
        raise GoodsReceiptError(str(exc)) from exc

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


def create_opening_receipt(warehouse, user, received_date=None, notes=""):
    """Start an empty draft opening-stock receipt: stock already on hand at
    cutover. No purchase order and no vendor, so it never enters PO or
    vendor receipt history. Lines carry variant, stock type, quantity and
    unit cost."""
    receipt = GoodsReceipt(
        receipt_type=GoodsReceipt.TYPE_OPENING,
        warehouse=warehouse,
        notes=(notes or "").strip(),
        created_by=user,
    )
    if received_date:
        receipt.received_date = received_date
    receipt.save()
    return receipt


@transaction.atomic
def post_opening_receipt(receipt, user):
    """Draft opening receipt -> posted. Adds the units to stock through one
    opening_stock StockTransaction (opening_in movements, never
    purchase_in). Each line keeps its unit cost for valuation. All or
    nothing; posting twice is refused. Posted opening receipts cannot be
    reversed; correct mistakes with a stocktaking adjustment."""
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.receipt_type != GoodsReceipt.TYPE_OPENING:
        raise GoodsReceiptError("Only opening stock receipts can be posted here.")
    if receipt.status == GoodsReceipt.STATUS_POSTED:
        raise GoodsReceiptAlreadyPosted(f"{receipt.grn_number} is already posted.")
    if receipt.status != GoodsReceipt.STATUS_DRAFT:
        raise GoodsReceiptError(f"{receipt.grn_number} is not a draft and cannot be posted.")

    lines = list(receipt.lines.order_by("pk"))
    problems = []
    if receipt.supplier_id or receipt.purchase_order_id:
        problems.append("Opening stock receipts have no vendor or purchase order.")
    if not lines:
        problems.append("Add at least one line before posting.")
    allowed_types = {value for value, _ in GoodsReceiptLine.STOCK_TYPE_CHOICES}
    for number, line in enumerate(lines, start=1):
        if line.po_line_id:
            problems.append(f"Line {number}: opening stock lines have no PO line.")
        if line.quantity <= 0:
            problems.append(f"Line {number}: quantity must be more than 0.")
        if line.stock_type not in allowed_types:
            problems.append(f"Line {number}: stock type must be retail or damaged.")
    if problems:
        raise GoodsReceiptError(" ".join(problems))

    stock_transaction = receive_opening_stock(
        warehouse=receipt.warehouse,
        lines=lines,
        performed_by=user,
        notes=f"Opening stock receipt {receipt.grn_number}",
    )
    receipt.status = GoodsReceipt.STATUS_POSTED
    receipt.stock_transaction = stock_transaction
    receipt.posted_by = user
    receipt.posted_at = timezone.now()
    receipt.save(update_fields=["status", "stock_transaction", "posted_by", "posted_at", "updated_at"])
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
    _refuse_active_incoming(po, "closed")
    po.status = PurchaseOrder.STATUS_CLOSED
    po.closed_by = user
    po.closed_at = timezone.now()
    po.close_reason = reason
    po.save(update_fields=["status", "closed_by", "closed_at", "close_reason", "updated_at"])
    return po


# --- Goods receipt reversals ---


@transaction.atomic
def reverse_goods_receipt(receipt, user, reason, quantities):
    """Correct a posted standard receipt: create an already-posted
    reversal receipt that removes the given units from stock.

    `quantities` is {original_line_id: units to reverse}. Variant, stock
    type, cost and tax are copied from the original lines, never taken from
    input. The original receipt and its movements are never changed.
    Several partial reversals are allowed; together they can never exceed
    what the original line received. Only available stock can be removed;
    customer reservations are never touched. A closed PO stays closed."""
    reason = (reason or "").strip()
    if not reason:
        raise GoodsReceiptError("A reversal reason is required.")
    po_id = GoodsReceipt.objects.values_list("purchase_order_id", flat=True).get(pk=receipt.pk)
    if po_id is None:
        raise GoodsReceiptError("Only purchase order receipts can be reversed.")
    po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
    original = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if (
        original.status != GoodsReceipt.STATUS_POSTED
        or original.receipt_type != GoodsReceipt.TYPE_STANDARD
    ):
        raise GoodsReceiptError(
            f"{original.grn_number}: only a posted standard goods receipt can be reversed."
        )

    wanted = {}
    for line_id, quantity in (quantities or {}).items():
        quantity = int(quantity or 0)
        if quantity < 0:
            raise GoodsReceiptError("Reversal quantities cannot be negative.")
        if quantity:
            wanted[int(line_id)] = quantity
    if not wanted:
        raise GoodsReceiptError("Enter a quantity to reverse on at least one line.")

    # The original receipt lock serialises reversals of the same receipt,
    # so the cumulative check below cannot race.
    lines = {line.pk: line for line in original.lines.order_by("pk")}
    unknown = set(wanted) - set(lines)
    if unknown:
        raise GoodsReceiptError(f"Lines do not belong to {original.grn_number}.")
    list(
        PurchaseOrderLine.objects.select_for_update()
        .filter(pk__in=sorted({lines[pk].po_line_id for pk in wanted}))
        .order_by("pk")
        .values_list("pk", flat=True)
    )

    already = selectors.reversed_quantities(wanted)
    problems = []
    for line_id, quantity in wanted.items():
        line = lines[line_id]
        remaining = line.quantity - already.get(line_id, 0)
        if quantity > remaining:
            problems.append(
                f"{line.variant.sku} [{line.stock_type}]: received {line.quantity}, "
                f"already reversed {already.get(line_id, 0)}, cannot reverse {quantity} more."
            )
    if problems:
        raise GoodsReceiptError(" ".join(problems))

    reversal = GoodsReceipt.objects.create(
        receipt_type=GoodsReceipt.TYPE_REVERSAL,
        supplier_id=original.supplier_id,
        warehouse_id=original.warehouse_id,
        purchase_order=po,
        reverses=original,
        reversal_reason=reason,
        created_by=user,
    )
    reversal_lines = GoodsReceiptLine.objects.bulk_create([
        GoodsReceiptLine(
            receipt=reversal,
            po_line_id=lines[line_id].po_line_id,
            variant_id=lines[line_id].variant_id,
            stock_type=lines[line_id].stock_type,
            quantity=quantity,
            unit_cost=lines[line_id].unit_cost,
            tax_rate=lines[line_id].tax_rate,
            reverses_line=lines[line_id],
        )
        for line_id, quantity in sorted(wanted.items())
    ])

    try:
        stock_transaction = reverse_purchased_stock(
            warehouse=original.warehouse,
            supplier=original.supplier,
            lines=reversal_lines,
            performed_by=user,
            notes=f"Reversal {reversal.grn_number} of {original.grn_number} for {po.po_number}: {reason}",
        )
    except PurchaseReversalStockError as exc:
        raise GoodsReceiptError(str(exc)) from exc

    reversal.status = GoodsReceipt.STATUS_POSTED
    reversal.stock_transaction = stock_transaction
    reversal.posted_by = user
    reversal.posted_at = timezone.now()
    reversal.save(update_fields=["status", "stock_transaction", "posted_by", "posted_at", "updated_at"])

    refresh_po_receipt_status(po)
    return reversal


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


# --- Booked incoming inventory (DEC-009) ---


class BookedQuantityError(Exception):
    """Raised when a booked quantity change is not allowed."""


@transaction.atomic
def set_confirmed_booked_quantity(line, quantity, performed_by, note):
    """Set the cumulative vendor-confirmed total for a PO line and append
    one BookedQuantityChange. The total includes units already received;
    it is not the remaining quantity. Returns (line, change); change is
    None when the value is unchanged.

    Lowering is refused while the remaining booking would no longer cover
    the active incoming allocations of customer orders: those must be
    reallocated or released first."""
    note = (note or "").strip()
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise BookedQuantityError("Booked quantity must be a whole number.")

    po = _lock(line.purchase_order)
    line = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk)
    if po.status not in PurchaseOrder.BOOKABLE_STATUSES:
        raise BookedQuantityError(
            f"{po.po_number} is {po.get_status_display()}. Booked quantities can be set "
            "only on issued or partially received purchase orders."
        )
    if quantity < 0 or quantity > line.quantity_ordered:
        raise BookedQuantityError(
            f"Booked quantity must be between 0 and the ordered quantity ({line.quantity_ordered})."
        )
    if quantity == line.confirmed_booked_quantity:
        return line, None
    if not note:
        raise BookedQuantityError("A note about the vendor confirmation is required.")

    received = selectors.received_quantities([line.pk], stock_type="retail").get(line.pk, 0)
    allocated = selectors.active_incoming_allocated_quantities([line.pk]).get(line.pk, 0)
    if max(quantity - received, 0) < allocated:
        raise BookedQuantityError(
            f"Customer orders are waiting for {selectors.format_units(allocated)} unit(s) of "
            f"this line. With {received} already received, the confirmed total cannot go "
            f"below {selectors.format_units(received + allocated)}. Reallocate or cancel "
            "those orders first."
        )

    change = BookedQuantityChange.objects.create(
        purchase_order_line=line,
        old_quantity=line.confirmed_booked_quantity,
        new_quantity=quantity,
        note=note,
        performed_by=performed_by,
    )
    line.confirmed_booked_quantity = quantity
    line.save(update_fields=["confirmed_booked_quantity", "updated_at"])
    return line, change


# --- DEC-010: purchase tax treatment classification and supplier payments ---
# Operational records only; Zoho Books is authoritative for accounting.
# Every function locks the PO row first (same lock order as the rest of
# this module) and never touches stock, goods receipts, reservations,
# incoming allocations, booked quantities or PO status.


class SupplierPaymentError(Exception):
    """Raised when a supplier payment ledger entry is not allowed."""


@transaction.atomic
def set_po_tax_treatment(po, treatment, user):
    """One-time classification of a pre-DEC-010 purchase order that has no
    tax treatment. Drafts choose it on the form instead."""
    po = _lock(po)
    if treatment not in dict(PurchaseOrder.TAX_TREATMENT_CHOICES):
        raise PurchaseOrderError("Choose GST invoice or no GST invoice.")
    if po.purchase_tax_treatment is not None:
        raise PurchaseOrderError(f"{po.po_number} already has a tax treatment.")
    if po.status in (PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_CANCELLED):
        raise PurchaseOrderError(
            f"{po.po_number} is {po.get_status_display().lower()}; only issued or later purchase "
            "orders are classified here (drafts choose it on the form)."
        )
    lines = list(po.lines.select_related("variant").order_by("pk"))
    problems = _tax_treatment_problems(treatment, po.amounts_include_tax, lines)
    if problems:
        raise PurchaseOrderError(" ".join(problems))
    po.purchase_tax_treatment = treatment
    po.save(update_fields=["purchase_tax_treatment", "updated_at"])
    return po


def _posted_audit(user):
    return {
        "status": PurchaseOrderPayment.STATUS_POSTED, "posted_by": user, "posted_at": timezone.now(),
    }


def next_payment_number():
    """DEC-011: next SPAY number from the database sequence. Concurrency-safe;
    a transaction that rolls back after this leaves a gap, which is accepted."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('purchases_spay_seq')")
        (value,) = cursor.fetchone()
    return f"{PurchaseOrderPayment.PAYMENT_NUMBER_PREFIX}{value:06d}"


def _check_entry_input(amount, entry_date, method, reference, notes):
    if amount is None or amount <= 0:
        raise SupplierPaymentError("Amount must be greater than zero.")
    if entry_date > timezone.localdate():
        raise SupplierPaymentError("The date cannot be in the future.")
    if method not in dict(PurchaseOrderPayment.METHOD_CHOICES):
        raise SupplierPaymentError("Choose a payment method.")
    reference = (reference or "").strip()
    notes = (notes or "").strip()
    if method in PurchaseOrderPayment.REFERENCE_REQUIRED_METHODS and not reference:
        raise SupplierPaymentError("A reference (UTR, cheque or card slip number) is required for this method.")
    if method == PurchaseOrderPayment.METHOD_OTHER and not reference and len(notes) < 10:
        raise SupplierPaymentError("For 'other', enter a reference or notes explaining the payment.")
    return reference, notes


def _lock_payable_po(po):
    po = _lock(po)
    if po.status not in PurchaseOrder.PAYABLE_STATUSES:
        raise SupplierPaymentError(
            f"{po.po_number} is {po.get_status_display().lower()}; payments can be recorded only "
            "once it is issued (and not after cancellation)."
        )
    if po.purchase_tax_treatment is None:
        raise SupplierPaymentError(
            f"{po.po_number} has no purchase tax treatment yet; a manager must classify it first."
        )
    return po


def _check_paid_through(paid_through, required):
    if paid_through is None:
        if required:
            raise SupplierPaymentError("Paid through is required to post a payment.")
        return
    # Re-read so a deactivation committed by someone else is seen.
    if not PaymentAccount.objects.filter(pk=paid_through.pk, is_active=True).exists():
        raise SupplierPaymentError(f"Paid through account '{paid_through.name}' is inactive.")


def _lock_payment(entry, po):
    """Lock the ledger row after its PO (DEC-011 lock order: PO, then entry)."""
    locked = PurchaseOrderPayment.objects.select_for_update().filter(pk=entry.pk).first()
    if locked is None:
        raise SupplierPaymentError("This draft payment no longer exists.")
    if locked.purchase_order_id != po.pk:
        raise SupplierPaymentError("This draft belongs to a different purchase order.")
    return locked


def _refuse_posted(entry, action):
    if not entry.is_draft or entry.entry_type != PurchaseOrderPayment.TYPE_PAYMENT:
        number = entry.payment_number or "This entry"
        raise SupplierPaymentError(
            f"{number} is already posted and cannot be {action}; record a reversal instead."
        )


@transaction.atomic
def create_payment_draft(po, *, amount, entry_date, method, reference, notes, user, paid_through=None):
    """DEC-011: save a supplier payment as a draft. A draft has no SPAY number
    and never counts toward net paid, payment status, caps or cancellation."""
    po = _lock_payable_po(po)
    reference, notes = _check_entry_input(amount, entry_date, method, reference, notes)
    _check_paid_through(paid_through, required=False)
    return PurchaseOrderPayment.objects.create(
        purchase_order=po, entry_type=PurchaseOrderPayment.TYPE_PAYMENT,
        status=PurchaseOrderPayment.STATUS_DRAFT, amount=amount, entry_date=entry_date, method=method,
        reference=reference, notes=notes, paid_through=paid_through, recorded_by=user,
    )


@transaction.atomic
def update_payment_draft(draft, *, user, amount, entry_date, method, reference, notes, paid_through):
    """Edit a draft. Posted entries are immutable."""
    po = _lock_payable_po(draft.purchase_order)
    draft = _lock_payment(draft, po)
    _refuse_posted(draft, "edited")
    reference, notes = _check_entry_input(amount, entry_date, method, reference, notes)
    _check_paid_through(paid_through, required=False)
    draft.amount, draft.entry_date, draft.method = amount, entry_date, method
    draft.reference, draft.notes, draft.paid_through = reference, notes, paid_through
    draft.save()
    return draft


@transaction.atomic
def delete_payment_draft(draft, user):
    """Delete a draft (allowed on any PO status, including cancelled)."""
    po = _lock(draft.purchase_order)
    draft = _lock_payment(draft, po)
    _refuse_posted(draft, "deleted")
    draft.delete()


@transaction.atomic
def post_payment(po, *, user, draft=None, amount, entry_date, method, reference, notes, paid_through):
    """DEC-011: the single posting path, used by direct Save as Paid and by
    posting a draft. Locks the PO, then the draft; revalidates everything;
    requires an active Paid Through; caps posted net paid at the ordered
    gross total; assigns the SPAY number last."""
    po = _lock_payable_po(po)
    if draft is not None:
        draft = _lock_payment(draft, po)
        _refuse_posted(draft, "posted again")
    reference, notes = _check_entry_input(amount, entry_date, method, reference, notes)
    _check_paid_through(paid_through, required=True)
    total = po.ordered_gross_total
    new_net = po.net_paid + amount
    if total is None or new_net > total:
        raise SupplierPaymentError(
            f"This payment would make net paid {new_net}, which would exceed the ordered gross "
            f"total of {total}."
        )
    entry = draft or PurchaseOrderPayment(
        purchase_order=po, entry_type=PurchaseOrderPayment.TYPE_PAYMENT, recorded_by=user,
    )
    entry.amount, entry.entry_date, entry.method = amount, entry_date, method
    entry.reference, entry.notes, entry.paid_through = reference, notes, paid_through
    entry.payment_number = next_payment_number()
    for field, value in _posted_audit(user).items():
        setattr(entry, field, value)
    entry.save()
    return entry


def record_po_payment(po, amount, entry_date, method, reference, notes, user, paid_through=None):
    """Record money paid to the supplier directly (Save as Paid). Net paid
    can never exceed the PO's ordered_gross_total."""
    return post_payment(
        po, user=user, amount=amount, entry_date=entry_date, method=method, reference=reference,
        notes=notes, paid_through=paid_through,
    )


@transaction.atomic
def record_vendor_refund(po, amount, entry_date, method, reference, notes, user):
    """Record money the supplier actually returned. Cannot exceed net paid."""
    po = _lock_payable_po(po)
    reference, notes = _check_entry_input(amount, entry_date, method, reference, notes)
    net = po.net_paid
    if amount > net:
        raise SupplierPaymentError(f"A vendor refund cannot exceed net paid ({net}).")
    return PurchaseOrderPayment.objects.create(
        purchase_order=po, entry_type=PurchaseOrderPayment.TYPE_VENDOR_REFUND, amount=amount,
        entry_date=entry_date, method=method, reference=reference, notes=notes, recorded_by=user,
        **_posted_audit(user),
    )


@transaction.atomic
def reverse_po_payment_entry(entry, reason, user):
    """Correct a wrongly recorded payment or vendor refund with a linked,
    full-amount reversal. Net paid must stay between zero and the ordered
    gross total, and a cancelled PO must stay at zero."""
    reason = (reason or "").strip()
    if not reason:
        raise SupplierPaymentError("A reversal reason is required.")
    po = _lock(entry.purchase_order)
    entry = PurchaseOrderPayment.objects.select_for_update().get(pk=entry.pk)
    if entry.is_draft:
        raise SupplierPaymentError("A draft payment cannot be reversed; edit or delete the draft instead.")
    if entry.entry_type == PurchaseOrderPayment.TYPE_REVERSAL:
        raise SupplierPaymentError("A reversal cannot be reversed.")
    if PurchaseOrderPayment.objects.filter(reverses=entry).exists():
        raise SupplierPaymentError("This entry is already reversed.")
    net = po.net_paid
    if entry.entry_type == PurchaseOrderPayment.TYPE_PAYMENT:
        new_net = net - entry.amount
    else:
        new_net = net + entry.amount
    if new_net < 0:
        raise SupplierPaymentError(
            "Reversing this payment would take net paid below zero. Reverse the vendor refund first."
        )
    total = po.ordered_gross_total
    if total is not None and new_net > total:
        raise SupplierPaymentError(
            f"Reversing this refund would make net paid {new_net}, which would exceed the ordered "
            f"gross total of {total}."
        )
    if po.status == PurchaseOrder.STATUS_CANCELLED and new_net != 0:
        raise SupplierPaymentError(f"{po.po_number} is cancelled; its net paid must stay at zero.")
    return PurchaseOrderPayment.objects.create(
        purchase_order=po, entry_type=PurchaseOrderPayment.TYPE_REVERSAL, amount=entry.amount,
        entry_date=timezone.localdate(), reverses=entry, reversal_reason=reason, recorded_by=user,
        **_posted_audit(user),
    )

