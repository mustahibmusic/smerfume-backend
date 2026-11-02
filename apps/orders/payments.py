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
    "payment_summary", "annotate_payment_summary", "STATUS_CHOICES",
]

ZERO = Decimal("0.00")
REFERENCE_MAX_LENGTH = 100

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
def record_order_payment(order, *, method, amount, reference, recorded_by, payment_date=None):
    """Record one customer payment against `order`. Locks the order row so
    payments for one order are serialised."""
    amount, reference = validate_payment_input(method, amount, reference)
    payment_date = payment_date or timezone.localdate()
    if payment_date > timezone.localdate():
        raise OrderPaymentError("The payment date cannot be in the future.")
    order = Order.objects.select_for_update().get(pk=order.pk)
    return OrderPayment.objects.create(
        order=order,
        payment_number=next_payment_number(),
        method=method,
        amount=amount,
        reference=reference,
        payment_date=payment_date,
        recorded_by=recorded_by,
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
