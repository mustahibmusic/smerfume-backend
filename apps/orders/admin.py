from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Order, OrderItem, ShippingAddress


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("variant", "quantity", "unit_price", "line_total")
    can_delete = False


class ShippingAddressInline(admin.StackedInline):
    model = ShippingAddress
    extra = 0
    readonly_fields = (
        "full_name", "mobile", "address_line1", "address_line2",
        "city", "state", "pincode", "country",
    )
    can_delete = False


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = (
        "order_number", "user", "status", "total",
        "is_guest_order_display", "guest_email", "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("order_number", "user__mobile_number", "user__email", "guest_email")
    readonly_fields = (
        "public_id", "order_number", "user", "subtotal",
        "discount_amount", "total", "guest_email", "is_guest_order_display", "created_at",
    )
    inlines = [OrderItemInline, ShippingAddressInline]

    fieldsets = (
        (None, {
            "fields": (
                "public_id", "order_number", "user",
                "is_guest_order_display", "guest_email",
            ),
        }),
        ("Amounts", {
            "fields": ("subtotal", "discount_amount", "total"),
        }),
        ("Status & Notes", {
            "fields": ("status", "customer_notes"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
        }),
    )

    @admin.display(description="Guest order", boolean=True)
    def is_guest_order_display(self, obj):
        return obj.is_guest_order
