"""
Reservation service — the single authoritative path for reserving,
confirming, releasing, and consuming inventory against an OrderItem.

Rules enforced here (per the approved design, not to be duplicated
elsewhere):
    - A bottle reserved for direct retail sale and a bottle reserved to be
      opened for decanting compete for the same InventoryStock(retail)
      counter — there is no separate decant-only reservation pool.
    - FIFO across PartialBottleLot happens once, at reservation time.
      Release and consume never re-run FIFO — they only reverse/apply the
      exact StockReservationAllocation rows already recorded.
    - Every mutating function runs inside transaction.atomic() and takes
      select_for_update() on the rows it touches before reading them.
"""

import datetime
import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .. import models as inv_models

logger = logging.getLogger(__name__)


class InsufficientStockError(Exception):
    """Not enough available capacity to satisfy a requested reservation."""


class InvalidReservationStateError(Exception):
    """The reservation is not in a state that allows the requested action."""


def _refuse_active_incoming_allocations(reservation):
    """Active incoming (booked) units are not on the shelf yet, so they can
    never be consumed; GRN conversion or reallocation (P2C) resolves them
    first. Historical (converted/reallocated/released) rows are skipped."""
    Allocation = inv_models.StockReservationAllocation
    if reservation.allocations.filter(
        allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
        incoming_status=Allocation.INCOMING_ACTIVE,
    ).exists():
        raise InvalidReservationStateError(
            "This reservation has active incoming (booked) allocations that have not "
            "arrived yet."
        )


def _release_active_incoming_allocations(reservation):
    """Mark active incoming rows released. They hold no stock, so nothing
    else changes; their units count as incoming sellable again."""
    Allocation = inv_models.StockReservationAllocation
    ids = list(
        reservation.allocations.filter(
            allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
            incoming_status=Allocation.INCOMING_ACTIVE,
        )
        .select_for_update()
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if ids:
        now = timezone.now()
        Allocation.objects.filter(pk__in=ids).update(
            incoming_status=Allocation.INCOMING_RELEASED,
            incoming_resolved_at=now,
            updated_at=now,
        )


def _physical_allocations(reservation):
    """Locked physical allocation rows. Incoming rows are never physical
    capacity, whatever their status."""
    return reservation.allocations.exclude(
        allocation_type=inv_models.StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE
    ).select_for_update()


def _lock_retail_stock(variant_id, warehouse):
    stock, _ = inv_models.InventoryStock.objects.select_for_update().get_or_create(
        variant_id=variant_id,
        warehouse=warehouse,
        stock_type=inv_models.InventoryStock.STOCK_TYPE_RETAIL,
        defaults={"quantity": Decimal("0")},
    )
    return stock


def _lock_partial_lots(variant_id, warehouse):
    return list(
        inv_models.PartialBottleLot.objects.select_for_update()
        .filter(variant_id=variant_id, warehouse=warehouse, is_depleted=False)
        .order_by("opened_at")
    )


def _lock_candidate_lines(variant_ids, warehouse):
    """Phase 1 lock: PO lines that may back incoming sales, by id. The PO
    row itself is not locked (spec §8)."""
    from apps.purchases.models import PurchaseOrder, PurchaseOrderLine

    return list(
        PurchaseOrderLine.objects.select_for_update(of=("self",))
        .filter(
            variant_id__in=variant_ids,
            purchase_order__warehouse=warehouse,
            purchase_order__status__in=PurchaseOrder.BOOKABLE_STATUSES,
            confirmed_booked_quantity__gt=0,
        )
        .order_by("pk")
    )


def _incoming_capacity(variant_ids, warehouse):
    """{variant_id: [[po_line, incoming units left], ...]} in allocation
    priority order: expected_date (nulls last), then PO id, then line id.

    The parent PO status is re-read after the line lock, so a PO closed or
    cancelled meanwhile drops out (race with P2C close/cancel guards)."""
    lines = _lock_candidate_lines(variant_ids, warehouse)
    capacity = {}
    for line, units in _bookable_by_priority(lines):
        capacity.setdefault(line.variant_id, []).append([line, units])
    return capacity


def _bookable_by_priority(lines):
    """[(line, incoming units left)] for already-locked PO lines, in
    allocation priority order: expected_date (nulls last), PO id, line id.
    The parent PO status is re-read after the line lock; lines of a PO that
    is no longer issued/partially received, or with nothing left, drop out."""
    from apps.purchases import selectors as purchase_selectors
    from apps.purchases.models import PurchaseOrder

    if not lines:
        return []
    po_rows = {
        pk: (status, expected_date)
        for pk, status, expected_date in PurchaseOrder.objects.filter(
            pk__in={line.purchase_order_id for line in lines}
        ).values_list("pk", "status", "expected_date")
    }
    lines = [
        line for line in lines
        if po_rows[line.purchase_order_id][0] in PurchaseOrder.BOOKABLE_STATUSES
    ]
    amounts = purchase_selectors.incoming_sellable_amounts(lines)

    def priority(line):
        expected_date = po_rows[line.purchase_order_id][1]
        return (
            expected_date is None, expected_date or datetime.date.min,
            line.purchase_order_id, line.pk,
        )

    return [(line, amounts[line.pk]) for line in sorted(lines, key=priority) if amounts[line.pk] > 0]


_ACTIVE_RESERVATION_STATUSES = (
    inv_models.StockReservation.STATUS_HELD,
    inv_models.StockReservation.STATUS_CONFIRMED,
)


def _active_incoming_rows():
    Allocation = inv_models.StockReservationAllocation
    return Allocation.objects.filter(
        allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
        incoming_status=Allocation.INCOMING_ACTIVE,
    )


def _lock_reservations(reservation_ids):
    """{id: StockReservation} locked by id; each must still be held or
    confirmed (an active incoming row on a finished reservation is corrupt)."""
    reservations = {
        reservation.pk: reservation
        for reservation in inv_models.StockReservation.objects.select_for_update()
        .filter(pk__in=sorted(set(reservation_ids)))
        .order_by("pk")
    }
    for reservation in reservations.values():
        if reservation.status not in _ACTIVE_RESERVATION_STATUSES:
            raise InvalidReservationStateError(
                f"Reservation {reservation.pk} is {reservation.status} but still has active "
                "incoming allocations."
            )
    return reservations


def _split_remainder(allocation, units, purchase_order_line_id=None):
    """New active incoming row carrying `units` of `allocation`, with
    split_from lineage (spec §5.5.1). It may sit on another PO line
    (multi-source reallocation)."""
    Allocation = inv_models.StockReservationAllocation
    return Allocation.objects.create(
        reservation=allocation.reservation,
        allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
        purchase_order_line_id=purchase_order_line_id or allocation.purchase_order_line_id,
        units=units,
        incoming_status=Allocation.INCOMING_ACTIVE,
        split_from=allocation,
    )


def _physical_replacement(reservation, stock, units):
    stock.quantity_reserved += units
    return inv_models.StockReservationAllocation.objects.create(
        reservation=reservation,
        allocation_type=inv_models.StockReservationAllocation.ALLOCATION_RETAIL_UNIT,
        inventory_stock=stock,
        units=units,
    )


def _check_reserved_fits(stock):
    if stock.quantity_reserved > stock.quantity:
        raise InsufficientStockError(
            f"{stock.variant} at {stock.warehouse}: reserving {stock.quantity_reserved} "
            f"would exceed the {stock.quantity} unit(s) in stock."
        )


def lock_active_incoming_for_lines(po_line_ids):
    """GRN conversion, lock phase (spec §5.5, §8). The caller already holds
    the PurchaseOrderLine locks, so no new active row can appear on these
    lines. Locks the owning reservations by id, then the active incoming
    allocations by id, before any stock row is locked.

    Returns the allocations in conversion (FIFO) order: reservation
    created_at, reservation id, allocation id."""
    po_line_ids = list(po_line_ids)
    if not po_line_ids:
        return []
    rows = _active_incoming_rows().filter(purchase_order_line_id__in=po_line_ids)
    reservations = _lock_reservations(rows.values_list("reservation_id", flat=True))
    allocations = list(
        rows.filter(reservation_id__in=list(reservations)).select_for_update().order_by("pk")
    )
    for allocation in allocations:
        allocation.reservation = reservations[allocation.reservation_id]
    allocations.sort(
        key=lambda a: (a.reservation.created_at, a.reservation_id, a.pk)
    )
    return allocations


def convert_incoming_allocations(allocations, receipt_lines, warehouse):
    """GRN conversion, convert phase. `allocations` come from
    lock_active_incoming_for_lines; `receipt_lines` are the posted retail
    GoodsReceiptLines, whose units are already in stock.

    Per receipt line (pk order), up to its quantity, each allocation in
    FIFO order becomes a retail_unit row on the same reservation. A part
    conversion leaves an active remainder (split_from = original) that a
    later line may convert. Surplus stays free stock. Raises
    InsufficientStockError if quantity_reserved would exceed quantity; the
    caller's transaction then rolls back."""
    Allocation = inv_models.StockReservationAllocation
    queues = {}
    for allocation in allocations:
        queues.setdefault(allocation.purchase_order_line_id, []).append(allocation)
    stocks = {}
    now = timezone.now()
    for line in sorted(receipt_lines, key=lambda line: line.pk):
        queue = queues.get(line.po_line_id)
        if not queue:
            continue
        stock = stocks.get(line.variant_id)
        if stock is None:
            stock = stocks[line.variant_id] = _lock_retail_stock(line.variant_id, warehouse)
        budget = Decimal(line.quantity)
        while budget > 0 and queue:
            allocation = queue.pop(0)
            take = min(allocation.units, budget)
            physical = _physical_replacement(allocation.reservation, stock, take)
            if take < allocation.units:
                queue.insert(0, _split_remainder(allocation, allocation.units - take))
            allocation.incoming_status = Allocation.INCOMING_CONVERTED
            allocation.converted_by_receipt_line = line
            allocation.replacement = physical
            allocation.incoming_resolved_at = now
            allocation.save(update_fields=[
                "incoming_status", "converted_by_receipt_line", "replacement",
                "incoming_resolved_at", "updated_at",
            ])
            budget -= take
    for stock in stocks.values():
        _check_reserved_fits(stock)
        stock.save(update_fields=["quantity_reserved", "updated_at"])


@transaction.atomic
def reallocate_incoming_allocation(allocation, performed_by):
    """Move an active incoming allocation off a PO line that will not
    deliver (spec §5.6). Covers its units with free physical stock first,
    then other confirmed incoming lines in checkout priority; the current
    line is never a source. All or nothing: if the units cannot be fully
    covered, raises InsufficientStockError and changes nothing. The order
    is never cancelled here.

    The first source becomes `replacement`; further incoming pieces are new
    active rows with split_from = the original (lineage only, possibly on
    another PO line). The original becomes `reallocated`.

    Lock order (spec §8): candidate PO lines by id (PO rows are not locked;
    their status is re-read after the line lock) -> reservation ->
    allocation -> retail stock."""
    from apps.purchases import selectors as purchase_selectors
    from apps.purchases.models import PurchaseOrder, PurchaseOrderLine

    Allocation = inv_models.StockReservationAllocation
    row = Allocation.objects.filter(pk=allocation.pk).values(
        "allocation_type", "purchase_order_line_id", "reservation_id",
        "reservation__warehouse_id", "purchase_order_line__variant_id",
    ).get()
    if row["allocation_type"] != Allocation.ALLOCATION_INCOMING_PO_LINE:
        raise InvalidReservationStateError("Only incoming allocations can be reallocated.")
    current_line_id = row["purchase_order_line_id"]
    variant_id = row["purchase_order_line__variant_id"]
    warehouse_id = row["reservation__warehouse_id"]

    lines = list(
        PurchaseOrderLine.objects.select_for_update(of=("self",))
        .filter(
            Q(pk=current_line_id)
            | Q(
                variant_id=variant_id,
                purchase_order__warehouse_id=warehouse_id,
                purchase_order__status__in=PurchaseOrder.BOOKABLE_STATUSES,
                confirmed_booked_quantity__gt=0,
            )
        )
        .order_by("pk")
    )
    reservation = _lock_reservations([row["reservation_id"]])[row["reservation_id"]]
    allocation = Allocation.objects.select_for_update().get(pk=allocation.pk)
    if (
        allocation.incoming_status != Allocation.INCOMING_ACTIVE
        or allocation.purchase_order_line_id != current_line_id
    ):
        raise InvalidReservationStateError(
            "Only an active incoming allocation can be reallocated."
        )
    allocation.reservation = reservation
    stock = _lock_retail_stock(variant_id, reservation.warehouse)

    needed = allocation.units
    on_hand = max(stock.available, Decimal("0")).to_integral_value(rounding=ROUND_FLOOR)
    physical = min(on_hand, needed)
    remaining = needed - physical
    sources = []
    others = [line for line in lines if line.pk != current_line_id]
    for line, units in _bookable_by_priority(others):
        if remaining <= 0:
            break
        take = min(units, remaining)
        sources.append((line, take))
        remaining -= take
    if remaining > 0:
        fmt = purchase_selectors.format_units
        raise InsufficientStockError(
            f"Cannot reallocate {reservation.variant} for order item "
            f"{reservation.order_item_id}: {fmt(remaining)} of {fmt(needed)} unit(s) "
            "could not be covered by free stock or other confirmed incoming stock."
        )

    replacement = None
    if physical > 0:
        replacement = _physical_replacement(reservation, stock, physical)
        _check_reserved_fits(stock)
        stock.save(update_fields=["quantity_reserved", "updated_at"])
    for line, units in sources:
        if replacement is None:
            replacement = Allocation.objects.create(
                reservation=reservation,
                allocation_type=Allocation.ALLOCATION_INCOMING_PO_LINE,
                purchase_order_line=line,
                units=units,
                incoming_status=Allocation.INCOMING_ACTIVE,
            )
        else:
            _split_remainder(allocation, units, purchase_order_line_id=line.pk)

    allocation.incoming_status = Allocation.INCOMING_REALLOCATED
    allocation.replacement = replacement
    allocation.incoming_resolved_at = timezone.now()
    allocation.save(update_fields=[
        "incoming_status", "replacement", "incoming_resolved_at", "updated_at",
    ])
    logger.info(
        "Incoming allocation %s reallocated by user %s (replacement %s).",
        allocation.pk, getattr(performed_by, "pk", None), replacement.pk,
    )
    return allocation


@transaction.atomic
def reserve_for_order_item(order_item, warehouse):
    """Reserve physical inventory for one OrderItem (in-store sales). Returns
    the created StockReservation. Never uses booked incoming stock.

    Dispatches to a direct-sale or decant-fulfillment reservation depending
    on whether the ordered variant has a DecantSource. Raises
    InsufficientStockError if there isn't enough available capacity.
    """
    return reserve_order_items([order_item], warehouse, allow_incoming=False)[0]


@transaction.atomic
def reserve_order_items(order_items, warehouse, allow_incoming=False):
    """Reserve inventory for every OrderItem of one online order, all or
    nothing. Returns the StockReservations in item order.

    Lock order (spec §8): candidate incoming PurchaseOrderLines by id (only
    when allow_incoming), then retail InventoryStock by variant id, then
    PartialBottleLots. Direct-sale items take physical stock first, then
    booked incoming units; decant items are physical only. Any shortfall
    raises InsufficientStockError and rolls the whole order back.
    """
    order_items = list(order_items)
    variant_ids = {item.variant_id for item in order_items}
    decant_sources = {
        source.decant_variant_id: source
        for source in inv_models.DecantSource.objects.select_related("source_variant").filter(
            decant_variant_id__in=variant_ids
        )
    }
    direct_ids = sorted(variant_ids - decant_sources.keys())
    source_ids = sorted({source.source_variant_id for source in decant_sources.values()})

    incoming = _incoming_capacity(direct_ids, warehouse) if allow_incoming and direct_ids else {}
    stocks = {
        variant_id: _lock_retail_stock(variant_id, warehouse)
        for variant_id in sorted(set(direct_ids) | set(source_ids))
    }
    lots = {variant_id: _lock_partial_lots(variant_id, warehouse) for variant_id in source_ids}

    reservations = []
    for item in order_items:
        source = decant_sources.get(item.variant_id)
        if source is None:
            reservations.append(_reserve_direct(
                item, stocks[item.variant_id], incoming.get(item.variant_id, []), warehouse,
            ))
        else:
            reservations.append(_reserve_decant(
                item, source, stocks[source.source_variant_id],
                lots[source.source_variant_id], warehouse,
            ))
    return reservations


def _reserve_direct(order_item, stock, incoming, warehouse):
    """Physical units first, then incoming units from `incoming` (the
    shared, priority-ordered [[line, units left], ...] for this variant)."""
    variant = order_item.variant
    needed = Decimal(order_item.quantity)
    incoming_total = sum((units for _, units in incoming), Decimal("0"))

    if incoming_total:
        # Whole physical bottles only, so the incoming remainder is whole too.
        on_hand = max(stock.available, Decimal("0"))
        physical = min(on_hand.to_integral_value(rounding=ROUND_FLOOR), needed)
        available = on_hand + incoming_total
    else:
        physical = needed
        available = stock.available
    if available < needed:
        raise InsufficientStockError(
            f"Only {available} unit(s) of {variant} available at {warehouse}, needed {needed}."
        )
    incoming_needed = needed - physical
    if incoming_needed != incoming_needed.to_integral_value():
        raise ValueError(f"Incoming allocations must be whole units, got {incoming_needed}.")

    reservation = inv_models.StockReservation.objects.create(
        order_item=order_item,
        variant=variant,
        warehouse=warehouse,
        purpose=inv_models.StockReservation.PURPOSE_DIRECT_SALE,
        quantity=needed,
    )
    if physical > 0:
        stock.quantity_reserved += physical
        stock.save(update_fields=["quantity_reserved", "updated_at"])
        inv_models.StockReservationAllocation.objects.create(
            reservation=reservation,
            allocation_type=inv_models.StockReservationAllocation.ALLOCATION_RETAIL_UNIT,
            inventory_stock=stock,
            units=physical,
        )

    remaining = incoming_needed
    for entry in incoming:
        if remaining <= 0:
            break
        take = min(entry[1], remaining)
        if take <= 0:
            continue
        entry[1] -= take
        remaining -= take
        inv_models.StockReservationAllocation.objects.create(
            reservation=reservation,
            allocation_type=inv_models.StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
            purchase_order_line=entry[0],
            units=take,
            incoming_status=inv_models.StockReservationAllocation.INCOMING_ACTIVE,
        )
    return reservation


def _reserve_decant(order_item, decant_source, retail_stock, lots, warehouse):
    source_variant = decant_source.source_variant
    size_ml = Decimal(source_variant.size_ml)
    needed_ml = Decimal(order_item.quantity) * decant_source.decant_volume_ml

    available_ml = sum((lot.available_ml for lot in lots), Decimal("0")) + retail_stock.available * size_ml
    if available_ml < needed_ml:
        raise InsufficientStockError(
            f"Only {available_ml}ml decant capacity available for {source_variant} "
            f"at {warehouse}, needed {needed_ml}ml."
        )

    reservation = inv_models.StockReservation.objects.create(
        order_item=order_item,
        variant=source_variant,
        warehouse=warehouse,
        purpose=inv_models.StockReservation.PURPOSE_DECANT_FULFILLMENT,
        quantity=needed_ml,
    )

    remaining = needed_ml
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.available_ml, remaining)
        if take <= 0:
            continue
        lot.reserved_ml += take
        lot.save(update_fields=["reserved_ml", "updated_at"])
        inv_models.StockReservationAllocation.objects.create(
            reservation=reservation,
            allocation_type=inv_models.StockReservationAllocation.ALLOCATION_PARTIAL_LOT,
            partial_lot=lot,
            ml_amount=take,
        )
        remaining -= take

    if remaining > 0:
        bottles_needed = (remaining / size_ml).to_integral_value(rounding=ROUND_CEILING)
        retail_stock.quantity_reserved += bottles_needed
        retail_stock.save(update_fields=["quantity_reserved", "updated_at"])
        inv_models.StockReservationAllocation.objects.create(
            reservation=reservation,
            allocation_type=inv_models.StockReservationAllocation.ALLOCATION_RETAIL_UNIT,
            inventory_stock=retail_stock,
            units=bottles_needed,
            claimed_ml=remaining,
        )

    return reservation


@transaction.atomic
def confirm_reservation(reservation):
    """Flip a held reservation to confirmed — no quantity change, just marks
    it as no longer subject to any future abandoned-order release policy."""
    reservation = inv_models.StockReservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status != inv_models.StockReservation.STATUS_HELD:
        raise InvalidReservationStateError(f"Cannot confirm a reservation in status={reservation.status}")

    reservation.status = inv_models.StockReservation.STATUS_CONFIRMED
    reservation.save(update_fields=["status", "updated_at"])
    return reservation


@transaction.atomic
def release_reservation(reservation):
    """Release a held/confirmed reservation, restoring exactly the capacity
    recorded on its StockReservationAllocation rows. Never recalculates
    what should be released — only reverses what was actually recorded.
    Active incoming (booked) rows become `released`, with no stock change."""
    reservation = inv_models.StockReservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status not in (
        inv_models.StockReservation.STATUS_HELD,
        inv_models.StockReservation.STATUS_CONFIRMED,
    ):
        raise InvalidReservationStateError(f"Cannot release a reservation in status={reservation.status}")

    _release_active_incoming_allocations(reservation)
    for allocation in _physical_allocations(reservation):
        if allocation.allocation_type == inv_models.StockReservationAllocation.ALLOCATION_RETAIL_UNIT:
            stock = inv_models.InventoryStock.objects.select_for_update().get(pk=allocation.inventory_stock_id)
            stock.quantity_reserved -= allocation.units
            stock.save(update_fields=["quantity_reserved", "updated_at"])
        else:
            lot = inv_models.PartialBottleLot.objects.select_for_update().get(pk=allocation.partial_lot_id)
            lot.reserved_ml -= allocation.ml_amount
            lot.save(update_fields=["reserved_ml", "updated_at"])

    reservation.status = inv_models.StockReservation.STATUS_RELEASED
    reservation.resolved_at = timezone.now()
    reservation.save(update_fields=["status", "resolved_at", "updated_at"])
    return reservation


@transaction.atomic
def consume_reservation(reservation, performed_by=None):
    """Apply a reservation's allocations for real, at packing time. Consumes
    the exact recorded allocations — never re-runs FIFO or picks different
    lots than were reserved."""
    reservation = inv_models.StockReservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status not in (
        inv_models.StockReservation.STATUS_HELD,
        inv_models.StockReservation.STATUS_CONFIRMED,
    ):
        raise InvalidReservationStateError(f"Cannot consume a reservation in status={reservation.status}")
    _refuse_active_incoming_allocations(reservation)

    allocations = list(_physical_allocations(reservation))

    if reservation.purpose == inv_models.StockReservation.PURPOSE_DIRECT_SALE:
        for allocation in allocations:
            _consume_direct_allocation(reservation, allocation, performed_by)
    else:
        for allocation in allocations:
            if allocation.allocation_type == inv_models.StockReservationAllocation.ALLOCATION_PARTIAL_LOT:
                _consume_partial_lot_allocation(reservation, allocation, performed_by)
            else:
                _consume_bottle_opening_allocation(reservation, allocation, performed_by)

    reservation.status = inv_models.StockReservation.STATUS_CONSUMED
    reservation.resolved_at = timezone.now()
    reservation.save(update_fields=["status", "resolved_at", "updated_at"])
    return reservation


def _consume_direct_allocation(reservation, allocation, performed_by):
    stock = inv_models.InventoryStock.objects.select_for_update().get(pk=allocation.inventory_stock_id)
    stock.quantity -= allocation.units
    stock.quantity_reserved -= allocation.units
    stock.save(update_fields=["quantity", "quantity_reserved", "updated_at"])

    inv_models.StockMovement.objects.create(
        variant=stock.variant,
        warehouse=stock.warehouse,
        stock_type=stock.stock_type,
        movement_type=inv_models.StockMovement.MOVEMENT_SALE_OUT,
        quantity_delta=-allocation.units,
        reason=inv_models.StockMovement.REASON_SALE,
        source_order_item=reservation.order_item,
        performed_by=performed_by,
    )


def _consume_partial_lot_allocation(reservation, allocation, performed_by):
    lot = inv_models.PartialBottleLot.objects.select_for_update().get(pk=allocation.partial_lot_id)
    lot.remaining_ml -= allocation.ml_amount
    lot.reserved_ml -= allocation.ml_amount
    if lot.remaining_ml <= 0:
        lot.is_depleted = True
    lot.save(update_fields=["remaining_ml", "reserved_ml", "is_depleted", "updated_at"])

    inv_models.StockMovement.objects.create(
        variant=lot.variant,
        warehouse=lot.warehouse,
        stock_type="partial",
        movement_type=inv_models.StockMovement.MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL,
        quantity_delta=-allocation.ml_amount,
        reason=inv_models.StockMovement.REASON_DECANT_PREPARATION,
        partial_lot=lot,
        source_order_item=reservation.order_item,
        performed_by=performed_by,
    )


def _consume_bottle_opening_allocation(reservation, allocation, performed_by):
    """A decant reservation whose fulfillment required opening (an) unopened
    bottle(s). Opens the bottle(s), claims what this order needs, and keeps
    whatever's left as a fresh unreserved PartialBottleLot — no volume is
    ever lost."""
    stock = inv_models.InventoryStock.objects.select_for_update().get(pk=allocation.inventory_stock_id)
    stock.quantity -= allocation.units
    stock.quantity_reserved -= allocation.units
    stock.save(update_fields=["quantity", "quantity_reserved", "updated_at"])

    txn = inv_models.StockTransaction.objects.create(
        transaction_type=inv_models.StockTransaction.TYPE_DECANT_BOTTLE_OPENED,
        performed_by=performed_by,
    )
    inv_models.StockMovement.objects.create(
        transaction_group=txn,
        variant=stock.variant,
        warehouse=stock.warehouse,
        stock_type=inv_models.InventoryStock.STOCK_TYPE_RETAIL,
        movement_type=inv_models.StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT,
        quantity_delta=-allocation.units,
        reason=inv_models.StockMovement.REASON_DECANT_PREPARATION,
        source_order_item=reservation.order_item,
        performed_by=performed_by,
    )

    opened_volume_ml = allocation.units * Decimal(stock.variant.size_ml)
    inv_models.StockMovement.objects.create(
        transaction_group=txn,
        variant=stock.variant,
        warehouse=stock.warehouse,
        stock_type="partial",
        movement_type=inv_models.StockMovement.MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN,
        quantity_delta=opened_volume_ml,
        reason=inv_models.StockMovement.REASON_DECANT_PREPARATION,
        source_order_item=reservation.order_item,
        performed_by=performed_by,
    )

    leftover_ml = opened_volume_ml - allocation.claimed_ml
    new_lot = inv_models.PartialBottleLot.objects.create(
        variant=stock.variant,
        warehouse=stock.warehouse,
        remaining_ml=leftover_ml,
        opened_at=timezone.now(),
        source_transaction=txn,
        is_depleted=(leftover_ml <= 0),
    )
    inv_models.StockMovement.objects.create(
        transaction_group=txn,
        variant=stock.variant,
        warehouse=stock.warehouse,
        stock_type="partial",
        movement_type=inv_models.StockMovement.MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL,
        quantity_delta=-allocation.claimed_ml,
        reason=inv_models.StockMovement.REASON_DECANT_PREPARATION,
        partial_lot=new_lot,
        source_order_item=reservation.order_item,
        performed_by=performed_by,
    )
