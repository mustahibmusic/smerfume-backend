"""
Order/checkout financial-snapshot service layer.

Kept separate from the checkout view so the discount-allocation and
per-line financial computation can be unit-tested without going through
the HTTP layer. All values computed here are checkout-time snapshots,
written once onto Order/OrderItem and never recalculated later.
"""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.inventory.models import StockReservation
from apps.inventory.services import reservation as reservation_service


def allocate_discount(cart_items, order_discount_amount):
    """Proportionally allocate an order-level discount across cart items by
    gross line amount, with the rounding remainder assigned to the last
    line so the allocations sum exactly to order_discount_amount.

    Args:
        cart_items: iterable of CartItem (or anything with .id and .line_total).
        order_discount_amount: Decimal, the total discount to allocate.

    Returns:
        Dict of {item.id: Decimal allocated_discount}.
    """
    items = list(cart_items)
    if not items:
        return {}

    if order_discount_amount <= 0:
        return {item.id: Decimal("0.00") for item in items}

    gross_total = sum((item.line_total for item in items), Decimal("0.00"))
    if gross_total <= 0:
        return {item.id: Decimal("0.00") for item in items}

    allocations = {}
    running_total = Decimal("0.00")
    for item in items[:-1]:
        share = (order_discount_amount * item.line_total / gross_total).quantize(Decimal("0.01"))
        allocations[item.id] = share
        running_total += share
    allocations[items[-1].id] = (order_discount_amount - running_total).quantize(Decimal("0.01"))
    return allocations


def build_order_item_financials(cart_item, discount_allocated):
    """Compute the immutable financial snapshot for one order line.

    tax_amount is always 0.00 — Django does not yet compute GST anywhere in
    this codebase. This is a known prerequisite gap (see CURRENT_STATE.md),
    not an assumption that tax is genuinely zero.
    """
    gross_line_amount = cart_item.line_total
    net_line_amount = gross_line_amount - discount_allocated
    tax_amount = Decimal("0.00")
    final_paid_line_amount = net_line_amount + tax_amount
    shipping_surcharge = (cart_item.variant.shipping_surcharge or Decimal("0.00")) * cart_item.quantity

    return {
        "gross_line_amount": gross_line_amount,
        "discount_allocated": discount_allocated,
        "net_line_amount": net_line_amount,
        "tax_amount": tax_amount,
        "final_paid_line_amount": final_paid_line_amount,
        "shipping_surcharge": shipping_surcharge,
    }


class CODVerificationError(Exception):
    """Raised when an order isn't eligible for COD verification."""


@transaction.atomic
def verify_cod_order(order, verified_by):
    """Staff-verify a pending COD order: hardens its inventory reservations
    (held -> confirmed) and advances the order to `confirmed`. This is the
    one authoritative path for COD confirmation — never flip Order.status
    to confirmed directly elsewhere.
    """
    # Import here, not at module load, to avoid a circular import: .models
    # imports nothing from services, but importing it at module scope here
    # would run before Order is fully defined during app loading in some
    # import orders (admin -> services -> models -> ... -> admin).
    from .models import Order

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.payment_method != Order.PAYMENT_METHOD_COD:
        raise CODVerificationError("Only COD orders require verification.")
    if order.status != Order.STATUS_PENDING:
        raise CODVerificationError(f"Cannot verify an order in status={order.status}.")

    for order_item in order.items.all():
        for res in order_item.stock_reservations.filter(status=StockReservation.STATUS_HELD):
            reservation_service.confirm_reservation(res)

    order.cod_verified_by = verified_by
    order.cod_verified_at = timezone.now()
    order.status = Order.STATUS_CONFIRMED
    order.save(update_fields=["cod_verified_by", "cod_verified_at", "status", "updated_at"])
    return order


class OrderPackingError(Exception):
    """Raised when an order isn't eligible to be packed."""


@transaction.atomic
def pack_order(order, performed_by=None):
    """Physically consume all inventory reserved for this order — the one
    authoritative trigger for the confirmed -> processing transition that
    performs real stock deduction.

    Requires status=confirmed: an order must have passed COD verification
    (or, once it exists, prepaid payment confirmation) before its inventory
    allocation is committed physically. Packing directly from `pending`
    would bypass that gate, so it is rejected rather than assumed allowed.

    Consumes each OrderItem's existing StockReservation via
    reservation_service.consume_reservation() — which itself only ever
    replays the exact StockReservationAllocation rows recorded at checkout.
    No FIFO is re-run and no new allocation decision is made here.

    Locking the Order row makes double-packing safe: a concurrent second
    call blocks on this row, then finds status != confirmed once the first
    call has advanced it to processing, and raises rather than consuming
    anything twice. consume_reservation()'s own per-reservation state guard
    is a second, independent line of defense.
    """
    from .models import Order

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status != Order.STATUS_CONFIRMED:
        raise OrderPackingError(
            f"Cannot pack an order in status={order.status}; it must be confirmed first."
        )

    for order_item in order.items.all():
        reservations = order_item.stock_reservations.filter(
            status__in=[StockReservation.STATUS_HELD, StockReservation.STATUS_CONFIRMED]
        )
        for res in reservations:
            reservation_service.consume_reservation(res, performed_by=performed_by)

    order.status = Order.STATUS_PROCESSING
    order.save(update_fields=["status", "updated_at"])
    return order


class OrderTransitionError(Exception):
    """Raised when an order isn't eligible for the requested simple status
    transition (shipped / delivered / cancelled)."""


@transaction.atomic
def mark_order_shipped(order, tracking_number=None):
    """processing -> shipped. No inventory mutation — physical stock was
    already consumed at packing; shipping is a logistics-only transition.
    tracking_number is a plain manually-entered value for now (no carrier
    integration exists yet)."""
    from .models import Order

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status != Order.STATUS_PROCESSING:
        raise OrderTransitionError(
            f"Cannot mark an order shipped from status={order.status}; it must be processing first."
        )

    if tracking_number:
        order.tracking_number = tracking_number
    order.status = Order.STATUS_SHIPPED
    order.save(update_fields=["status", "tracking_number", "updated_at"])
    return order


@transaction.atomic
def mark_order_delivered(order):
    """shipped -> delivered. No inventory mutation. Uses a full (unrestricted)
    save() rather than update_fields, so Order.save()'s existing delivered_at
    auto-population hook — which sets the field on the in-memory instance
    before the actual write — is not silently excluded from the UPDATE."""
    from .models import Order

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status != Order.STATUS_SHIPPED:
        raise OrderTransitionError(
            f"Cannot mark an order delivered from status={order.status}; it must be shipped first."
        )

    order.status = Order.STATUS_DELIVERED
    order.save()
    return order


@transaction.atomic
def cancel_order(order):
    """pending/confirmed -> cancelled. Releases every active reservation via
    the existing reservation service before changing status, so a
    subsequent read of inventory reflects the cancellation immediately.

    Rejected once the order has reached processing or later — physical
    stock has already left the shelf by then, and undoing a pack/ship is
    return/refund territory (Phase 5), not a simple cancellation.

    Idempotency: once status is cancelled, a repeat call fails the
    precondition check below and never reaches the release loop, so the
    same reservation can never be released twice through repeated
    cancellation attempts.
    """
    from .models import Order

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status not in (Order.STATUS_PENDING, Order.STATUS_CONFIRMED):
        raise OrderTransitionError(
            f"Cannot cancel an order in status={order.status}; "
            "only pending or confirmed orders can be cancelled."
        )

    for order_item in order.items.all():
        reservations = order_item.stock_reservations.filter(
            status__in=[StockReservation.STATUS_HELD, StockReservation.STATUS_CONFIRMED]
        )
        for res in reservations:
            reservation_service.release_reservation(res)

    order.status = Order.STATUS_CANCELLED
    order.save(update_fields=["status", "updated_at"])
    return order
