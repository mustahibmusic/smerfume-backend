"""
Read-only purchase queries. Received quantities are always derived, never
stored: POSTED STANDARD goods receipt lines minus POSTED REVERSAL lines. A
short (missing) unit is never on a receipt line, so it never counts as
received; damaged units that physically arrived do.
"""

from django.db.models import Case, F, IntegerField, Sum, When

from .models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, ReceiptDiscrepancy


def _posted_standard_lines():
    return GoodsReceiptLine.objects.filter(
        receipt__status=GoodsReceipt.STATUS_POSTED,
        receipt__receipt_type=GoodsReceipt.TYPE_STANDARD,
    )


def received_quantities(po_line_ids):
    """{po_line_id: net physically received units} for many PO lines in
    one query. Lines with nothing received are omitted."""
    signed = Case(
        When(receipt__receipt_type=GoodsReceipt.TYPE_REVERSAL, then=-F("quantity")),
        default=F("quantity"),
        output_field=IntegerField(),
    )
    rows = (
        GoodsReceiptLine.objects.filter(
            receipt__status=GoodsReceipt.STATUS_POSTED,
            receipt__receipt_type__in=(GoodsReceipt.TYPE_STANDARD, GoodsReceipt.TYPE_REVERSAL),
            po_line_id__in=list(po_line_ids),
        )
        .values("po_line_id")
        .annotate(total=Sum(signed))
    )
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
