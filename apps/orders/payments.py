"""DEC-013 customer payment ledger.

record_order_payment() is the only business write path for OrderPayment.
Paid amount, balance due and payment status are always derived from the
payment rows (payment_summary for one order, annotate_payment_summary for
a queryset), never from Order.payment_method / payment_reference.
"""

import re
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Case, CharField, DecimalField, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Order, OrderPayment, OrderPaymentError, Refund, RefundAdjustment

__all__ = [
    "OrderPaymentError", "record_order_payment", "validate_payment_input", "next_payment_number",
    "payment_summary", "annotate_payment_summary", "STATUS_CHOICES", "OUTSTANDING_STATUSES",
]

ZERO = Decimal("0.00")
REFERENCE_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 1000

STATUS_UNPAID = "unpaid"
STATUS_PARTIALLY_PAID = "partially_paid"
STATUS_PAID = "paid"
STATUS_OVERPAID = "overpaid"
STATUS_CHOICES = (
    (STATUS_UNPAID, "Unpaid"),
    (STATUS_PARTIALLY_PAID, "Partially paid"),
    (STATUS_PAID, "Paid"),
    (STATUS_OVERPAID, "Overpaid"),
)
# Money still to collect: the statuses a manual Record Payment can act on.
OUTSTANDING_STATUSES = (STATUS_UNPAID, STATUS_PARTIALLY_PAID)

_MONEY = DecimalField(max_digits=12, decimal_places=2)


def _looks_like_card_number(reference):
    """13-19 digits (spaces and dashes ignored) that pass the Luhn check.
    A safety check against card numbers typed into the reference field,
    not PCI protection."""
    digits = re.sub(r"[\s-]", "", reference)
    if not re.fullmatch(r"[0-9]{13,19}", digits):
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def validate_reference(method, reference):
    """Return the stripped reference or raise OrderPaymentError."""
    reference = (reference or "").strip()
    if len(reference) > REFERENCE_MAX_LENGTH:
        raise OrderPaymentError(f"The reference can be at most {REFERENCE_MAX_LENGTH} characters.")
    if method in OrderPayment.REFERENCE_REQUIRED_METHODS and not reference:
        raise OrderPaymentError(
            f"A reference (UTR, slip or transaction number) is required for "
            f"{dict(OrderPayment.METHOD_CHOICES)[method]} payments."
        )
    if method == Order.PAYMENT_METHOD_CARD and _looks_like_card_number(reference):
        raise OrderPaymentError("Enter the slip or transaction reference, not the card number.")
    return reference


def validate_payment_input(method, amount, reference):
    """Check one payment's method, amount and reference. Returns
    (amount, reference); raises OrderPaymentError."""
    if method not in Order.IN_STORE_PAYMENT_METHODS:
        raise OrderPaymentError("Choose cash, UPI, card or netbanking.")
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise OrderPaymentError("The amount must be a decimal number.")
    if amount <= 0:
        raise OrderPaymentError("The amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        raise OrderPaymentError("The amount can have at most two decimal places.")
    return amount.quantize(Decimal("0.01")), validate_reference(method, reference)


def next_payment_number():
    """Next CPAY number from the database sequence. Concurrency-safe; a
    rolled-back transaction leaves a gap, which is accepted (DEC-013)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('orders_cpay_seq')")
        (value,) = cursor.fetchone()
    return f"{OrderPayment.PAYMENT_NUMBER_PREFIX}{value:06d}"


@transaction.atomic
def record_order_payment(order, *, method, amount, reference, recorded_by, payment_date=None, notes=""):
    """Record one customer payment against `order` (DEC-013 Record Payment).

    The order row is locked, then the paid total is recalculated from the
    payment rows: that locked balance is authoritative, so concurrent or
    repeated submissions can never take the paid total above Order.total.
    A cancelled order cannot receive a payment; the date cannot be in the
    future. No draft payments: a row is either saved or nothing is."""
    amount, reference = validate_payment_input(method, amount, reference)
    notes = (notes or "").strip()
    if len(notes) > NOTES_MAX_LENGTH:
        raise OrderPaymentError(f"Notes can be at most {NOTES_MAX_LENGTH} characters.")
    payment_date = payment_date or timezone.localdate()
    if payment_date > timezone.localdate():
        raise OrderPaymentError("The payment date cannot be in the future.")
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status == Order.STATUS_CANCELLED:
        raise OrderPaymentError(f"{order.order_number} is cancelled and cannot receive payments.")
    paid = order.payments.aggregate(total=Sum("amount"))["total"] or ZERO
    balance_due = order.total - paid
    if balance_due <= 0:
        raise OrderPaymentError(f"{order.order_number} is fully paid: nothing left to pay.")
    if amount > balance_due:
        raise OrderPaymentError(f"The amount is more than the balance due (₹{balance_due}).")
    payment = OrderPayment.objects.create(
        order=order,
        payment_number=next_payment_number(),
        method=method,
        amount=amount,
        reference=reference,
        payment_date=payment_date,
        notes=notes,
        recorded_by=recorded_by,
    )
    refresh_legacy_payment_summary(order)
    return payment


def refresh_legacy_payment_summary(order):
    """Keep Order.payment_method / payment_reference as a compatibility
    summary of an in-store order's payment rows: blank with none, the single
    payment's method and reference with one, "split" and a blank reference
    with several. Display only; no calculation reads these fields. Online
    orders keep their checkout method (COD logic depends on it)."""
    if order.channel != Order.CHANNEL_IN_STORE:
        return
    rows = list(order.payments.order_by("payment_date", "id").values_list("method", "reference")[:2])
    if not rows:
        method, reference = "", ""
    elif len(rows) == 1:
        method, reference = rows[0]
    else:
        method, reference = Order.PAYMENT_METHOD_SPLIT, ""
    Order.objects.filter(pk=order.pk).update(
        payment_method=method, payment_reference=reference, updated_at=timezone.now(),
    )


def derive_status(total, paid):
    if paid > total:
        return STATUS_OVERPAID
    if paid == total:
        return STATUS_PAID
    if paid == 0:
        return STATUS_UNPAID
    return STATUS_PARTIALLY_PAID


def _refunded(order):
    """Completed refunds plus their adjustments for this order."""
    refunds = Refund.objects.filter(return_request__order=order, refund_status=Refund.STATUS_COMPLETED)
    amount = refunds.aggregate(total=Sum("refund_amount"))["total"] or ZERO
    adjustments = RefundAdjustment.objects.filter(refund__in=refunds).aggregate(
        total=Sum("adjustment_amount")
    )["total"] or ZERO
    return amount + adjustments


def payment_summary(order):
    """total_paid, balance_due, refunded and payment_status for one order."""
    paid = order.payments.aggregate(total=Sum("amount"))["total"] or ZERO
    return {
        "total_paid": paid,
        "balance_due": order.total - paid,
        "refunded": _refunded(order),
        "payment_status": derive_status(order.total, paid),
    }


def annotate_payment_summary(queryset):
    """Annotate total_paid, balance_due and payment_status in SQL (one
    correlated subquery; no per-row queries)."""
    paid = (
        OrderPayment.objects.filter(order=OuterRef("pk"))
        .order_by().values("order").annotate(total=Sum("amount")).values("total")
    )
    queryset = queryset.annotate(
        total_paid=Coalesce(Subquery(paid, output_field=_MONEY), Value(ZERO), output_field=_MONEY),
    )
    return queryset.annotate(
        balance_due=F("total") - F("total_paid"),
        payment_status=Case(
            When(total_paid__gt=F("total"), then=Value(STATUS_OVERPAID)),
            When(total_paid=F("total"), then=Value(STATUS_PAID)),
            When(Q(total_paid=0), then=Value(STATUS_UNPAID)),
            default=Value(STATUS_PARTIALLY_PAID),
            output_field=CharField(),
        ),
    )


def orders_open_for_manual_payment(queryset=None):
    """Orders that may take a manual Record Payment (DEC-013): in-store,
    not cancelled, balance due above zero. Shared by Django Admin and the
    Smerfume Panel to decide whether to offer the action;
    record_order_payment() re-checks cancelled and balance under the lock."""
    queryset = Order.objects.all() if queryset is None else queryset
    return annotate_payment_summary(
        queryset.filter(channel=Order.CHANNEL_IN_STORE).exclude(status=Order.STATUS_CANCELLED)
    ).filter(balance_due__gt=0)
