"""
Manual inventory adjustment service — the only legitimate path to change
InventoryStock.quantity or PartialBottleLot.remaining_ml outside of the
reservation/consumption (apps.inventory.services.reservation), return-
disposition (apps.orders.services._apply_return_disposition_inventory)
and purchase receipt (apps.inventory.services.receipt) flows. Purchased
stock is received through a goods receipt, not an adjustment. Every adjustment is atomic, row-locked, and produces a real
StockMovement — there is no way to change these fields without a
corresponding ledger entry, and a zero-delta adjustment is rejected rather
than producing a fake audit-only movement.
"""

from decimal import Decimal

from django.db import transaction

from ..models import InventoryStock, PartialBottleLot, StockMovement


class InventoryAdjustmentError(Exception):
    """Raised when a manual adjustment is invalid or would leave inventory
    in an inconsistent state."""


@transaction.atomic
def adjust_inventory_stock(stock, quantity_delta, reason, notes="", performed_by=None):
    """Apply a signed quantity_delta to an existing InventoryStock row —
    receiving purchased stock, a stocktaking correction, a manual
    write-off, etc. — and record the corresponding StockMovement."""
    if quantity_delta == 0:
        raise InventoryAdjustmentError("quantity_delta cannot be zero.")

    stock = InventoryStock.objects.select_for_update().get(pk=stock.pk)
    new_quantity = stock.quantity + quantity_delta
    if new_quantity < 0:
        raise InventoryAdjustmentError(
            f"Adjustment would leave quantity at {new_quantity}, which is negative."
        )

    stock.quantity = new_quantity
    stock.save(update_fields=["quantity", "updated_at"])

    StockMovement.objects.create(
        variant=stock.variant, warehouse=stock.warehouse, stock_type=stock.stock_type,
        movement_type=StockMovement.MOVEMENT_ADJUSTMENT, quantity_delta=quantity_delta,
        reason=reason, notes=notes, performed_by=performed_by,
    )
    return stock


@transaction.atomic
def adjust_partial_lot(lot, ml_delta, reason, notes="", performed_by=None):
    """Apply a signed ml_delta to a PartialBottleLot's remaining_ml — a
    correction, an evaporation/spillage write-off, etc. If this brings
    remaining_ml to exactly 0 the lot is marked depleted, matching normal
    consumption behavior; it is never deleted."""
    if ml_delta == 0:
        raise InventoryAdjustmentError("ml_delta cannot be zero.")

    lot = PartialBottleLot.objects.select_for_update().get(pk=lot.pk)
    new_remaining = lot.remaining_ml + ml_delta
    if new_remaining < lot.reserved_ml:
        raise InventoryAdjustmentError(
            f"Adjustment would leave remaining_ml ({new_remaining}) below the "
            f"currently reserved_ml ({lot.reserved_ml})."
        )

    lot.remaining_ml = new_remaining
    if new_remaining == 0:
        lot.is_depleted = True
    lot.save(update_fields=["remaining_ml", "is_depleted", "updated_at"])

    StockMovement.objects.create(
        variant=lot.variant, warehouse=lot.warehouse, stock_type="partial",
        movement_type=StockMovement.MOVEMENT_ADJUSTMENT, quantity_delta=ml_delta,
        reason=reason, notes=notes, partial_lot=lot, performed_by=performed_by,
    )
    return lot
