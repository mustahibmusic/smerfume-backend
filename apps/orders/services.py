"""
Order/checkout financial-snapshot service layer.

Kept separate from the checkout view so the discount-allocation and
per-line financial computation can be unit-tested without going through
the HTTP layer. All values computed here are checkout-time snapshots,
written once onto Order/OrderItem and never recalculated later.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.inventory.models import StockReservation
from apps.inventory.services import reservation as reservation_service

RETURN_WINDOW_DAYS = 1



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


class ReturnEligibilityError(Exception):
    """Raised when a return request fails eligibility checks."""


class ReturnTransitionError(Exception):
    """Raised when a Return isn't in a state that allows the requested
    transition."""


@transaction.atomic
def create_return(order, items, requested_by=None):
    """Create a Return with one ReturnItem per entry in `items`.

    items: list of dicts, each with keys:
        order_item (OrderItem), reason (str), requested_quantity (int),
        reason_notes (str, optional).

    Enforces, for the order and for each item:
      - Order.status == delivered
      - now <= Order.delivered_at + RETURN_WINDOW_DAYS
      - reason is one of ReturnItem.REASON_CHOICES — customer-preference
        reasons ("didn't like it", "changed my mind") have no matching
        choice and are therefore always rejected, not merely discouraged
      - cumulative requested quantity across all non-cancelled/non-rejected
        ReturnItems for that OrderItem never exceeds the purchased quantity,
        checked under select_for_update() on the OrderItem row so two
        concurrent requests against the same line can't both succeed

    Raises ReturnEligibilityError on any violation; nothing is created if
    any single item fails, since the whole call runs in one transaction.
    """
    from .models import Order, OrderItem, Return, ReturnItem

    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status != Order.STATUS_DELIVERED:
        raise ReturnEligibilityError(
            f"Cannot request a return for an order in status={order.status}; it must be delivered."
        )
    if order.delivered_at is None:
        raise ReturnEligibilityError(
            "Order has no delivered_at timestamp; cannot evaluate the return window."
        )
    if timezone.now() > order.delivered_at + timedelta(days=RETURN_WINDOW_DAYS):
        raise ReturnEligibilityError(
            f"Return window has closed — requests must be raised within {RETURN_WINDOW_DAYS} "
            "calendar day of delivery."
        )
    if not items:
        raise ReturnEligibilityError("At least one return item is required.")

    valid_reasons = {choice[0] for choice in ReturnItem.REASON_CHOICES}
    return_request = Return.objects.create(order=order, status=Return.STATUS_REQUESTED)

    for entry in items:
        order_item = OrderItem.objects.select_for_update().get(pk=entry["order_item"].pk)
        if order_item.order_id != order.pk:
            raise ReturnEligibilityError("Order item does not belong to this order.")

        reason = entry["reason"]
        if reason not in valid_reasons:
            raise ReturnEligibilityError(f"'{reason}' is not a valid return reason.")

        requested_quantity = entry["requested_quantity"]
        if requested_quantity <= 0:
            raise ReturnEligibilityError("requested_quantity must be greater than 0.")

        already_claimed = (
            ReturnItem.objects.filter(order_item=order_item)
            .exclude(return_request__status__in=[Return.STATUS_REJECTED, Return.STATUS_CANCELLED])
            .aggregate(total=Sum("requested_quantity"))["total"]
            or 0
        )
        if already_claimed + requested_quantity > order_item.quantity:
            remaining = order_item.quantity - already_claimed
            raise ReturnEligibilityError(
                f"Cannot return {requested_quantity} of {order_item.variant} — only "
                f"{remaining} unit(s) remain eligible for return."
            )

        ReturnItem.objects.create(
            return_request=return_request,
            order_item=order_item,
            reason=reason,
            reason_notes=entry.get("reason_notes", ""),
            requested_quantity=requested_quantity,
        )

    return return_request


@transaction.atomic
def approve_return(return_request):
    """requested -> approved. Also stamps approved_quantity == requested_quantity
    on every line — per-line approval below what was requested isn't
    implemented (no requirement for it yet); this just gives
    finalize_return_item() a stable cap to validate received_quantity
    against, per the approved design."""
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_REQUESTED:
        raise ReturnTransitionError(f"Cannot approve a return in status={return_request.status}.")

    for item in return_request.items.select_for_update():
        item.approved_quantity = item.requested_quantity
        item.save(update_fields=["approved_quantity", "updated_at"])

    return_request.status = Return.STATUS_APPROVED
    return_request.save(update_fields=["status", "updated_at"])
    return return_request


@transaction.atomic
def reject_return(return_request):
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_REQUESTED:
        raise ReturnTransitionError(f"Cannot reject a return in status={return_request.status}.")
    return_request.status = Return.STATUS_REJECTED
    return_request.save(update_fields=["status", "updated_at"])
    return return_request


@transaction.atomic
def cancel_return(return_request):
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status not in (
        Return.STATUS_REQUESTED, Return.STATUS_APPROVED, Return.STATUS_IN_TRANSIT,
    ):
        raise ReturnTransitionError(
            f"Cannot cancel a return in status={return_request.status}; only a request not yet "
            "received can be cancelled."
        )
    return_request.status = Return.STATUS_CANCELLED
    return_request.save(update_fields=["status", "updated_at"])
    return return_request


@transaction.atomic
def mark_return_in_transit(return_request):
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_APPROVED:
        raise ReturnTransitionError(f"Cannot mark in-transit a return in status={return_request.status}.")
    return_request.status = Return.STATUS_IN_TRANSIT
    return_request.save(update_fields=["status", "updated_at"])
    return return_request


@transaction.atomic
def mark_return_received(return_request):
    """in_transit -> received. Requires every ReturnItem on this Return to
    already have received_quantity recorded (a plain editable field, not a
    protected transition itself — recording a quantity has no side effects
    on its own; only advancing Return.status does)."""
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_IN_TRANSIT:
        raise ReturnTransitionError(f"Cannot mark received a return in status={return_request.status}.")

    items = list(return_request.items.select_for_update())
    missing = [item.pk for item in items if item.received_quantity is None]
    if missing:
        raise ReturnTransitionError(
            f"Cannot mark received — received_quantity is not set for ReturnItem(s): {missing}."
        )

    return_request.status = Return.STATUS_RECEIVED
    return_request.save(update_fields=["status", "updated_at"])
    return return_request


@transaction.atomic
def start_inspection(return_request):
    from .models import Return

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_RECEIVED:
        raise ReturnTransitionError(
            f"Cannot start inspection on a return in status={return_request.status}."
        )
    return_request.status = Return.STATUS_INSPECTION_PENDING
    return_request.save(update_fields=["status", "updated_at"])
    return return_request

