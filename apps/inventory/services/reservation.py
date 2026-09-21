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

from decimal import ROUND_CEILING, Decimal

from django.db import transaction
from django.utils import timezone

from .. import models as inv_models


class InsufficientStockError(Exception):
    """Not enough available capacity to satisfy a requested reservation."""


class InvalidReservationStateError(Exception):
    """The reservation is not in a state that allows the requested action."""


def _lock_retail_stock(variant, warehouse):
    stock, _ = inv_models.InventoryStock.objects.select_for_update().get_or_create(
        variant=variant,
        warehouse=warehouse,
        stock_type=inv_models.InventoryStock.STOCK_TYPE_RETAIL,
        defaults={"quantity": Decimal("0")},
    )
    return stock


def _lock_partial_lots(variant, warehouse):
    return list(
        inv_models.PartialBottleLot.objects.select_for_update()
        .filter(variant=variant, warehouse=warehouse, is_depleted=False)
        .order_by("opened_at")
    )


@transaction.atomic
def reserve_for_order_item(order_item, warehouse):
    """Reserve inventory for one OrderItem. Returns the created StockReservation.

    Dispatches to a direct-sale or decant-fulfillment reservation depending
    on whether the ordered variant has a DecantSource. Raises
    InsufficientStockError if there isn't enough available capacity.
    """
    variant = order_item.variant
    decant_source = inv_models.DecantSource.objects.filter(decant_variant=variant).first()

    if decant_source is None:
        return _reserve_direct(order_item, variant, warehouse)
    return _reserve_decant(order_item, decant_source, warehouse)


def _reserve_direct(order_item, variant, warehouse):
    stock = _lock_retail_stock(variant, warehouse)
    needed = Decimal(order_item.quantity)

    if stock.available < needed:
        raise InsufficientStockError(
            f"Only {stock.available} unit(s) of {variant} available at {warehouse}, needed {needed}."
        )

    stock.quantity_reserved += needed
    stock.save(update_fields=["quantity_reserved", "updated_at"])

    reservation = inv_models.StockReservation.objects.create(
        order_item=order_item,
        variant=variant,
        warehouse=warehouse,
        purpose=inv_models.StockReservation.PURPOSE_DIRECT_SALE,
        quantity=needed,
    )
    inv_models.StockReservationAllocation.objects.create(
        reservation=reservation,
        allocation_type=inv_models.StockReservationAllocation.ALLOCATION_RETAIL_UNIT,
        inventory_stock=stock,
        units=needed,
    )
    return reservation


def _reserve_decant(order_item, decant_source, warehouse):
    source_variant = decant_source.source_variant
    size_ml = Decimal(source_variant.size_ml)
    needed_ml = Decimal(order_item.quantity) * decant_source.decant_volume_ml

    lots = _lock_partial_lots(source_variant, warehouse)
    retail_stock = _lock_retail_stock(source_variant, warehouse)

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
    what should be released — only reverses what was actually recorded."""
    reservation = inv_models.StockReservation.objects.select_for_update().get(pk=reservation.pk)
    if reservation.status not in (
        inv_models.StockReservation.STATUS_HELD,
        inv_models.StockReservation.STATUS_CONFIRMED,
    ):
        raise InvalidReservationStateError(f"Cannot release a reservation in status={reservation.status}")

    for allocation in reservation.allocations.select_for_update():
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

    allocations = list(reservation.allocations.select_for_update())

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
