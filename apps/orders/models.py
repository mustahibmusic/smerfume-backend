from django.conf import settings
from django.db import models

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

    class Meta:
        db_table = "orders_order"
        ordering = ["-created_at"]

    def __str__(self):
        return self.order_number


class OrderItem(BaseModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "orders_orderitem"

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
