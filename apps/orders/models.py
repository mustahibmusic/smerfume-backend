from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.core.models import BaseModel


class Order(BaseModel):
    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_PROCESSING = "processing"
    STATUS_SHIPPED = "shipped"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"
    STATUS_REFUNDED = "refunded"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_REFUNDED, "Refunded"),
    ]

    PAYMENT_METHOD_COD = "cod"
    PAYMENT_METHOD_PREPAID = "prepaid"

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_METHOD_COD, "Cash on Delivery"),
        (PAYMENT_METHOD_PREPAID, "Prepaid"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    order_number = models.CharField(max_length=24, unique=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    customer_notes = models.TextField(blank=True)
    guest_email = models.EmailField(null=True, blank=True)

    # ── Payment / COD verification ───────────────────────────────────────
    payment_method = models.CharField(
        max_length=10, choices=PAYMENT_METHOD_CHOICES, default=PAYMENT_METHOD_COD
    )
    cod_verified_at = models.DateTimeField(null=True, blank=True)
    cod_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # ── Fulfillment ───────────────────────────────────────────────────────
    # Auto-populated in save() the moment status transitions to `delivered` —
    # controls the return-eligibility window, so it is not meant to be
    # hand-edited; any correction must be an explicit, auditable action.
    delivered_at = models.DateTimeField(null=True, blank=True)
    # Set only via mark_order_shipped() — carrier integration (Shiprocket) is
    # not implemented yet, so this is a plain manually-entered value for now.
    tracking_number = models.CharField(max_length=100, null=True, blank=True)

    # ── Shipping financial snapshot ─────────────────────────────────────
    # Checkout-time snapshot, frozen thereafter — see docs/INVENTORY_MANAGEMENT.md.
    base_shipping_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    free_shipping_applied = models.BooleanField(default=False)
    shipping_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cod_handling_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    convenience_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # ── Replacement orders ───────────────────────────────────────────────
    is_replacement = models.BooleanField(default=False)
    original_order = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replacement_orders",
    )

    class Meta:
        db_table = "orders_order"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_shipping_charge__gte=0), name="order_base_shipping_charge_gte_0"
            ),
            models.CheckConstraint(
                condition=models.Q(shipping_charge__gte=0), name="order_shipping_charge_gte_0"
            ),
        ]

    @property
    def is_guest_order(self) -> bool:
        """True when this order was placed via guest checkout."""
        return self.guest_email is not None

    def save(self, *args, **kwargs):
        if self.pk and not self.delivered_at and self.status == self.STATUS_DELIVERED:
            previous_status = (
                Order.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            )
            if previous_status != self.STATUS_DELIVERED:
                self.delivered_at = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.order_number


class OrderItem(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    # ── Shipping snapshot ────────────────────────────────────────────────
    # Snapshotted from ProductVariant.shipping_surcharge at checkout — the
    # TOTAL surcharge for this line (already × quantity), never recalculated
    # from the variant's current configuration.
    shipping_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # ── Discount / tax financial snapshot ───────────────────────────────
    # All checkout-time snapshots, frozen thereafter. gross_line_amount is
    # unit_price × quantity (== line_total); discount_allocated is this
    # line's proportional share of Order.discount_amount; net_line_amount
    # is gross minus that share; tax_amount is a placeholder (Django does
    # not yet compute GST anywhere — see CURRENT_STATE.md); final_paid_line_amount
    # is the true refund basis for a returned unit of this line.
    gross_line_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_allocated = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_line_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    final_paid_line_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        db_table = "orders_orderitem"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(shipping_surcharge__gte=0), name="orderitem_shipping_surcharge_gte_0"
            ),
        ]

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.quantity}× {self.variant} @ ₹{self.unit_price}"


class ShippingAddress(BaseModel):
    INDIA_STATES = [
        ("AN", "Andaman and Nicobar Islands"), ("AP", "Andhra Pradesh"),
        ("AR", "Arunachal Pradesh"), ("AS", "Assam"), ("BR", "Bihar"),
        ("CH", "Chandigarh"), ("CT", "Chhattisgarh"), ("DN", "Dadra and Nagar Haveli"),
        ("DD", "Daman and Diu"), ("DL", "Delhi"), ("GA", "Goa"), ("GJ", "Gujarat"),
        ("HR", "Haryana"), ("HP", "Himachal Pradesh"), ("JK", "Jammu and Kashmir"),
        ("JH", "Jharkhand"), ("KA", "Karnataka"), ("KL", "Kerala"), ("LA", "Ladakh"),
        ("LD", "Lakshadweep"), ("MP", "Madhya Pradesh"), ("MH", "Maharashtra"),
        ("MN", "Manipur"), ("ML", "Meghalaya"), ("MZ", "Mizoram"), ("NL", "Nagaland"),
        ("OR", "Odisha"), ("PY", "Puducherry"), ("PB", "Punjab"), ("RJ", "Rajasthan"),
        ("SK", "Sikkim"), ("TN", "Tamil Nadu"), ("TG", "Telangana"), ("TR", "Tripura"),
        ("UP", "Uttar Pradesh"), ("UT", "Uttarakhand"), ("WB", "West Bengal"),
    ]

    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name="shipping_address",
    )
    full_name = models.CharField(max_length=200)
    mobile = models.CharField(max_length=15)
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, choices=INDIA_STATES)
    pincode = models.CharField(max_length=6)
    country = models.CharField(max_length=100, default="India")

    class Meta:
        db_table = "orders_shippingaddress"

    def __str__(self):
        return f"{self.full_name}, {self.city}, {self.state} - {self.pincode}"


class Return(BaseModel):
    """The customer's overall return request/process. Disposition and
    resolution live on ReturnItem (per-line), not here — this model tracks
    only the process lifecycle. The actual refund transaction is a separate
    Refund record (apps.orders.services.create_refund_for_return) — this
    model only carries the base-shipping-refund amount, since that's an
    order-wide computation, not a per-line one."""

    STATUS_REQUESTED = "requested"
    STATUS_APPROVED = "approved"
    STATUS_IN_TRANSIT = "in_transit"
    STATUS_RECEIVED = "received"
    STATUS_INSPECTION_PENDING = "inspection_pending"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_IN_TRANSIT, "In Transit"),
        (STATUS_RECEIVED, "Received"),
        (STATUS_INSPECTION_PENDING, "Inspection Pending"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="returns")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED, db_index=True)

    # Set only via create_refund_for_return() — never recalculated once set.
    base_shipping_refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # None = use the computed rule; True/False = staff-forced override,
    # applied instead of the computed rule at refund-creation time.
    base_shipping_refund_override = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "orders_return"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(base_shipping_refund_amount__gte=0),
                name="return_base_shipping_refund_amount_gte_0",
            ),
        ]

    def __str__(self):
        return f"Return({self.order.order_number}, {self.status})"


class ReturnItem(BaseModel):
    """One returned line within a Return. Eligibility is enforced entirely
    through `reason` — only documented, Smerfume-fault/logistics reasons are
    valid; customer-preference returns ("didn't like it", "changed my
    mind") are not supported and have no corresponding choice here."""

    REASON_WRONG_ITEM = "wrong_item"
    REASON_WRONG_VARIANT = "wrong_variant"
    REASON_TRANSIT_DAMAGE = "transit_damage"
    REASON_MANUFACTURING_DEFECT = "manufacturing_defect"
    REASON_MISSING_ITEMS = "missing_items"

    REASON_CHOICES = [
        (REASON_WRONG_ITEM, "Wrong Item Received"),
        (REASON_WRONG_VARIANT, "Wrong Variant/Size Received"),
        (REASON_TRANSIT_DAMAGE, "Transit Damage"),
        (REASON_MANUFACTURING_DEFECT, "Manufacturing Defect"),
        (REASON_MISSING_ITEMS, "Missing Items"),
    ]

    DISPOSITION_RESTOCKED_RETAIL = "restocked_retail"
    DISPOSITION_RESTOCKED_PARTIAL = "restocked_partial"
    DISPOSITION_DAMAGED = "damaged"
    DISPOSITION_REJECTED = "rejected"

    DISPOSITION_CHOICES = [
        (DISPOSITION_RESTOCKED_RETAIL, "Restocked - Retail"),
        (DISPOSITION_RESTOCKED_PARTIAL, "Restocked - Partial"),
        (DISPOSITION_DAMAGED, "Damaged"),
        (DISPOSITION_REJECTED, "Rejected"),
    ]

    RESOLUTION_REFUND = "refund"
    RESOLUTION_REPLACEMENT = "replacement"
    RESOLUTION_NOT_APPLICABLE = "not_applicable"

    RESOLUTION_CHOICES = [
        (RESOLUTION_REFUND, "Refund"),
        (RESOLUTION_REPLACEMENT, "Replacement"),
        (RESOLUTION_NOT_APPLICABLE, "Not Applicable"),
    ]

    return_request = models.ForeignKey(Return, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name="return_items")
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    reason_notes = models.TextField(blank=True)
    requested_quantity = models.PositiveSmallIntegerField()
    # Set automatically (== requested_quantity) when the parent Return is
    # approved — see approve_return(). Per-line approval below the requested
    # quantity is not implemented; this field exists so finalize_return_item
    # has a stable cap to validate received_quantity against.
    approved_quantity = models.PositiveSmallIntegerField(null=True, blank=True)
    received_quantity = models.PositiveSmallIntegerField(null=True, blank=True)

    # ── Inspection / disposition — set only via finalize_return_item() ────
    disposition = models.CharField(max_length=20, choices=DISPOSITION_CHOICES, null=True, blank=True)
    resolution = models.CharField(max_length=20, choices=RESOLUTION_CHOICES, null=True, blank=True)
    remaining_quantity_ml = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    inspection_notes = models.TextField(blank=True)
    inspected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    inspected_at = models.DateTimeField(null=True, blank=True)

    # ── Refund calculation — set only via create_refund_for_return(), once,
    # and never recalculated afterward. Both remain 0/null for resolution
    # in (replacement, not_applicable) — only resolution=refund lines ever
    # get a value here.
    refund_line_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    surcharge_refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        db_table = "orders_returnitem"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(requested_quantity__gt=0), name="returnitem_requested_quantity_gt_0"
            ),
            models.CheckConstraint(
                condition=models.Q(remaining_quantity_ml__isnull=True) | models.Q(remaining_quantity_ml__gt=0),
                name="returnitem_remaining_ml_positive_or_null",
            ),
            models.CheckConstraint(
                condition=models.Q(refund_line_amount__isnull=True) | models.Q(refund_line_amount__gte=0),
                name="returnitem_refund_line_amount_gte_0_or_null",
            ),
            models.CheckConstraint(
                condition=models.Q(surcharge_refund_amount__gte=0),
                name="returnitem_surcharge_refund_amount_gte_0",
            ),
        ]

    def __str__(self):
        return f"ReturnItem({self.order_item}, {self.reason})"


class Refund(BaseModel):
    """The financial record of what's owed back to the customer for a
    Return, and its processing status. One Refund per Return (created once
    the Return is fully inspected — see create_refund_for_return()),
    aggregating every resolution=refund ReturnItem's product + surcharge
    amounts plus the Return's base_shipping_refund_amount.

    This model records the calculation and (manual, for now) payment
    workflow — it never calls an external payment gateway. refund_amount
    is written once at creation and never modified afterward; a correction
    after completion must go through RefundAdjustment, not a direct edit
    here."""

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    METHOD_GATEWAY = "gateway"
    METHOD_BANK_TRANSFER = "bank_transfer"
    METHOD_UPI = "upi"

    METHOD_CHOICES = [
        (METHOD_GATEWAY, "Payment Gateway"),
        (METHOD_BANK_TRANSFER, "Bank Transfer"),
        (METHOD_UPI, "UPI"),
    ]

    return_request = models.ForeignKey(Return, on_delete=models.PROTECT, related_name="refunds")
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2)
    refund_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    # Left unset at creation — there is no live gateway to infer a channel
    # from, so a staff member must explicitly choose it (set_refund_method())
    # before the refund can move to processing. Never guessed from
    # Order.payment_method.
    refund_method = models.CharField(max_length=20, choices=METHOD_CHOICES, null=True, blank=True)
    # UTR / bank reference (manual COD) or gateway refund id (prepaid) —
    # either way, entered manually; no live provider integration exists.
    refund_reference = models.CharField(max_length=100, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "orders_refund"
        constraints = [
            models.CheckConstraint(condition=models.Q(refund_amount__gte=0), name="refund_amount_gte_0"),
        ]

    def __str__(self):
        return f"Refund({self.return_request.order.order_number}, {self.refund_amount}, {self.refund_status})"


class RefundAdjustment(BaseModel):
    """An explicit, audited correction to an already-completed Refund.
    Refund.refund_amount is never edited directly once refund_status is
    completed — a correction is recorded here instead, and the effective
    refunded amount for reporting is refund_amount + sum(adjustments)."""

    refund = models.ForeignKey(Refund, on_delete=models.PROTECT, related_name="adjustments")
    adjustment_amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "orders_refundadjustment"

    def __str__(self):
        return f"RefundAdjustment({self.refund_id}, {self.adjustment_amount})"
