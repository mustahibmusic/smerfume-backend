"""Read-only order queries shared by the Smerfume Panel (DEC-019).

Payment figures (paid, balance due, payment status) live in
``apps.orders.payments``; this module only names order sets.
"""

from apps.orders.models import Order

# Orders staff still have to pack and send.
FULFILMENT_STATUSES = (Order.STATUS_CONFIRMED, Order.STATUS_PROCESSING)


def _orders(queryset):
    return Order.objects.all() if queryset is None else queryset


def order_value_orders(queryset=None):
    """Orders that count toward order value: every status except cancelled.

    DEC-019: this is "order value" (Order.total), not net sales; refunds
    are not deducted.
    """
    return _orders(queryset).exclude(status=Order.STATUS_CANCELLED)


def orders_to_fulfil(queryset=None):
    """Confirmed or processing orders (not yet shipped)."""
    return _orders(queryset).filter(status__in=FULFILMENT_STATUSES)


def orders_awaiting_cod_verification(queryset=None):
    """Pending COD orders not yet verified (orders.services.verify_cod_order
    is the only path that confirms them)."""
    return _orders(queryset).filter(
        payment_method=Order.PAYMENT_METHOD_COD,
        status=Order.STATUS_PENDING,
        cod_verified_at__isnull=True,
    )
