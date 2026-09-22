from django import forms
from django.contrib import admin
from django.contrib import messages
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from rest_framework import serializers as drf_serializers
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

from apps.catalog.models import ProductVariant
from apps.inventory.models import Warehouse
from apps.inventory.services.reservation import InsufficientStockError

from . import services as order_services
from .models import Order, OrderItem, Refund, RefundAdjustment, Return, ReturnItem, ShippingAddress
from .serializers import validate_mobile_number


# ── In-store sale form (DEC-008) ─────────────────────────────────────────────

class InStoreSaleForm(forms.Form):
    customer_mobile = forms.CharField(
        max_length=15, widget=UnfoldAdminTextInputWidget,
        help_text="Required. Finds the customer, or creates one if new.",
    )
    customer_name = forms.CharField(
        max_length=150, required=False, widget=UnfoldAdminTextInputWidget,
        help_text="Only used when a new customer is created.",
    )
    payment_method = forms.ChoiceField(
        choices=[c for c in Order.PAYMENT_METHOD_CHOICES if c[0] in Order.IN_STORE_PAYMENT_METHODS],
        widget=UnfoldAdminSelectWidget,
    )
    payment_reference = forms.CharField(
        max_length=100, required=False, widget=UnfoldAdminTextInputWidget,
        help_text="UPI/netbanking UTR or card slip number.",
    )
    discount_amount = forms.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, initial=0, required=False,
        widget=UnfoldAdminDecimalFieldWidget,
        help_text="Order-level discount, spread across lines. Cannot exceed the subtotal.",
    )
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.order_by("name"), widget=UnfoldAdminSelectWidget,
        help_text="Stock location the items leave from.",
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].initial = Warehouse.objects.filter(is_default=True).first()

    def clean_customer_mobile(self):
        try:
            return validate_mobile_number(self.cleaned_data["customer_mobile"])
        except drf_serializers.ValidationError as exc:
            raise forms.ValidationError([str(e) for e in exc.detail]) from exc


class InStoreSaleLineForm(forms.Form):
    sku = forms.CharField(max_length=64, widget=UnfoldAdminTextInputWidget)
    # No initial value: blank extra rows must stay "unchanged" so the
    # formset skips them instead of reporting required-field errors.
    quantity = forms.IntegerField(
        min_value=1, max_value=1000, widget=UnfoldAdminIntegerFieldWidget,
    )

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        variant = ProductVariant.objects.filter(sku=sku).first()
        if variant is None:
            raise forms.ValidationError(f"Unknown SKU '{sku}'.")
        self.cleaned_data["variant"] = variant
        return sku


class _BaseLineFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(form.cleaned_data.get("variant") for form in self.forms):
            raise forms.ValidationError("Add at least one item.")


InStoreSaleLineFormSet = forms.formset_factory(
    InStoreSaleLineForm, formset=_BaseLineFormSet, extra=5, max_num=50
)


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
        "order_number", "user", "channel", "status", "payment_method", "total",
        "is_guest_order_display", "guest_email", "created_at",
    )
    list_filter = ("channel", "status", "payment_method", "created_at")
    search_fields = ("order_number", "user__mobile_number", "user__email", "guest_email")
    readonly_fields = (
        "public_id", "order_number", "user", "subtotal",
        "discount_amount", "total", "guest_email", "is_guest_order_display", "created_at",
        "payment_method", "payment_reference", "cod_verified_at", "cod_verified_by",
        "channel", "created_by",
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
    actions_list = ["new_in_store_sale"]

    def has_add_permission(self, request):
        # The generic add form would create an Order with no items,
        # reservations or totals. Online orders come from checkout; counter
        # sales use the "New in-store sale" page (new_in_store_sale).
        return False

    def has_delete_permission(self, request, obj=None):
        # An order anchors reservations/movements/returns/refunds — never
        # deletable, even by a superuser. cancel_order() is the correct way
        # to unwind one that hasn't shipped.
        return False

    fieldsets = (
        (None, {
            "fields": (
                "public_id", "order_number", "user", "channel", "created_by",
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
            "fields": ("payment_method", "payment_reference", "cod_verified_at", "cod_verified_by"),
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

    @action(
        description="New in-store sale",
        url_path="in-store-sale",
        permissions=["orders.add_order"],
        icon="point_of_sale",
    )
    def new_in_store_sale(self, request):
        """Record a walk-in counter sale through
        order_services.create_in_store_order() — the same service the staff
        API uses. Any failure re-renders the form and creates nothing."""
        if request.method == "POST":
            form = InStoreSaleForm(request.POST)
            formset = InStoreSaleLineFormSet(request.POST, prefix="lines")
            if form.is_valid() and formset.is_valid():
                data = form.cleaned_data
                lines = [
                    (line["variant"], line["quantity"])
                    for line in formset.cleaned_data
                    if line.get("variant")
                ]
                try:
                    order = order_services.create_in_store_order(
                        customer_mobile=data["customer_mobile"],
                        customer_name=data["customer_name"],
                        lines=lines,
                        payment_method=data["payment_method"],
                        payment_reference=data["payment_reference"],
                        discount_amount=data["discount_amount"] or 0,
                        warehouse=data["warehouse"],
                        notes=data["notes"],
                        staff_user=request.user,
                    )
                except (order_services.InStoreSaleError, InsufficientStockError) as exc:
                    form.add_error(None, str(exc))
                else:
                    self.message_user(
                        request,
                        f"In-store sale {order.order_number} recorded (total {order.total}).",
                        level=messages.SUCCESS,
                    )
                    return redirect(reverse("admin:orders_order_change", args=[order.pk]))
        else:
            form = InStoreSaleForm()
            formset = InStoreSaleLineFormSet(prefix="lines")

        context = {
            **self.admin_site.each_context(request),
            "title": "New in-store sale",
            "opts": self.model._meta,
            "form": form,
            "formset": formset,
        }
        return TemplateResponse(request, "admin/orders/in_store_sale.html", context)

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
    # base_shipping_refund_override is intentionally editable — it's the
    # staff override point ("unless an explicit staff override is used")
    # for the computed base-shipping-refund rule; create_refund reads it
    # at calculation time. base_shipping_refund_amount is the resulting
    # computed/stored value and stays read-only.
    readonly_fields = ("public_id", "order", "status", "created_at", "base_shipping_refund_amount")
    fields = (
        "public_id", "order", "status", "created_at",
        "base_shipping_refund_override", "base_shipping_refund_amount",
    )
    inlines = [ReturnItemInline]
    actions = [
        "approve_returns", "reject_returns", "cancel_returns",
        "mark_in_transit", "mark_received", "start_inspection",
        "create_refund",
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

    @admin.action(description="Create refund for selected returns (completed returns only)")
    def create_refund(self, request, queryset):
        count = 0
        for return_request in queryset:
            try:
                # refund_method is left unset — there is no live payment
                # provider to infer it from. Staff must explicitly choose
                # it on the Refund's own change form before it can be
                # marked processing.
                order_services.create_refund_for_return(
                    return_request, approved_by=request.user,
                )
                count += 1
            except order_services.RefundError as exc:
                self.message_user(
                    request, f"Return #{return_request.pk}: {exc}", level=messages.WARNING
                )
        if count:
            self.message_user(
                request,
                f"Created {count} refund(s). Set each refund's method before marking it processing.",
                level=messages.SUCCESS,
            )


class ReturnItemInspectionForm(forms.ModelForm):
    """A normal-looking ModelForm bound to real ReturnItem fields — but
    ReturnItemAdmin.save_model() below never calls form.save()/obj.save().
    The only way these fields are ever persisted is through
    finalize_return_item(), which performs the inventory mutation and the
    field write in one atomic operation. This form exists purely to render
    editable widgets and produce cleaned_data for that call."""

    # The model allows these null (before inspection); the form requires
    # them, since inspection is meaningless without a real answer for each.
    received_quantity = forms.IntegerField(min_value=0)
    disposition = forms.ChoiceField(choices=ReturnItem.DISPOSITION_CHOICES)
    resolution = forms.ChoiceField(choices=ReturnItem.RESOLUTION_CHOICES)

    class Meta:
        model = ReturnItem
        fields = ["received_quantity", "disposition", "resolution", "remaining_quantity_ml", "inspection_notes"]

    def clean(self):
        cleaned = super().clean()
        disposition = cleaned.get("disposition")
        remaining_ml = cleaned.get("remaining_quantity_ml")
        if disposition == ReturnItem.DISPOSITION_RESTOCKED_PARTIAL and not remaining_ml:
            self.add_error("remaining_quantity_ml", "Required when disposition is restocked_partial.")
        if disposition != ReturnItem.DISPOSITION_RESTOCKED_PARTIAL and remaining_ml:
            self.add_error("remaining_quantity_ml", "Only valid when disposition is restocked_partial.")
        return cleaned


@admin.register(ReturnItem)
class ReturnItemAdmin(ModelAdmin):
    list_display = (
        "id", "return_request", "order_item", "reason",
        "requested_quantity", "approved_quantity", "received_quantity",
        "disposition", "resolution",
    )
    list_filter = ("disposition", "resolution", "reason")
    search_fields = ("return_request__order__order_number",)
    form = ReturnItemInspectionForm

    def has_add_permission(self, request):
        # ReturnItems are only ever created via create_return().
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None):
        return [
            "return_request", "order_item", "reason", "reason_notes",
            "requested_quantity", "approved_quantity",
            "received_quantity", "disposition", "resolution",
            "remaining_quantity_ml", "inspection_notes",
            "inspected_by", "inspected_at",
        ]

    def get_readonly_fields(self, request, obj=None):
        always_readonly = [
            "return_request", "order_item", "reason", "reason_notes",
            "requested_quantity", "approved_quantity", "inspected_by", "inspected_at",
        ]
        if obj is not None and obj.disposition is not None:
            # Already finalized — nothing left to edit; finalize_return_item
            # would reject a second attempt anyway, but there's no reason to
            # invite one.
            return always_readonly + [
                "received_quantity", "disposition", "resolution",
                "remaining_quantity_ml", "inspection_notes",
            ]
        return always_readonly

    def save_model(self, request, obj, form, change):
        try:
            order_services.finalize_return_item(
                obj,
                received_quantity=form.cleaned_data["received_quantity"],
                disposition=form.cleaned_data["disposition"],
                resolution=form.cleaned_data["resolution"],
                remaining_quantity_ml=form.cleaned_data.get("remaining_quantity_ml"),
                inspection_notes=form.cleaned_data.get("inspection_notes", ""),
                inspected_by=request.user,
            )
            self.message_user(request, "Return item inspected and finalized.", level=messages.SUCCESS)
        except order_services.ReturnInspectionError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)


class RefundProcessingForm(forms.ModelForm):
    """Like ReturnItemInspectionForm: looks like a normal ModelForm, but
    RefundAdmin.save_model() never calls form.save()/obj.save() — every
    field change is routed through mark_refund_processing()/complete_refund()/
    fail_refund() instead, so refund_amount can never be hand-edited and a
    completed refund's figures can never be silently overwritten."""

    ACTION_NONE = ""
    ACTION_COMPLETE = "complete"
    ACTION_FAIL = "fail"

    action = forms.ChoiceField(
        choices=[
            (ACTION_NONE, "— No change —"),
            (ACTION_COMPLETE, "Mark Completed"),
            (ACTION_FAIL, "Mark Failed"),
        ],
        required=False,
    )
    refund_reference = forms.CharField(required=False, help_text="UTR / bank reference / gateway refund id.")
    failure_reason = forms.CharField(required=False, widget=forms.Textarea)

    class Meta:
        model = Refund
        fields = ["refund_method", "notes"]


@admin.register(Refund)
class RefundAdmin(ModelAdmin):
    list_display = (
        "id", "return_request", "refund_amount", "refund_status",
        "refund_method", "refund_reference", "processed_at",
    )
    list_filter = ("refund_status", "refund_method")
    search_fields = ("return_request__order__order_number", "refund_reference")
    form = RefundProcessingForm
    actions = ["mark_processing"]

    def has_add_permission(self, request):
        # Refunds are only ever created via create_refund_for_return()
        # (the "Create refund" action on ReturnAdmin) — never a raw add
        # form, since the amount must come from the calculation, not a
        # hand-typed figure.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None):
        return [
            "return_request", "refund_amount", "refund_status", "refund_method",
            "refund_reference", "failure_reason", "processed_at", "processed_by",
            "approved_by", "notes", "action",
        ]

    def get_readonly_fields(self, request, obj=None):
        always_readonly = [
            "return_request", "refund_amount", "refund_status",
            "processed_at", "processed_by", "approved_by",
        ]
        if obj is not None and obj.refund_status in (Refund.STATUS_COMPLETED, Refund.STATUS_FAILED):
            # Terminal states — nothing left to change through this form.
            return always_readonly + ["refund_method", "refund_reference", "failure_reason", "notes"]
        if obj is not None and obj.refund_status == Refund.STATUS_PROCESSING:
            # The method has already been committed to processing money —
            # only the completion fields (reference/failure reason/notes)
            # stay editable from here.
            return always_readonly + ["refund_method"]
        return always_readonly

    def save_model(self, request, obj, form, change):
        action = form.cleaned_data.get("action")
        method_changed = False
        try:
            # refund_method is excluded from the form once readonly (see
            # get_readonly_fields), so it's only present here while the
            # refund is still pending and staff explicitly picked a value.
            if "refund_method" in form.cleaned_data:
                new_method = form.cleaned_data["refund_method"]
                current = Refund.objects.get(pk=obj.pk)
                if new_method != current.refund_method:
                    order_services.set_refund_method(obj, new_method)
                    method_changed = True
            if action == RefundProcessingForm.ACTION_COMPLETE:
                order_services.complete_refund(
                    obj,
                    refund_reference=form.cleaned_data.get("refund_reference", ""),
                    processed_by=request.user,
                    notes=form.cleaned_data.get("notes", ""),
                )
                self.message_user(request, "Refund marked completed.", level=messages.SUCCESS)
            elif action == RefundProcessingForm.ACTION_FAIL:
                order_services.fail_refund(
                    obj,
                    failure_reason=form.cleaned_data.get("failure_reason", ""),
                    processed_by=request.user,
                )
                self.message_user(request, "Refund marked failed.", level=messages.WARNING)
            elif method_changed:
                self.message_user(request, "Refund method updated.", level=messages.SUCCESS)
            else:
                self.message_user(request, "No action selected — nothing changed.", level=messages.INFO)
        except order_services.RefundError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)

    @admin.action(description="Mark selected refunds as Processing")
    def mark_processing(self, request, queryset):
        count = 0
        for refund in queryset:
            try:
                order_services.mark_refund_processing(refund)
                count += 1
            except order_services.RefundError as exc:
                self.message_user(request, f"Refund #{refund.pk}: {exc}", level=messages.WARNING)
        if count:
            self.message_user(request, f"Marked {count} refund(s) as processing.", level=messages.SUCCESS)


class RefundAdjustmentForm(forms.ModelForm):
    class Meta:
        model = RefundAdjustment
        fields = ["refund", "adjustment_amount", "reason"]


@admin.register(RefundAdjustment)
class RefundAdjustmentAdmin(ModelAdmin):
    list_display = ("id", "refund", "adjustment_amount", "approved_by", "created_at")
    search_fields = ("refund__return_request__order__order_number",)
    form = RefundAdjustmentForm

    def has_change_permission(self, request, obj=None):
        # Adjustments are an append-only correction log — never edited
        # once recorded.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:
            return ("refund", "adjustment_amount", "reason", "approved_by", "created_at")
        return ("approved_by", "created_at")

    def save_model(self, request, obj, form, change):
        try:
            order_services.create_refund_adjustment(
                form.cleaned_data["refund"],
                form.cleaned_data["adjustment_amount"],
                form.cleaned_data["reason"],
                approved_by=request.user,
            )
            self.message_user(request, "Refund adjustment recorded.", level=messages.SUCCESS)
        except order_services.RefundError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
