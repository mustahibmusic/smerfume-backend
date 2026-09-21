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
