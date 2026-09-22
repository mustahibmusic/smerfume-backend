from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import Cart, CartItem


class CartItemInline(TabularInline):
    model = CartItem
    extra = 0
    readonly_fields = ("variant", "quantity", "line_total")
    can_delete = False

    @admin.display(description="Line total")
    def line_total(self, obj):
        return obj.line_total


@admin.register(Cart)
class CartAdmin(ModelAdmin):
    """Read-only — carts are entirely customer/checkout-driven
    (apps.cart.views, apps.cart.services.merge_guest_cart). Registered for
    support/debugging visibility only, since this was previously invisible
    in admin altogether."""

    list_display = ("id", "user", "session_key", "item_count", "created_at", "updated_at")
    search_fields = ("user__email", "user__mobile_number", "session_key")
    list_filter = ("created_at",)
    readonly_fields = ("public_id", "user", "session_key", "created_at", "updated_at")
    inlines = [CartItemInline]

    @admin.display(description="Items")
    def item_count(self, obj):
        return obj.items.count()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
