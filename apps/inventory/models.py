from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class Warehouse(BaseModel):
    """A physical fulfillment location. Smerfume operates one today; the
    schema does not assume single-warehouse operation going forward."""

    name = models.CharField(max_length=100, unique=True)
    address_line1 = models.CharField(max_length=255, blank=True)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    pincode = models.CharField(max_length=6, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="unique_default_warehouse",
            ),
        ]

    def __str__(self):
        return self.name


class Supplier(BaseModel):
    """Vendor/supplier master data. Owned entirely in Django per DEC-006 —
    not sourced from or dependent on Zoho."""

    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.name


class InventoryStock(BaseModel):
    """Current on-hand/reserved quantity for one variant, at one warehouse,
    in one stock classification. Quantities are always whole units (pcs) —
    opened-bottle volume is tracked separately in PartialBottleLot."""

    STOCK_TYPE_RETAIL = "retail"
    STOCK_TYPE_TESTER = "tester"
    STOCK_TYPE_DAMAGED = "damaged"
    STOCK_TYPE_PROMOTIONAL = "promotional"

    STOCK_TYPE_CHOICES = (
        (STOCK_TYPE_RETAIL, "Retail"),
        (STOCK_TYPE_TESTER, "Tester"),
        (STOCK_TYPE_DAMAGED, "Damaged"),
        (STOCK_TYPE_PROMOTIONAL, "Promotional"),
    )

    variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="inventory_stocks",
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="inventory_stocks"
    )
    stock_type = models.CharField(max_length=20, choices=STOCK_TYPE_CHOICES)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quantity_reserved = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reorder_level = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "warehouse", "stock_type"],
                name="unique_variant_warehouse_stock_type",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0),
                name="inventorystock_quantity_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_reserved__gte=0),
                name="inventorystock_quantity_reserved_gte_0",
            ),
        ]

    @property
    def available(self):
        return self.quantity - self.quantity_reserved

    def __str__(self):
        return f"{self.variant} @ {self.warehouse} [{self.stock_type}]: {self.quantity}"


class PartialBottleLot(BaseModel):
    """One physically opened bottle's remaining usable volume. FIFO
    consumption for decants operates over these lots (ordered by
    opened_at), not over a single pooled quantity — required to support
    true FIFO across bottles opened at different times."""

    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="partial_lots"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="partial_lots"
    )
    remaining_ml = models.DecimalField(max_digits=10, decimal_places=2)
    reserved_ml = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    opened_at = models.DateTimeField(
        help_text=(
            "When this lot's volume became available for FIFO consumption. "
            "For a bottle opened during fulfillment, this is the moment it "
            "was opened. For volume recovered from a customer return, this "
            "is the inspection/restock moment, not the original bottle's "
            "historical opening date. Immutable once set."
        )
    )
    is_depleted = models.BooleanField(default=False)
    source_transaction = models.ForeignKey(
        "StockTransaction",
        on_delete=models.PROTECT,
        related_name="partial_lots_opened",
    )

    class Meta:
        indexes = [
            models.Index(fields=["variant", "warehouse", "is_depleted", "opened_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(remaining_ml__gte=0),
                name="partiallot_remaining_ml_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_ml__gte=0),
                name="partiallot_reserved_ml_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_ml__lte=models.F("remaining_ml")),
                name="partiallot_reserved_ml_lte_remaining_ml",
            ),
        ]

    @property
    def available_ml(self):
        return self.remaining_ml - self.reserved_ml

    def __str__(self):
        return f"{self.variant} lot @ {self.warehouse}: {self.remaining_ml}ml remaining"


class DecantSource(BaseModel):
    """Maps a decant SKU to the full-size variant its physical stock is
    drawn from. The decant variant carries no InventoryStock of its own —
    its sellable availability is always derived from the source variant's
    retail + PartialBottleLot capacity."""

    decant_variant = models.OneToOneField(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="decant_source",
    )
    source_variant = models.ForeignKey(
        "catalog.ProductVariant",
        on_delete=models.PROTECT,
        related_name="decant_children",
    )
    decant_volume_ml = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(decant_volume_ml__gt=0),
                name="decantsource_decant_volume_ml_gt_0",
            ),
        ]

    def __str__(self):
        return f"{self.decant_variant} <- {self.decant_volume_ml}ml from {self.source_variant}"


class StockTransaction(BaseModel):
    """Groups the StockMovement rows produced by a single physical/logical
    inventory event (e.g. opening a bottle: one retail unit out, one
    partial lot created) so the related effects can be queried together."""

    TYPE_PURCHASE_RECEIPT = "purchase_receipt"
    TYPE_DECANT_BOTTLE_OPENED = "decant_bottle_opened"
    TYPE_ADJUSTMENT = "adjustment"
    TYPE_RETURN_DISPOSITION = "return_disposition"
    TYPE_SALE = "sale"

    TYPE_CHOICES = (
        (TYPE_PURCHASE_RECEIPT, "Purchase Receipt"),
        (TYPE_DECANT_BOTTLE_OPENED, "Decant Bottle Opened"),
        (TYPE_ADJUSTMENT, "Adjustment"),
        (TYPE_RETURN_DISPOSITION, "Return Disposition"),
        (TYPE_SALE, "Sale"),
    )

    transaction_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.get_transaction_type_display()} #{self.pk}"


class StockMovement(BaseModel):
    """Append-only ledger row for every physical inventory change. Every
    movement traces to its origin (order, return, supplier, transaction
    group, or partial lot). Zero-quantity rows are never created purely
    for audit purposes — a disposition with no physical stock effect
    (e.g. a damaged decant, whose liquid was already deducted at original
    fulfillment) records its audit trail on ReturnItem instead."""

    MOVEMENT_PURCHASE_IN = "purchase_in"
    MOVEMENT_SALE_OUT = "sale_out"
    MOVEMENT_ADJUSTMENT = "adjustment"
    MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT = "decant_bottle_opened_retail_out"
    MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN = "decant_bottle_opened_partial_in"
    MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL = "decant_fulfilled_from_partial"
    MOVEMENT_RETURN_RESTOCKED_RETAIL = "return_restocked_retail"
    MOVEMENT_RETURN_RESTOCKED_PARTIAL = "return_restocked_partial"
    MOVEMENT_RETURN_DAMAGED = "return_damaged"

    MOVEMENT_TYPE_CHOICES = (
        (MOVEMENT_PURCHASE_IN, "Purchase In"),
        (MOVEMENT_SALE_OUT, "Sale Out"),
        (MOVEMENT_ADJUSTMENT, "Adjustment"),
        (MOVEMENT_DECANT_BOTTLE_OPENED_RETAIL_OUT, "Decant Bottle Opened - Retail Out"),
        (MOVEMENT_DECANT_BOTTLE_OPENED_PARTIAL_IN, "Decant Bottle Opened - Partial In"),
        (MOVEMENT_DECANT_FULFILLED_FROM_PARTIAL, "Decant Fulfilled From Partial"),
        (MOVEMENT_RETURN_RESTOCKED_RETAIL, "Return Restocked - Retail"),
        (MOVEMENT_RETURN_RESTOCKED_PARTIAL, "Return Restocked - Partial"),
        (MOVEMENT_RETURN_DAMAGED, "Return Damaged"),
    )

    STOCK_TYPE_CHOICES = InventoryStock.STOCK_TYPE_CHOICES + (("partial", "Partial"),)

    REASON_PURCHASE = "purchase"
    REASON_SALE = "sale"
    REASON_DECANT_PREPARATION = "decant_preparation"
    REASON_RETURN = "return"
    REASON_DAMAGED_GOODS = "damaged_goods"
    REASON_STOLEN_GOODS = "stolen_goods"
    REASON_STOCK_WRITTEN_OFF = "stock_written_off"
    REASON_STOCKTAKING_RESULTS = "stocktaking_results"
    REASON_MARKETING_USE = "marketing_use"

    REASON_CHOICES = (
        (REASON_PURCHASE, "Purchase"),
        (REASON_SALE, "Sale"),
        (REASON_DECANT_PREPARATION, "Decant Preparation"),
        (REASON_RETURN, "Return"),
        (REASON_DAMAGED_GOODS, "Damaged Goods"),
        (REASON_STOLEN_GOODS, "Stolen Goods"),
        (REASON_STOCK_WRITTEN_OFF, "Stock Written Off"),
        (REASON_STOCKTAKING_RESULTS, "Stocktaking Results"),
        (REASON_MARKETING_USE, "Marketing Use"),
    )

    transaction_group = models.ForeignKey(
        StockTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="stock_movements"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_movements"
    )
    stock_type = models.CharField(
        max_length=20, choices=STOCK_TYPE_CHOICES, null=True, blank=True
    )
    movement_type = models.CharField(max_length=40, choices=MOVEMENT_TYPE_CHOICES)
    quantity_delta = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    notes = models.TextField(blank=True)
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    partial_lot = models.ForeignKey(
        PartialBottleLot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movements",
    )
    source_order_item = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        indexes = [
            models.Index(fields=["variant", "warehouse", "created_at"]),
            models.Index(fields=["movement_type"]),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} {self.quantity_delta} - {self.variant}"


class StockReservation(BaseModel):
    """A hold placed against inventory at checkout, for one OrderItem.

    `variant`/`warehouse` identify what physical capacity is held — for a
    direct-sale reservation this is the ordered variant itself; for a
    decant-fulfillment reservation this is the *source* variant, since
    that's what the physical hold is actually against (the decant SKU
    carries no InventoryStock of its own). `purpose` disambiguates the two
    so release/consume know which behavior applies without re-deriving it.
    """

    PURPOSE_DIRECT_SALE = "direct_sale"
    PURPOSE_DECANT_FULFILLMENT = "decant_fulfillment"

    PURPOSE_CHOICES = (
        (PURPOSE_DIRECT_SALE, "Direct Sale"),
        (PURPOSE_DECANT_FULFILLMENT, "Decant Fulfillment"),
    )

    STATUS_HELD = "held"
    STATUS_CONFIRMED = "confirmed"
    STATUS_RELEASED = "released"
    STATUS_CONSUMED = "consumed"

    STATUS_CHOICES = (
        (STATUS_HELD, "Held"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_RELEASED, "Released"),
        (STATUS_CONSUMED, "Consumed"),
    )

    order_item = models.ForeignKey(
        "orders.OrderItem", on_delete=models.PROTECT, related_name="stock_reservations"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="stock_reservations"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_reservations"
    )
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Unit convention follows purpose: pcs for direct_sale, ml for decant_fulfillment.",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_HELD)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["order_item", "status"]),
        ]

    def __str__(self):
        return f"Reservation({self.order_item_id}, {self.quantity}, {self.status})"


class StockReservationAllocation(BaseModel):
    """Records exactly what physical capacity one StockReservation claimed.

    Release and consume operate strictly off these rows — never by
    re-running FIFO or re-deriving what "should" be released/consumed.
    """

    ALLOCATION_RETAIL_UNIT = "retail_unit"
    ALLOCATION_PARTIAL_LOT = "partial_lot"

    ALLOCATION_TYPE_CHOICES = (
        (ALLOCATION_RETAIL_UNIT, "Retail Unit"),
        (ALLOCATION_PARTIAL_LOT, "Partial Lot"),
    )

    reservation = models.ForeignKey(
        StockReservation, on_delete=models.CASCADE, related_name="allocations"
    )
    allocation_type = models.CharField(max_length=20, choices=ALLOCATION_TYPE_CHOICES)

    # Populated when allocation_type == retail_unit
    inventory_stock = models.ForeignKey(
        InventoryStock,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reservation_allocations",
    )
    units = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # For a decant_fulfillment reservation that had to open a bottle, this is
    # how much of that bottle's volume the decant order actually claims —
    # the remainder becomes a fresh, unreserved PartialBottleLot at consume
    # time. Always null for purpose=direct_sale (the whole bottle is sold).
    claimed_ml = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Populated when allocation_type == partial_lot
    partial_lot = models.ForeignKey(
        PartialBottleLot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reservation_allocations",
    )
    ml_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        allocation_type="retail_unit",
                        inventory_stock__isnull=False,
                        units__isnull=False,
                        partial_lot__isnull=True,
                        ml_amount__isnull=True,
                    )
                    | models.Q(
                        allocation_type="partial_lot",
                        partial_lot__isnull=False,
                        ml_amount__isnull=False,
                        inventory_stock__isnull=True,
                        units__isnull=True,
                        claimed_ml__isnull=True,
                    )
                ),
                name="allocation_fields_match_allocation_type",
            ),
        ]

    def __str__(self):
        if self.allocation_type == self.ALLOCATION_RETAIL_UNIT:
            return f"{self.units} retail unit(s) for reservation {self.reservation_id}"
        return f"{self.ml_amount}ml from lot {self.partial_lot_id} for reservation {self.reservation_id}"
