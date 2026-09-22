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

from apps.inventory.models import (
    DecantSource,
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockTransaction,
    Warehouse,
)
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


class ReturnInspectionError(Exception):
    """Raised when a ReturnItem inspection/finalization request is invalid,
    including an attempt to finalize an already-finalized item."""


@transaction.atomic
def finalize_return_item(
    return_item,
    *,
    received_quantity,
    disposition,
    resolution,
    inspected_by=None,
    inspection_notes="",
    remaining_quantity_ml=None,
):
    """Record inspection results for one ReturnItem and perform the
    corresponding physical inventory mutation, atomically:
    validate -> inspect -> create the inventory movement(s) -> finalize.

    A returned item never becomes sellable inventory merely by arriving —
    only a successful call here, with an explicit disposition, has any
    inventory effect. `rejected` and a damaged decant both finalize the
    ReturnItem with zero inventory movement, by design (see
    _apply_return_disposition_inventory).

    Idempotency/concurrency: select_for_update() locks the ReturnItem row
    first; if disposition is already set, this raises immediately, before
    any inventory mutation is attempted. A concurrent second call blocks on
    that lock until the first transaction commits, then sees disposition
    already set and is rejected the same way — the database transaction is
    what enforces this, not the admin UI calling it at most once.

    If every ReturnItem on the parent Return is now finalized, the Return
    automatically advances inspection_pending -> completed in the same
    transaction; it is never marked completed while any item is still
    unfinalized.
    """
    from .models import Return, ReturnItem

    return_item = ReturnItem.objects.select_for_update().get(pk=return_item.pk)

    if return_item.disposition is not None:
        raise ReturnInspectionError("This return item has already been finalized.")

    return_request = return_item.return_request
    if return_request.status != Return.STATUS_INSPECTION_PENDING:
        raise ReturnInspectionError(
            f"Cannot inspect a return item whose Return is in status={return_request.status}; "
            "the Return must be in inspection_pending."
        )

    if return_item.approved_quantity is None:
        raise ReturnInspectionError("This return item has no approved_quantity recorded.")

    if received_quantity < 0:
        raise ReturnInspectionError("received_quantity cannot be negative.")
    if received_quantity > return_item.approved_quantity:
        raise ReturnInspectionError(
            f"received_quantity ({received_quantity}) cannot exceed "
            f"approved_quantity ({return_item.approved_quantity})."
        )

    valid_dispositions = {choice[0] for choice in ReturnItem.DISPOSITION_CHOICES}
    if disposition not in valid_dispositions:
        raise ReturnInspectionError(f"'{disposition}' is not a valid disposition.")

    valid_resolutions = {choice[0] for choice in ReturnItem.RESOLUTION_CHOICES}
    if resolution not in valid_resolutions:
        raise ReturnInspectionError(f"'{resolution}' is not a valid resolution.")

    # A StockMovement is never created with quantity_delta=0 — rejected is
    # the only disposition where zero physical inventory is genuinely
    # expected, so it's the only one allowed to carry received_quantity=0.
    if received_quantity == 0 and disposition != ReturnItem.DISPOSITION_REJECTED:
        raise ReturnInspectionError(
            f"received_quantity=0 is only valid for disposition=rejected, not {disposition!r}."
        )

    variant = return_item.order_item.variant
    is_decant = DecantSource.objects.filter(decant_variant=variant).exists()

    if disposition == ReturnItem.DISPOSITION_RESTOCKED_RETAIL and is_decant:
        raise ReturnInspectionError(
            "A decant has no independent retail stock pool — it cannot be disposed as restocked_retail."
        )

    if disposition == ReturnItem.DISPOSITION_RESTOCKED_PARTIAL:
        if remaining_quantity_ml is None or remaining_quantity_ml <= 0:
            raise ReturnInspectionError("restocked_partial requires a positive remaining_quantity_ml.")
    elif remaining_quantity_ml is not None:
        raise ReturnInspectionError("remaining_quantity_ml is only valid for disposition=restocked_partial.")

    _apply_return_disposition_inventory(
        return_item=return_item,
        disposition=disposition,
        received_quantity=received_quantity,
        remaining_quantity_ml=remaining_quantity_ml,
        is_decant=is_decant,
        performed_by=inspected_by,
    )

    return_item.received_quantity = received_quantity
    return_item.disposition = disposition
    return_item.resolution = resolution
    return_item.remaining_quantity_ml = remaining_quantity_ml
    return_item.inspection_notes = inspection_notes
    return_item.inspected_by = inspected_by
    return_item.inspected_at = timezone.now()
    return_item.save(update_fields=[
        "received_quantity", "disposition", "resolution", "remaining_quantity_ml",
        "inspection_notes", "inspected_by", "inspected_at", "updated_at",
    ])

    _complete_return_if_fully_inspected(return_request)

    return return_item


def _apply_return_disposition_inventory(*, return_item, disposition, received_quantity, remaining_quantity_ml, is_decant, performed_by):
    """The only place a return's physical inventory impact is decided.
    Reuses the existing inventory schema exactly — no second mutation
    mechanism, no decant stock pool, no FIFO recalculation (FIFO belongs to
    reservation/consumption, not to restocking)."""
    from .models import ReturnItem

    if disposition == ReturnItem.DISPOSITION_REJECTED:
        return  # no inventory movement, no restoration — audit trail is the ReturnItem itself

    variant = return_item.order_item.variant
    warehouse = Warehouse.objects.get(is_default=True)

    if disposition == ReturnItem.DISPOSITION_DAMAGED:
        if is_decant:
            # The liquid was already deducted from the source variant's
            # inventory when the original order was packed — there is
            # nothing left to move now. No fake zero-delta movement; the
            # ReturnItem (disposition=damaged, inspection notes/by/at) is
            # the entire audit trail for this case.
            return

        stock, _ = InventoryStock.objects.select_for_update().get_or_create(
            variant=variant, warehouse=warehouse, stock_type=InventoryStock.STOCK_TYPE_DAMAGED,
            defaults={"quantity": Decimal("0")},
        )
        stock.quantity += received_quantity
        stock.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(
            variant=variant, warehouse=warehouse, stock_type=InventoryStock.STOCK_TYPE_DAMAGED,
            movement_type=StockMovement.MOVEMENT_RETURN_DAMAGED,
            quantity_delta=Decimal(received_quantity), reason=StockMovement.REASON_RETURN,
            source_order_item=return_item.order_item, source_return_item=return_item,
            performed_by=performed_by,
        )
        return

    if disposition == ReturnItem.DISPOSITION_RESTOCKED_RETAIL:
        # is_decant already rejected by the caller for this disposition.
        stock, _ = InventoryStock.objects.select_for_update().get_or_create(
            variant=variant, warehouse=warehouse, stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            defaults={"quantity": Decimal("0")},
        )
        stock.quantity += received_quantity
        stock.save(update_fields=["quantity", "updated_at"])
        StockMovement.objects.create(
            variant=variant, warehouse=warehouse, stock_type=InventoryStock.STOCK_TYPE_RETAIL,
            movement_type=StockMovement.MOVEMENT_RETURN_RESTOCKED_RETAIL,
            quantity_delta=Decimal(received_quantity), reason=StockMovement.REASON_RETURN,
            source_order_item=return_item.order_item, source_return_item=return_item,
            performed_by=performed_by,
        )
        return

    if disposition == ReturnItem.DISPOSITION_RESTOCKED_PARTIAL:
        if is_decant:
            source_variant = DecantSource.objects.get(decant_variant=variant).source_variant
        else:
            source_variant = variant

        txn = StockTransaction.objects.create(
            transaction_type=StockTransaction.TYPE_RETURN_DISPOSITION, performed_by=performed_by,
        )
        lot = PartialBottleLot.objects.create(
            variant=source_variant, warehouse=warehouse,
            remaining_ml=remaining_quantity_ml, reserved_ml=Decimal("0"),
            opened_at=timezone.now(), source_transaction=txn,
            source_return_item=return_item,
        )
        StockMovement.objects.create(
            transaction_group=txn, variant=source_variant, warehouse=warehouse, stock_type="partial",
            movement_type=StockMovement.MOVEMENT_RETURN_RESTOCKED_PARTIAL,
            quantity_delta=remaining_quantity_ml, reason=StockMovement.REASON_RETURN,
            partial_lot=lot, source_order_item=return_item.order_item, source_return_item=return_item,
            performed_by=performed_by,
        )
        return


def _complete_return_if_fully_inspected(return_request):
    from .models import Return

    if return_request.status != Return.STATUS_INSPECTION_PENDING:
        return
    if return_request.items.filter(disposition__isnull=True).exists():
        return
    return_request.status = Return.STATUS_COMPLETED
    return_request.save(update_fields=["status", "updated_at"])


# ── Refund calculation and processing ──────────────────────────────────────
#
# Kept entirely separate from inspection/disposition: finalize_return_item()
# never creates a Refund, and nothing here ever mutates inventory. Every
# amount is derived from the immutable checkout-time snapshots on
# Order/OrderItem (never today's ProductVariant price) and, once written to
# a ReturnItem/Return/Refund, is never recalculated — a correction after
# completion is a RefundAdjustment, never an edit to the original figures.
#
# TAX BOUNDARY (flagging, not guessing): OrderItem.final_paid_line_amount is
# defined (Phase 3) as net_line_amount + tax_amount — i.e. tax added ON TOP
# of the item price. That's a tax-EXCLUSIVE assumption. Indian MRP pricing
# is typically tax-INCLUSIVE by law, which would make that formula wrong
# once real GST lands (it would double-count tax). Today tax_amount is
# always 0.00 everywhere in this codebase, so the formula is numerically
# correct right now regardless of which semantic is eventually chosen —
# but this refund calculation inherits whatever OrderItem.final_paid_line_amount
# says, correct or not, without re-deriving it. Fixing the tax-inclusive/
# exclusive question is out of scope here (GST semantics are explicitly not
# to be invented) and needs a business decision before real tax_amount
# values ever appear.

SHIPPING_REFUND_REASONS = {
    "wrong_item", "wrong_variant", "transit_damage", "missing_items",
}  # manufacturing_defect deliberately excluded — matches the approved rule


class RefundError(Exception):
    """Raised when a refund calculation or lifecycle operation is invalid."""


def _already_claimed_product_refund(order_item):
    """Sum of refund_line_amount across every ReturnItem on this OrderItem
    whose Refund is not failed — a failed refund releases its claim on the
    historical paid amount, allowing a later return to claim it instead."""
    from .models import Refund, ReturnItem

    total = Decimal("0.00")
    for ri in ReturnItem.objects.filter(order_item=order_item).exclude(refund_line_amount__isnull=True):
        refund = ri.return_request.refunds.first()
        if refund is None or refund.refund_status != Refund.STATUS_FAILED:
            total += ri.refund_line_amount
    return total


def _already_claimed_surcharge_refund(order_item):
    from .models import Refund, ReturnItem

    total = Decimal("0.00")
    for ri in ReturnItem.objects.filter(order_item=order_item).exclude(surcharge_refund_amount=Decimal("0.00")):
        refund = ri.return_request.refunds.first()
        if refund is None or refund.refund_status != Refund.STATUS_FAILED:
            total += ri.surcharge_refund_amount
    return total


def _calculate_base_shipping_refund(return_request, order, items):
    """items: the Return's ReturnItems (already fetched/locked by the
    caller). Scoped to this one Return only — does not aggregate physical
    return coverage across multiple Returns against the same order, since
    that's added complexity no requirement has driven yet."""
    from .models import ReturnItem

    if return_request.base_shipping_refund_override is True:
        return order.base_shipping_charge
    if return_request.base_shipping_refund_override is False:
        return Decimal("0.00")

    total_order_quantity = sum(oi.quantity for oi in order.items.all())
    total_returned_quantity = sum(ri.received_quantity or 0 for ri in items)
    covers_entire_order = total_returned_quantity == total_order_quantity
    all_refund_resolution = all(ri.resolution == ReturnItem.RESOLUTION_REFUND for ri in items)
    all_reasons_qualify = all(ri.reason in SHIPPING_REFUND_REASONS for ri in items)

    if covers_entire_order and all_refund_resolution and all_reasons_qualify:
        return order.base_shipping_charge
    return Decimal("0.00")


@transaction.atomic
def create_refund_for_return(return_request, refund_method=None, approved_by=None):
    """Calculate and record the refund owed for a completed Return. Does
    not move money — this only creates the financial record, status
    STATUS_PENDING. Locks the Return, Order, and every referenced OrderItem
    before computing anything, so two concurrent refund calculations
    against overlapping OrderItems can't both succeed past their
    historical-amount caps.

    refund_method is optional and defaults to unset: there is no live
    payment/refund gateway to infer a channel from, so a staff member
    chooses it explicitly via set_refund_method() before the refund can
    move to processing.
    """
    from .models import Order, OrderItem, Refund, Return, ReturnItem

    return_request = Return.objects.select_for_update().get(pk=return_request.pk)
    if return_request.status != Return.STATUS_COMPLETED:
        raise RefundError(
            f"Cannot create a refund for a return in status={return_request.status}; it must be completed."
        )
    if Refund.objects.filter(return_request=return_request).exists():
        raise RefundError("A refund has already been created for this return.")

    order = Order.objects.select_for_update().get(pk=return_request.order_id)
    items = list(return_request.items.select_for_update().select_related("order_item"))

    refund_items = [item for item in items if item.resolution == ReturnItem.RESOLUTION_REFUND]
    if not refund_items:
        raise RefundError("This return has no resolution=refund items — there is nothing to refund.")

    total_amount = Decimal("0.00")

    for item in refund_items:
        order_item = OrderItem.objects.select_for_update().get(pk=item.order_item_id)

        unit_paid = order_item.final_paid_line_amount / order_item.quantity
        line_refund = (unit_paid * item.received_quantity).quantize(Decimal("0.01"))

        already_claimed = _already_claimed_product_refund(order_item)
        if already_claimed + line_refund > order_item.final_paid_line_amount:
            raise RefundError(
                f"Refund for order item {order_item.pk} ({line_refund}) plus what's already "
                f"claimed ({already_claimed}) would exceed the historical amount paid "
                f"({order_item.final_paid_line_amount})."
            )

        surcharge_refund = Decimal("0.00")
        if item.reason in SHIPPING_REFUND_REASONS and order_item.shipping_surcharge > 0:
            unit_surcharge = order_item.shipping_surcharge / order_item.quantity
            surcharge_refund = (unit_surcharge * item.received_quantity).quantize(Decimal("0.01"))

            already_claimed_surcharge = _already_claimed_surcharge_refund(order_item)
            if already_claimed_surcharge + surcharge_refund > order_item.shipping_surcharge:
                raise RefundError(
                    f"Surcharge refund for order item {order_item.pk} would exceed the "
                    f"original surcharge ({order_item.shipping_surcharge})."
                )

        item.refund_line_amount = line_refund
        item.surcharge_refund_amount = surcharge_refund
        item.save(update_fields=["refund_line_amount", "surcharge_refund_amount", "updated_at"])

        total_amount += line_refund + surcharge_refund

    base_shipping_refund = _calculate_base_shipping_refund(return_request, order, items)
    return_request.base_shipping_refund_amount = base_shipping_refund
    return_request.save(update_fields=["base_shipping_refund_amount", "updated_at"])
    total_amount += base_shipping_refund

    refund = Refund.objects.create(
        return_request=return_request,
        refund_amount=total_amount,
        refund_status=Refund.STATUS_PENDING,
        refund_method=refund_method,
        approved_by=approved_by,
    )
    return refund


@transaction.atomic
def set_refund_method(refund, refund_method):
    """Explicitly set/change the refund method while it's still pending.
    There is no live gateway to infer this from, so it must always be a
    deliberate staff choice.
    """
    from .models import Refund

    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.refund_status != Refund.STATUS_PENDING:
        raise RefundError(f"Cannot change refund method for a refund in status={refund.refund_status}.")
    refund.refund_method = refund_method
    refund.save(update_fields=["refund_method", "updated_at"])
    return refund


@transaction.atomic
def mark_refund_processing(refund):
    from .models import Refund

    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.refund_status != Refund.STATUS_PENDING:
        raise RefundError(f"Cannot mark processing a refund in status={refund.refund_status}.")
    if not refund.refund_method:
        raise RefundError("Cannot mark processing a refund with no refund_method set.")
    refund.refund_status = Refund.STATUS_PROCESSING
    refund.save(update_fields=["refund_status", "updated_at"])
    return refund


@transaction.atomic
def complete_refund(refund, refund_reference, processed_by, notes=""):
    """processing -> completed. Never called for external gateway execution
    — this only records that a (manual, for now) payment was made. Once
    completed, refund_amount is immutable; any later correction must be a
    RefundAdjustment. If this refund brings the order's cumulative
    completed refunds up to its full refundable total, the order moves to
    `refunded` — otherwise it stays `delivered` (see _maybe_mark_order_refunded)."""
    from .models import Refund

    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.refund_status != Refund.STATUS_PROCESSING:
        raise RefundError(f"Cannot complete a refund in status={refund.refund_status}.")

    refund.refund_status = Refund.STATUS_COMPLETED
    refund.refund_reference = refund_reference
    refund.processed_by = processed_by
    refund.processed_at = timezone.now()
    if notes:
        refund.notes = notes
    refund.save(update_fields=[
        "refund_status", "refund_reference", "processed_by", "processed_at", "notes", "updated_at",
    ])

    _maybe_mark_order_refunded(refund.return_request.order_id)

    return refund


@transaction.atomic
def fail_refund(refund, failure_reason, processed_by):
    """processing -> failed. Not terminal — releases this refund's claim on
    the historical paid amounts (see _already_claimed_*), so a later return
    or a retried refund can claim them instead."""
    from .models import Refund

    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.refund_status != Refund.STATUS_PROCESSING:
        raise RefundError(f"Cannot fail a refund in status={refund.refund_status}.")
    refund.refund_status = Refund.STATUS_FAILED
    refund.failure_reason = failure_reason
    refund.processed_by = processed_by
    refund.processed_at = timezone.now()
    refund.save(update_fields=[
        "refund_status", "failure_reason", "processed_by", "processed_at", "updated_at",
    ])
    return refund


def _maybe_mark_order_refunded(order_id):
    """delivered -> refunded only when cumulative COMPLETED refunds cover
    the order's full refundable total (total minus the always-non-refundable
    cod_handling_charge/convenience_fee). A partial refund leaves the order
    at `delivered`, per the approved rule."""
    from .models import Order, Refund

    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.STATUS_DELIVERED:
        return

    refundable_total = order.total - order.cod_handling_charge - order.convenience_fee
    completed_total = Refund.objects.filter(
        return_request__order=order, refund_status=Refund.STATUS_COMPLETED,
    ).aggregate(total=Sum("refund_amount"))["total"] or Decimal("0.00")

    if completed_total >= refundable_total:
        order.status = Order.STATUS_REFUNDED
        order.save(update_fields=["status", "updated_at"])


@transaction.atomic
def create_refund_adjustment(refund, adjustment_amount, reason, approved_by):
    """Record a correction against an already-completed refund without
    touching its original refund_amount. Only valid against a completed
    refund — a pending/processing refund's amount can simply be corrected
    directly before it's completed, since nothing has been promised yet."""
    from .models import Refund, RefundAdjustment

    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.refund_status != Refund.STATUS_COMPLETED:
        raise RefundError("Adjustments can only be recorded against a completed refund.")

    return RefundAdjustment.objects.create(
        refund=refund, adjustment_amount=adjustment_amount, reason=reason, approved_by=approved_by,
    )
