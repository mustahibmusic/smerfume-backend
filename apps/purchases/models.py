from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q, Value
from django.db.models.functions import Cast, Concat, Greatest, Length, LPad
from django.utils import timezone

from apps.core.models import BaseModel

MONEY = Decimal("0.01")
UNIT_COST = Decimal("0.0001")


def _round(value, places):
    return value.quantize(places, rounding=ROUND_HALF_UP)


class PurchaseOrder(BaseModel):
    """Commercial procurement record: what Smerfume ordered from a vendor.

    A purchase order never changes inventory. Stock only moves when a goods
    receipt is posted (a later phase). Status changes go through
    apps.purchases.services, never through direct edits."""

    STATUS_DRAFT = "draft"
    STATUS_ISSUED = "issued"
    STATUS_PARTIALLY_RECEIVED = "partially_received"
    STATUS_RECEIVED = "received"
    STATUS_CLOSED = "closed"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_ISSUED, "Issued"),
        (STATUS_PARTIALLY_RECEIVED, "Partially received"),
        (STATUS_RECEIVED, "Received"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    # Never selectable by hand. partially_received / received are set only
    # by services.refresh_po_receipt_status() from posted receipts, and
    # issued -> cancelled is further blocked once anything is received.
    # partially_received -> closed is services.close_purchase_order().
    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT: {STATUS_ISSUED, STATUS_CANCELLED},
        STATUS_ISSUED: {STATUS_CANCELLED, STATUS_PARTIALLY_RECEIVED, STATUS_RECEIVED},
        STATUS_PARTIALLY_RECEIVED: {STATUS_RECEIVED, STATUS_CLOSED},
    }
    # Backward moves caused by a goods receipt reversal. Used only by
    # services.refresh_po_receipt_status(); never offered to staff and not
    # part of can_transition_to(). A closed PO is never reopened.
    RECALCULATION_TRANSITIONS = {
        STATUS_RECEIVED: {STATUS_PARTIALLY_RECEIVED, STATUS_ISSUED},
        STATUS_PARTIALLY_RECEIVED: {STATUS_ISSUED},
    }
    RECEIVABLE_STATUSES = (STATUS_ISSUED, STATUS_PARTIALLY_RECEIVED)

    # Header fields staff may edit in each status. Lines are editable only
    # in draft.
    EDITABLE_DRAFT_FIELDS = (
        "supplier", "warehouse", "order_date", "expected_date",
        "vendor_reference", "amounts_include_tax", "notes",
    )
    EDITABLE_OPEN_FIELDS = ("expected_date", "notes")
    EDITABLE_FINAL_FIELDS = ("notes",)

    # Database-generated from the primary key (same approach as
    # Supplier.vendor_code): always present, never changes, cannot collide.
    po_number = models.GeneratedField(
        expression=Concat(
            Value("PO-"),
            LPad(
                Cast("id", models.CharField()),
                Greatest(Length(Cast("id", models.CharField())), 5),
                Value("0"),
            ),
        ),
        output_field=models.CharField(max_length=20),
        db_persist=True,
        unique=True,
    )
    supplier = models.ForeignKey(
        "inventory.Supplier", on_delete=models.PROTECT,
        related_name="purchase_orders", verbose_name="vendor",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    order_date = models.DateField(default=timezone.localdate)
    expected_date = models.DateField(null=True, blank=True)
    vendor_reference = models.CharField(
        max_length=100, blank=True, help_text="The vendor's quote or order number."
    )
    amounts_include_tax = models.BooleanField(
        default=False, help_text="Tick if the vendor's prices include GST."
    )
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)
    # Set by services.close_purchase_order().
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-id"]
        permissions = [
            ("issue_purchaseorder", "Can issue purchase order"),
            ("cancel_purchaseorder", "Can cancel purchase order"),
            ("close_purchaseorder", "Can close purchase order"),
        ]

    def __str__(self):
        return self.po_number if self.pk else "New purchase order"

    # po_number has no value until the row is saved, so Django cannot run
    # its unique check on an unsaved instance. The database enforces it.
    def validate_unique(self, exclude=None):
        super().validate_unique(exclude={*(exclude or ()), "po_number"})

    def validate_constraints(self, exclude=None):
        super().validate_constraints(exclude={*(exclude or ()), "po_number"})

    def clean(self):
        super().clean()
        if self.expected_date and self.order_date and self.expected_date < self.order_date:
            raise ValidationError({"expected_date": "Expected date cannot be before the order date."})

    def editable_fields(self):
        if self.status == self.STATUS_DRAFT:
            return self.EDITABLE_DRAFT_FIELDS
        if self.status in (self.STATUS_ISSUED, self.STATUS_PARTIALLY_RECEIVED):
            return self.EDITABLE_OPEN_FIELDS
        return self.EDITABLE_FINAL_FIELDS

    def can_transition_to(self, new_status):
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    # Totals. total_discounted_amount is what the vendor charges after line
    # discounts (tax-inclusive when amounts_include_tax is set);
    # total_taxable_value always excludes tax.
    def _sum(self, attr):
        return sum((getattr(line, attr) for line in self.lines.all()), Decimal("0.00"))

    @property
    def total_gross_amount(self):
        return self._sum("gross_line_amount")

    @property
    def total_discount_amount(self):
        return self._sum("line_discount_amount")

    @property
    def total_discounted_amount(self):
        return self._sum("discounted_line_amount")

    @property
    def total_taxable_value(self):
        values = [line.taxable_value for line in self.lines.all()]
        if any(value is None for value in values):
            return None
        return sum(values, Decimal("0.00"))


class PurchaseOrderLine(BaseModel):
    """One ordered item on a purchase order. The same variant may appear on
    several lines (e.g. at different prices); goods receipts reference the
    line itself. Received quantities are derived from posted receipts,
    never stored here."""

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="purchase_order_lines"
    )
    quantity_ordered = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(0)],
        help_text="Vendor price per unit before the line discount.",
    )
    line_discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(0)],
        help_text="Discount for the whole line, not per unit.",
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="GST % snapshot. Required before issue when prices include tax.",
    )
    hsn_code = models.CharField(
        max_length=8, blank=True,
        validators=[RegexValidator(r"^\d{4,8}$", "Enter a 4 to 8 digit HSN code.")],
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity_ordered__gt=0), name="po_line_quantity_ordered_gt_0"
            ),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="po_line_unit_price_gte_0"
            ),
            models.CheckConstraint(
                condition=Q(line_discount_amount__gte=0), name="po_line_discount_gte_0"
            ),
            models.CheckConstraint(
                condition=Q(line_discount_amount__lte=F("unit_price") * F("quantity_ordered")),
                name="po_line_discount_lte_gross",
            ),
            models.CheckConstraint(
                condition=Q(tax_rate__isnull=True) | Q(tax_rate__gte=0, tax_rate__lte=100),
                name="po_line_tax_rate_0_to_100",
            ),
        ]

    def __str__(self):
        return f"{self.variant} x {self.quantity_ordered}"

    def clean(self):
        super().clean()
        if (
            self.unit_price is not None and self.quantity_ordered
            and self.line_discount_amount is not None
            and self.line_discount_amount > self.unit_price * self.quantity_ordered
        ):
            raise ValidationError(
                {"line_discount_amount": "Discount cannot exceed the gross line amount."}
            )
        # Only a new/changed choice is checked here; issue re-checks every
        # line, so a historical line never becomes invalid on its own.
        if self.variant_id and self.variant.is_decant:
            raise ValidationError({"variant": "Decants are made in-house and cannot be purchased."})

    @property
    def gross_line_amount(self):
        return _round(self.unit_price * self.quantity_ordered, MONEY)

    @property
    def discounted_line_amount(self):
        return self.gross_line_amount - self.line_discount_amount

    @property
    def taxable_value(self):
        """Ex-tax value of the line. None when prices include tax but no
        tax rate is captured yet (issuing is blocked in that case)."""
        if not self.purchase_order.amounts_include_tax:
            return self.discounted_line_amount
        if self.tax_rate is None:
            return None
        return _round(self.discounted_line_amount / (1 + self.tax_rate / 100), MONEY)

    @property
    def effective_unit_cost_ex_tax(self):
        """Acquisition cost per unit excluding tax, to 4 decimal places.
        Goods receipts copy this as their cost snapshot."""
        if self.purchase_order.amounts_include_tax:
            if self.tax_rate is None:
                return None
            # Divide unrounded amounts so the unit cost is not skewed by
            # rounding the line total first.
            ex_tax = self.discounted_line_amount / (1 + self.tax_rate / 100)
        else:
            ex_tax = self.discounted_line_amount
        return _round(ex_tax / self.quantity_ordered, UNIT_COST)


class GoodsReceipt(BaseModel):
    """Goods receipt note (GRN): what physically arrived at a warehouse.

    Posting a receipt is the only normal procurement path that increases
    inventory (apps.purchases.services.post_goods_receipt). A receipt is not
    a vendor bill. Posted receipts are immutable; corrections are linked
    reversal receipts (services.reverse_goods_receipt), then a new standard
    receipt with the right values."""

    TYPE_STANDARD = "standard"
    TYPE_OPENING = "opening"
    TYPE_REVERSAL = "reversal"

    TYPE_CHOICES = (
        (TYPE_STANDARD, "Standard"),
        (TYPE_OPENING, "Opening stock"),
        (TYPE_REVERSAL, "Reversal"),
    )

    STATUS_DRAFT = "draft"
    STATUS_POSTED = "posted"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_POSTED, "Posted"),
        (STATUS_CANCELLED, "Cancelled"),
    )

    grn_number = models.GeneratedField(
        expression=Concat(
            Value("GRN-"),
            LPad(
                Cast("id", models.CharField()),
                Greatest(Length(Cast("id", models.CharField())), 5),
                Value("0"),
            ),
        ),
        output_field=models.CharField(max_length=20),
        db_persist=True,
        unique=True,
    )
    receipt_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_STANDARD)
    supplier = models.ForeignKey(
        "inventory.Supplier", on_delete=models.PROTECT, null=True, blank=True,
        related_name="goods_receipts", verbose_name="vendor",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="goods_receipts"
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, null=True, blank=True, related_name="receipts"
    )
    # The posted standard receipt a reversal receipt corrects.
    reverses = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    reversal_reason = models.TextField(blank=True)
    received_date = models.DateField(default=timezone.localdate)
    vendor_document_reference = models.CharField(
        max_length=100, blank=True,
        help_text="Delivery challan or invoice number printed on the delivery. Not a bill.",
    )
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    stock_transaction = models.OneToOneField(
        "inventory.StockTransaction", on_delete=models.PROTECT, null=True, blank=True,
        related_name="goods_receipt",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-id"]
        permissions = [
            ("post_goodsreceipt", "Can post goods receipt"),
            ("cancel_goodsreceipt", "Can cancel goods receipt"),
            ("reverse_goodsreceipt", "Can reverse goods receipt"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(receipt_type="standard")
                | Q(purchase_order__isnull=False, supplier__isnull=False),
                name="grn_standard_has_po_and_supplier",
            ),
            models.CheckConstraint(
                condition=~Q(receipt_type="opening") | Q(purchase_order__isnull=True),
                name="grn_opening_has_no_po",
            ),
            models.CheckConstraint(
                condition=~Q(receipt_type="reversal") | Q(reverses__isnull=False),
                name="grn_reversal_has_original",
            ),
            models.CheckConstraint(
                condition=Q(receipt_type="reversal") | Q(reverses__isnull=True),
                name="grn_reverses_only_on_reversal",
            ),
            models.CheckConstraint(
                condition=~Q(receipt_type="reversal", status="posted") | ~Q(reversal_reason=""),
                name="grn_reversal_has_reason",
            ),
            models.CheckConstraint(
                condition=~Q(status="posted")
                | Q(stock_transaction__isnull=False, posted_at__isnull=False),
                name="grn_posted_has_transaction",
            ),
        ]

    def __str__(self):
        return self.grn_number if self.pk else "New goods receipt"

    # grn_number has no value until the row is saved; the database enforces
    # its uniqueness.
    def validate_unique(self, exclude=None):
        super().validate_unique(exclude={*(exclude or ()), "grn_number"})

    def validate_constraints(self, exclude=None):
        super().validate_constraints(exclude={*(exclude or ()), "grn_number"})

    @property
    def is_draft(self):
        return self.status == self.STATUS_DRAFT


class GoodsReceiptLine(BaseModel):
    """Units physically received for one purchase order line. A short
    (missing) unit is never entered here. The variant, cost and tax are
    copied from the PO line; posting recomputes and freezes them."""

    STOCK_TYPE_CHOICES = (
        ("retail", "Retail"),
        ("damaged", "Damaged"),
    )

    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, null=True, blank=True,
        related_name="receipt_lines", verbose_name="PO line",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, related_name="goods_receipt_lines"
    )
    stock_type = models.CharField(max_length=20, choices=STOCK_TYPE_CHOICES, default="retail")
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)], help_text="Units physically received."
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=4, validators=[MinValueValidator(0)],
        help_text="Cost per unit excluding tax. Frozen when the receipt is posted.",
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    # On a reversal receipt: the original posted line this line reverses.
    reverses_line = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversal_lines"
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="grn_line_quantity_gt_0"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="grn_line_unit_cost_gte_0"),
            models.CheckConstraint(
                condition=Q(tax_rate__isnull=True) | Q(tax_rate__gte=0, tax_rate__lte=100),
                name="grn_line_tax_rate_0_to_100",
            ),
            models.CheckConstraint(
                condition=Q(stock_type__in=["retail", "damaged"]),
                name="grn_line_stock_type_retail_or_damaged",
            ),
        ]

    def __str__(self):
        return f"{self.variant} x {self.quantity} [{self.stock_type}]"

    def apply_po_line_snapshot(self):
        """Copy variant, ex-tax unit cost and tax rate from the PO line.
        Prefills drafts; posting calls it again with the PO line locked."""
        if self.po_line_id:
            self.variant = self.po_line.variant
            self.unit_cost = self.po_line.effective_unit_cost_ex_tax
            self.tax_rate = self.po_line.tax_rate

    def clean(self):
        super().clean()
        if self.po_line_id and self.receipt_id:
            if self.po_line.purchase_order_id != self.receipt.purchase_order_id:
                raise ValidationError({"po_line": "This line belongs to a different purchase order."})
        self.apply_po_line_snapshot()

    def save(self, *args, **kwargs):
        # Reversal lines keep the values copied from the original line.
        if self.receipt.is_draft and self.receipt.receipt_type == GoodsReceipt.TYPE_STANDARD:
            self.apply_po_line_snapshot()
        super().save(*args, **kwargs)


class ReceiptDiscrepancy(BaseModel):
    """Something about a delivery that did not match the order: short,
    damaged, wrong item or excess. Descriptive workflow data only: it never
    changes inventory or received quantities.

    Staff record short / wrong_item / excess on a draft goods receipt.
    Damaged discrepancies are created automatically when a receipt with
    damaged stock is posted. After posting, only resolving is allowed."""

    TYPE_SHORT = "short"
    TYPE_DAMAGED = "damaged"
    TYPE_WRONG_ITEM = "wrong_item"
    TYPE_EXCESS = "excess"

    TYPE_CHOICES = (
        (TYPE_SHORT, "Short"),
        (TYPE_DAMAGED, "Damaged"),
        (TYPE_WRONG_ITEM, "Wrong item"),
        (TYPE_EXCESS, "Excess"),
    )
    # Types staff enter by hand; damaged comes from damaged receipt lines.
    STAFF_TYPES = (TYPE_SHORT, TYPE_WRONG_ITEM, TYPE_EXCESS)

    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"

    STATUS_CHOICES = (
        (STATUS_OPEN, "Open"),
        (STATUS_RESOLVED, "Resolved"),
    )

    # Operational outcomes only. vendor_credit_expected creates no
    # accounting entry (Zoho owns that); return_planned moves no stock
    # (supplier returns are a later phase).
    RESOLUTION_CHOICES = (
        ("accepted", "Accepted"),
        ("replacement_expected", "Replacement expected"),
        ("vendor_credit_expected", "Vendor credit expected"),
        ("written_off", "Written off"),
        ("return_planned", "Return planned"),
    )

    receipt = models.ForeignKey(
        GoodsReceipt, on_delete=models.CASCADE, related_name="discrepancies"
    )
    po_line = models.ForeignKey(
        PurchaseOrderLine, on_delete=models.PROTECT, null=True, blank=True,
        related_name="discrepancies", verbose_name="PO line",
    )
    receipt_line = models.ForeignKey(
        GoodsReceiptLine, on_delete=models.PROTECT, null=True, blank=True,
        related_name="discrepancies",
    )
    variant = models.ForeignKey(
        "catalog.ProductVariant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="receipt_discrepancies",
        help_text="For a wrong item: what actually arrived, if it is in the catalogue.",
    )
    observed_item_description = models.CharField(
        max_length=255, blank=True,
        help_text="For a wrong item not in the catalogue: describe what arrived.",
    )
    discrepancy_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    resolution = models.CharField(max_length=30, choices=RESOLUTION_CHOICES, blank=True)
    resolution_notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "receipt discrepancy"
        verbose_name_plural = "receipt discrepancies"
        permissions = [("resolve_receiptdiscrepancy", "Can resolve receipt discrepancy")]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gt=0), name="discrepancy_quantity_gt_0"),
            models.CheckConstraint(
                condition=~Q(discrepancy_type="damaged") | Q(receipt_line__isnull=False),
                name="discrepancy_damaged_has_receipt_line",
            ),
            models.CheckConstraint(
                condition=~Q(discrepancy_type__in=["short", "excess"]) | Q(po_line__isnull=False),
                name="discrepancy_short_excess_has_po_line",
            ),
            models.CheckConstraint(
                condition=~Q(discrepancy_type="wrong_item")
                | Q(variant__isnull=False) | ~Q(observed_item_description=""),
                name="discrepancy_wrong_item_described",
            ),
            models.CheckConstraint(
                condition=Q(status="open", resolution="", resolved_at__isnull=True)
                | Q(status="resolved", resolved_at__isnull=False) & ~Q(resolution=""),
                name="discrepancy_resolution_matches_status",
            ),
            # One damaged discrepancy per damaged receipt line.
            models.UniqueConstraint(
                fields=["receipt_line", "discrepancy_type"],
                condition=Q(receipt_line__isnull=False),
                name="unique_discrepancy_per_receipt_line_type",
            ),
        ]

    def __str__(self):
        return f"{self.get_discrepancy_type_display()} x {self.quantity}"

    @property
    def item_label(self):
        if self.variant_id:
            return self.variant.sku
        if self.observed_item_description:
            return self.observed_item_description
        if self.po_line_id:
            return self.po_line.variant.sku
        return "-"

    def clean(self):
        super().clean()
        errors = {}
        receipt = self.receipt if self.receipt_id else None
        if self.po_line_id and receipt and self.po_line.purchase_order_id != receipt.purchase_order_id:
            errors["po_line"] = "This line belongs to a different purchase order."
        if self.discrepancy_type in (self.TYPE_SHORT, self.TYPE_EXCESS) and not self.po_line_id:
            errors["po_line"] = "Choose the purchase order line this relates to."
        if self.discrepancy_type == self.TYPE_WRONG_ITEM:
            if not self.variant_id and not self.observed_item_description.strip():
                errors["observed_item_description"] = (
                    "Choose the item that arrived or describe it."
                )
            elif self.po_line_id and self.variant_id == self.po_line.variant_id:
                errors["variant"] = "A wrong item must differ from the ordered item."
        if self.discrepancy_type == self.TYPE_DAMAGED:
            line = self.receipt_line if self.receipt_line_id else None
            if line is None or line.stock_type != "damaged" or (
                receipt and line.receipt_id != receipt.pk
            ):
                errors["receipt_line"] = "A damaged discrepancy needs a damaged line of this receipt."
        if errors:
            raise ValidationError(errors)
