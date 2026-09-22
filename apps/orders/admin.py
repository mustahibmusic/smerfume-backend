from django.contrib import admin
from django.contrib import messages
from unfold.admin import ModelAdmin

from . import services as order_services
from .models import Order, OrderItem, Return, ReturnItem, ShippingAddress


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = (
        "variant", "quantity", "unit_price", "line_total",
        "shipping_surcharge", "discount_allocated", "net_line_amount",
        "tax_amount", "final_paid_line_amount",
    )
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
        "order_number", "user", "status", "payment_method", "total",
        "is_guest_order_display", "guest_email", "created_at",
    )
    list_filter = ("status", "payment_method", "created_at")
    search_fields = ("order_number", "user__mobile_number", "user__email", "guest_email")
    readonly_fields = (
        "public_id", "order_number", "user", "subtotal",
        "discount_amount", "total", "guest_email", "is_guest_order_display", "created_at",
        "payment_method", "cod_verified_at", "cod_verified_by",
        "base_shipping_charge", "free_shipping_applied", "shipping_charge",
        "cod_handling_charge", "convenience_fee",
        "delivered_at", "is_replacement", "original_order", "tracking_number",
        # status has business-consequence transitions (confirmed->processing
        # must consume inventory; pending->confirmed must harden COD
        # reservations) that only the service functions below perform
        # correctly — never editable directly, use the actions instead.
        "status",
    )
    inlines = [OrderItemInline, ShippingAddressInline]
    actions = ["verify_cod_orders", "pack_orders", "mark_shipped", "mark_delivered", "cancel_orders"]

    def has_delete_permission(self, request, obj=None):
        # An order anchors reservations/movements/returns/refunds — never
        # deletable, even by a superuser. cancel_order() is the correct way
        # to unwind one that hasn't shipped.
        return False

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
        ("Shipping", {
            "fields": (
                "base_shipping_charge", "free_shipping_applied", "shipping_charge",
                "cod_handling_charge", "convenience_fee",
            ),
        }),
        ("Payment", {
            "fields": ("payment_method", "cod_verified_at", "cod_verified_by"),
        }),
        ("Status & Notes", {
            "fields": ("status", "customer_notes"),
        }),
        ("Replacement", {
            "fields": ("is_replacement", "original_order"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "delivered_at", "tracking_number"),
        }),
    )

    @admin.display(description="Guest order", boolean=True)
    def is_guest_order_display(self, obj):
        return obj.is_guest_order

    @admin.action(description="Verify selected COD orders (confirms reservation, advances to Confirmed)")
    def verify_cod_orders(self, request, queryset):
        verified_count = 0
        for order in queryset:
            try:
                order_services.verify_cod_order(order, verified_by=request.user)
                verified_count += 1
            except order_services.CODVerificationError as exc:
                self.message_user(
                    request, f"{order.order_number}: {exc}", level=messages.WARNING
                )
        if verified_count:
            self.message_user(
                request, f"Verified {verified_count} order(s).", level=messages.SUCCESS
            )

    @admin.action(description="Pack selected orders (consumes reserved inventory, advances to Processing)")
    def pack_orders(self, request, queryset):
        packed_count = 0
        for order in queryset:
            try:
                order_services.pack_order(order, performed_by=request.user)
                packed_count += 1
            except order_services.OrderPackingError as exc:
                self.message_user(
                    request, f"{order.order_number}: {exc}", level=messages.WARNING
                )
        if packed_count:
            self.message_user(
                request, f"Packed {packed_count} order(s).", level=messages.SUCCESS
            )

    @admin.action(description="Mark selected orders as Shipped")
    def mark_shipped(self, request, queryset):
        shipped_count = 0
        for order in queryset:
            try:
                order_services.mark_order_shipped(order)
                shipped_count += 1
            except order_services.OrderTransitionError as exc:
                self.message_user(
                    request, f"{order.order_number}: {exc}", level=messages.WARNING
                )
        if shipped_count:
            self.message_user(
                request, f"Marked {shipped_count} order(s) as shipped.", level=messages.SUCCESS
            )

    @admin.action(description="Mark selected orders as Delivered")
    def mark_delivered(self, request, queryset):
        delivered_count = 0
        for order in queryset:
            try:
                order_services.mark_order_delivered(order)
                delivered_count += 1
            except order_services.OrderTransitionError as exc:
                self.message_user(
                    request, f"{order.order_number}: {exc}", level=messages.WARNING
                )
        if delivered_count:
            self.message_user(
                request, f"Marked {delivered_count} order(s) as delivered.", level=messages.SUCCESS
            )

    @admin.action(description="Cancel selected orders (releases reserved inventory)")
    def cancel_orders(self, request, queryset):
        cancelled_count = 0
        for order in queryset:
            try:
                order_services.cancel_order(order)
                cancelled_count += 1
            except order_services.OrderTransitionError as exc:
                self.message_user(
                    request, f"{order.order_number}: {exc}", level=messages.WARNING
                )
        if cancelled_count:
            self.message_user(
                request, f"Cancelled {cancelled_count} order(s).", level=messages.SUCCESS
            )




class ReturnItemInline(admin.TabularInline):
    model = ReturnItem
    extra = 0
    fields = ("order_item", "reason", "reason_notes", "requested_quantity", "received_quantity")
    readonly_fields = ("order_item", "reason", "reason_notes", "requested_quantity")
    can_delete = False


@admin.register(Return)
class ReturnAdmin(ModelAdmin):
    list_display = ("id", "order", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("order__order_number",)
    readonly_fields = ("public_id", "order", "status", "created_at")
    fields = ("public_id", "order", "status", "created_at")
    inlines = [ReturnItemInline]
    actions = [
        "approve_returns", "reject_returns", "cancel_returns",
        "mark_in_transit", "mark_received", "start_inspection",
    ]

    def has_add_permission(self, request):
        # Return creation must go through create_return()'s eligibility
        # checks (delivery status, 1-day window, structured reason,
        # cumulative-quantity cap) — a raw admin "Add" form has no way to
        # enforce those, so it's disabled here rather than left as a bypass.
        # Customers create returns via POST /api/orders/<order_number>/returns/
        # (apps/orders/views.py::CreateReturnView) — this admin intentionally
        # offers no equivalent staff-initiated creation path.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _run_action(self, request, queryset, service_fn, error_cls, verb):
        count = 0
        for return_request in queryset:
            try:
                service_fn(return_request)
                count += 1
            except error_cls as exc:
                self.message_user(
                    request, f"Return #{return_request.pk}: {exc}", level=messages.WARNING
                )
        if count:
            self.message_user(request, f"{verb} {count} return(s).", level=messages.SUCCESS)

    @admin.action(description="Approve selected returns")
    def approve_returns(self, request, queryset):
        self._run_action(
            request, queryset, order_services.approve_return,
            order_services.ReturnTransitionError, "Approved",
        )

    @admin.action(description="Reject selected returns")
    def reject_returns(self, request, queryset):
        self._run_action(
            request, queryset, order_services.reject_return,
            order_services.ReturnTransitionError, "Rejected",
        )

    @admin.action(description="Cancel selected returns")
    def cancel_returns(self, request, queryset):
        self._run_action(
            request, queryset, order_services.cancel_return,
            order_services.ReturnTransitionError, "Cancelled",
        )

    @admin.action(description="Mark selected returns as In Transit")
    def mark_in_transit(self, request, queryset):
        self._run_action(
            request, queryset, order_services.mark_return_in_transit,
            order_services.ReturnTransitionError, "Marked in-transit",
        )

    @admin.action(description="Mark selected returns as Received (requires received_quantity set on every line)")
    def mark_received(self, request, queryset):
        self._run_action(
            request, queryset, order_services.mark_return_received,
            order_services.ReturnTransitionError, "Marked received",
        )

    @admin.action(description="Start inspection on selected returns")
    def start_inspection(self, request, queryset):
        self._run_action(
            request, queryset, order_services.start_inspection,
            order_services.ReturnTransitionError, "Started inspection on",
        )
