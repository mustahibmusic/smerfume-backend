"""
Read-only purchase queries. Received quantities are always derived, never
stored: POSTED STANDARD goods receipt lines minus POSTED REVERSAL lines. A
short (missing) unit is never on a receipt line, so it never counts as
received; damaged units that physically arrived do. Booked incoming quantities
(DEC-009) are derived the same way; only confirmed_booked_quantity is stored.
"""

from decimal import Decimal

from django.conf import settings
from django.db.models import Case, F, IntegerField, Sum, When

from apps.inventory.models import InventoryStock, StockReservationAllocation

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptDiscrepancy,
)


def _posted_standard_lines():
    return GoodsReceiptLine.objects.filter(
        receipt__status=GoodsReceipt.STATUS_POSTED,
        receipt__receipt_type=GoodsReceipt.TYPE_STANDARD,
    )


def received_quantities(po_line_ids, stock_type=None):
    """{po_line_id: net physically received units} for many PO lines in
    one query. Lines with nothing received are omitted. stock_type limits
    the count to one stock type (reversal lines copy the original's)."""
    signed = Case(
        When(receipt__receipt_type=GoodsReceipt.TYPE_REVERSAL, then=-F("quantity")),
        default=F("quantity"),
        output_field=IntegerField(),
    )
    lines = GoodsReceiptLine.objects.filter(
        receipt__status=GoodsReceipt.STATUS_POSTED,
        receipt__receipt_type__in=(GoodsReceipt.TYPE_STANDARD, GoodsReceipt.TYPE_REVERSAL),
        po_line_id__in=list(po_line_ids),
    )
    if stock_type is not None:
        lines = lines.filter(stock_type=stock_type)
    rows = lines.values("po_line_id").annotate(total=Sum(signed))
    return {row["po_line_id"]: row["total"] for row in rows if row["total"]}


def reversed_quantities(line_ids):
    """{original_line_id: units already reversed by posted reversals}."""
    rows = (
        GoodsReceiptLine.objects.filter(
            receipt__status=GoodsReceipt.STATUS_POSTED,
            receipt__receipt_type=GoodsReceipt.TYPE_REVERSAL,
            reverses_line_id__in=list(line_ids),
        )
        .values("reverses_line_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["reverses_line_id"]: row["total"] for row in rows}


def reversible_quantities(receipt):
    """{original_line_id: units still reversible} for a posted standard
    receipt; empty for any other receipt."""
    if (
        receipt.status != GoodsReceipt.STATUS_POSTED
        or receipt.receipt_type != GoodsReceipt.TYPE_STANDARD
    ):
        return {}
    lines = list(receipt.lines.all())
    reversed_ = reversed_quantities(line.pk for line in lines)
    return {line.pk: line.quantity - reversed_.get(line.pk, 0) for line in lines}


def received_quantity(po_line):
    return received_quantities([po_line.pk]).get(po_line.pk, 0)


def outstanding_quantity(po_line, received=None):
    if received is None:
        received = received_quantity(po_line)
    return max(po_line.quantity_ordered - received, 0)


def po_has_posted_receipts(po):
    return _posted_standard_lines().filter(po_line__purchase_order=po).exists()


def closed_short_quantity(po_line, received=None):
    """Units Smerfume stopped waiting for when the PO was closed: ordered
    minus physically received. 0 unless the PO is closed."""
    if po_line.purchase_order.status != PurchaseOrder.STATUS_CLOSED:
        return 0
    return outstanding_quantity(po_line, received)


def open_discrepancy_count(po):
    """Open discrepancies on posted receipts of this PO."""
    return ReceiptDiscrepancy.objects.filter(
        receipt__purchase_order=po,
        receipt__status=GoodsReceipt.STATUS_POSTED,
        status=ReceiptDiscrepancy.STATUS_OPEN,
    ).count()


# --- Booked incoming inventory (DEC-009) ---

ZERO = Decimal("0")


def format_units(value):
    """Whole-unit quantities for messages: Decimal("3.00") -> "3"."""
    return str(int(value)) if value == int(value) else str(value)


def active_incoming_allocated_quantities(po_line_ids):
    """{po_line_id: units held by active incoming allocations}. Converted,
    reallocated and released rows never count: converted units are already
    covered by physical quantity_reserved."""
    rows = (
        StockReservationAllocation.objects.filter(
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            incoming_status=StockReservationAllocation.INCOMING_ACTIVE,
            purchase_order_line_id__in=list(po_line_ids),
        )
        .values("purchase_order_line_id")
        .annotate(total=Sum("units"))
    )
    return {row["purchase_order_line_id"]: row["total"] for row in rows}


def _incoming_amounts(lines):
    """{line_id: incoming sellable} for already-filtered bookable lines:
    max(confirmed_booked_quantity - net retail received - active allocated, 0)."""
    ids = [line.pk for line in lines]
    received = received_quantities(ids, stock_type="retail")
    allocated = active_incoming_allocated_quantities(ids)
    return {
        line.pk: max(
            Decimal(line.confirmed_booked_quantity)
            - received.get(line.pk, 0)
            - allocated.get(line.pk, ZERO),
            ZERO,
        )
        for line in lines
    }


def _bookable_lines(variant_ids, warehouse):
    return list(
        PurchaseOrderLine.objects.filter(
            variant_id__in=list(variant_ids),
            purchase_order__warehouse=warehouse,
            purchase_order__status__in=PurchaseOrder.BOOKABLE_STATUSES,
            confirmed_booked_quantity__gt=0,
        )
    )


def incoming_sellable(po_line):
    """Incoming units of one PO line still available to sell. Zero unless
    its PO is issued or partially received."""
    if po_line.purchase_order.status not in PurchaseOrder.BOOKABLE_STATUSES:
        return ZERO
    return _incoming_amounts([po_line])[po_line.pk]


def incoming_sellable_by_line(variant_ids, warehouse):
    """{po_line_id: incoming sellable} for bookable lines of these variants
    at this warehouse. Lines with nothing to sell are omitted."""
    amounts = _incoming_amounts(_bookable_lines(variant_ids, warehouse))
    return {line_id: amount for line_id, amount in amounts.items() if amount > 0}


def sellable_quantities(variant_ids, warehouse):
    """{variant_id: physical retail available + incoming sellable}. The one
    authoritative availability figure (DEC-009). Incoming counts only when
    settings.BOOKED_INCOMING_SALES_ENABLED is True."""
    variant_ids = list(variant_ids)
    result = {variant_id: ZERO for variant_id in variant_ids}
    for stock in InventoryStock.objects.filter(
        variant_id__in=variant_ids,
        warehouse=warehouse,
        stock_type=InventoryStock.STOCK_TYPE_RETAIL,
    ):
        result[stock.variant_id] += max(stock.available, ZERO)
    if settings.BOOKED_INCOMING_SALES_ENABLED:
        lines = _bookable_lines(variant_ids, warehouse)
        amounts = _incoming_amounts(lines)
        for line in lines:
            result[line.variant_id] += amounts[line.pk]
    return result


def sellable_quantity(variant, warehouse):
    return sellable_quantities([variant.pk], warehouse)[variant.pk]
