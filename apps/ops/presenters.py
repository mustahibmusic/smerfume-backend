"""Formatting / view-model helpers for /ops/ templates.

Display only: no calculations of totals, balances or statuses. Those
come from apps.orders (model fields and apps.orders.payments).
"""

from django.core.exceptions import ObjectDoesNotExist

from apps.orders import payments as order_payments

PAYMENT_STATUS_LABELS = dict(order_payments.STATUS_CHOICES)

# Visual tone per status, for badges only.
ORDER_STATUS_TONES = {
    "pending": "warn",
    "confirmed": "info",
    "processing": "info",
    "shipped": "info",
    "delivered": "ok",
    "cancelled": "muted",
    "refunded": "muted",
}
PAYMENT_STATUS_TONES = {
    order_payments.STATUS_UNPAID: "danger",
    order_payments.STATUS_PARTIALLY_PAID: "warn",
    order_payments.STATUS_PAID: "ok",
    order_payments.STATUS_OVERPAID: "info",
}


def shipping_address(order):
    try:
        return order.shipping_address
    except ObjectDoesNotExist:
        return None


def customer_name(order):
    address = shipping_address(order)
    if address is not None and address.full_name:
        return address.full_name
    return order.user.full_name or order.guest_email or order.user.email or "—"


def customer_contact(order):
    address = shipping_address(order)
    if address is not None and address.mobile:
        return address.mobile
    return order.user.mobile_number or order.guest_email or order.user.email or ""


def payment_status_label(status):
    return PAYMENT_STATUS_LABELS.get(status, status)


def order_row(order):
    """List row for an order annotated by annotate_payment_summary()."""
    return {
        "order": order,
        "customer": customer_name(order),
        "contact": customer_contact(order),
        "status_tone": ORDER_STATUS_TONES.get(order.status, "muted"),
        "payment_status_label": payment_status_label(order.payment_status),
        "payment_status_tone": PAYMENT_STATUS_TONES.get(order.payment_status, "muted"),
    }
