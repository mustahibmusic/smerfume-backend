"""
Read-only purchase queries. Received quantities are always derived from
POSTED STANDARD goods receipt lines, never stored. A short (missing) unit
is never on a receipt line, so it never counts as received; damaged units
that physically arrived do.

Reversal receipts are not posted yet. When they are, received_quantity
must subtract posted reversal lines.
"""

from django.db.models import Sum

from .models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, ReceiptDiscrepancy


def _posted_standard_lines():
    return GoodsReceiptLine.objects.filter(
        receipt__status=GoodsReceipt.STATUS_POSTED,
        receipt__receipt_type=GoodsReceipt.TYPE_STANDARD,
    )


def received_quantities(po_line_ids):
    """{po_line_id: physically received units} for many PO lines in one
    query. Lines with nothing received are omitted."""
    rows = (
        _posted_standard_lines()
        .filter(po_line_id__in=list(po_line_ids))
        .values("po_line_id")
        .annotate(total=Sum("quantity"))
    )
    return {row["po_line_id"]: row["total"] for row in rows}


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
