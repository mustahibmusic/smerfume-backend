"""When a purchase action is offered to a user, shared by Django Admin and
the Smerfume Panel (/ops/) so both show the same buttons.

Display/access rules only: each apps.purchases.services function re-checks
the status and amounts with the rows locked and stays authoritative.
"""

from . import selectors
from .models import GoodsReceipt, PurchaseOrder, ReceiptDiscrepancy


def can_issue(user, po):
    return user.has_perm("purchases.issue_purchaseorder") and po.status == PurchaseOrder.STATUS_DRAFT


def can_cancel(user, po):
    return user.has_perm("purchases.cancel_purchaseorder") and po.status in (
        PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_ISSUED,
    )


def can_close(user, po):
    return user.has_perm("purchases.close_purchaseorder") and po.status == PurchaseOrder.STATUS_PARTIALLY_RECEIVED


def can_confirm_booked(user, po):
    return user.has_perm("purchases.confirm_booked_quantity") and po.status in PurchaseOrder.BOOKABLE_STATUSES


def can_receive(user, po):
    return user.has_perm("purchases.add_goodsreceipt") and po.status in PurchaseOrder.RECEIVABLE_STATUSES


def is_payable(po):
    return po.status in PurchaseOrder.PAYABLE_STATUSES and po.purchase_tax_treatment is not None


def is_fully_paid(po):
    """Nothing left to pay (posted entries only, DEC-011). Unknown totals
    keep payment available; post_payment still refuses them."""
    remaining = po.remaining_against_po
    return remaining is not None and remaining <= 0


def can_record_payment(user, po):
    # DEC-011: drafting needs record_po_payment, Save as Paid needs post_po_payment.
    can_use = user.has_perm("purchases.record_po_payment") or user.has_perm("purchases.post_po_payment")
    return can_use and is_payable(po) and not is_fully_paid(po)


def can_record_vendor_refund(user, po):
    return user.has_perm("purchases.record_po_payment") and is_payable(po)


def can_reverse_payment_entry(user, po):
    return user.has_perm("purchases.reverse_po_payment") and selectors.reversible_payment_entries(po).exists()


def can_set_tax_treatment(user, po):
    return (
        user.has_perm("purchases.set_po_tax_treatment")
        and po.purchase_tax_treatment is None
        and po.status not in (PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_CANCELLED)
    )


def can_post_receipt(user, receipt):
    return user.has_perm("purchases.post_goodsreceipt") and receipt.status == GoodsReceipt.STATUS_DRAFT


def can_cancel_receipt(user, receipt):
    return user.has_perm("purchases.cancel_goodsreceipt") and receipt.status == GoodsReceipt.STATUS_DRAFT


def can_reverse_receipt(user, receipt):
    return user.has_perm("purchases.reverse_goodsreceipt") and any(
        remaining > 0 for remaining in selectors.reversible_quantities(receipt).values()
    )


def can_resolve_discrepancy(user, discrepancy):
    return (
        user.has_perm("purchases.resolve_receiptdiscrepancy")
        and discrepancy.status == ReceiptDiscrepancy.STATUS_OPEN
        and discrepancy.receipt.status == GoodsReceipt.STATUS_POSTED
    )


def po_actions(user, po):
    """{action: allowed} for one purchase order."""
    return {
        "issue": can_issue(user, po),
        "cancel": can_cancel(user, po),
        "close": can_close(user, po),
        "confirm_booked": can_confirm_booked(user, po),
        "receive": can_receive(user, po),
        "record_payment": can_record_payment(user, po),
        "record_vendor_refund": can_record_vendor_refund(user, po),
        "reverse_payment_entry": can_reverse_payment_entry(user, po),
        "set_tax_treatment": can_set_tax_treatment(user, po),
    }
