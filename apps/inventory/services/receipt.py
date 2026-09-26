"""
Purchase receipt stock-in: the ledger side of posting a goods receipt
(apps.purchases.services.post_goods_receipt). Must run inside the
caller's transaction; it never commits on its own.

Stock rows are locked in a fixed order (variant, then stock type) so two
receipts can never deadlock each other. A missing InventoryStock row is
created through get_or_create, which relies on the
unique_variant_warehouse_stock_type constraint: a concurrent creator
blocks on the unique index, gets IntegrityError, and re-reads the row the
other transaction created (with the lock) instead of duplicating it.
"""

from collections import defaultdict
from decimal import Decimal

from ..models import InventoryStock, StockMovement, StockTransaction


def receive_purchased_stock(*, warehouse, supplier, lines, performed_by, notes=""):
    """Add physically received units to stock.

    `lines` is a list of objects with variant_id, stock_type and quantity
    (goods receipt lines). Creates one StockTransaction and one
    purchase_in StockMovement per line, linked back to the line. Returns
    the StockTransaction."""
    per_stock = defaultdict(int)
    for line in lines:
        per_stock[(line.variant_id, line.stock_type)] += line.quantity

    stocks = {}
    for variant_id, stock_type in sorted(per_stock):
        stock, _ = InventoryStock.objects.select_for_update().get_or_create(
            variant_id=variant_id, warehouse=warehouse, stock_type=stock_type,
            defaults={"quantity": Decimal("0")},
        )
        stocks[(variant_id, stock_type)] = stock

    for key, quantity in per_stock.items():
        stock = stocks[key]
        stock.quantity += quantity
        stock.save(update_fields=["quantity", "updated_at"])

    transaction_group = StockTransaction.objects.create(
        transaction_type=StockTransaction.TYPE_PURCHASE_RECEIPT,
        performed_by=performed_by,
        notes=notes,
    )
    StockMovement.objects.bulk_create([
        StockMovement(
            transaction_group=transaction_group,
            variant_id=line.variant_id,
            warehouse=warehouse,
            stock_type=line.stock_type,
            movement_type=StockMovement.MOVEMENT_PURCHASE_IN,
            quantity_delta=Decimal(line.quantity),
            reason=StockMovement.REASON_PURCHASE,
            supplier=supplier,
            source_receipt_line=line,
            performed_by=performed_by,
        )
        for line in lines
    ])
    return transaction_group
