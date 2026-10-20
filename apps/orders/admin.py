from django import forms
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html, format_html_join
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
from apps.inventory.models import StockMovement, StockReservationAllocation, Warehouse
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


class ShipOrderForm(forms.Form):
    # Required here even though mark_order_shipped() accepts none: shipping
    # from the admin must always record the courier tracking number.
    tracking_number = forms.CharField(max_length=100, widget=UnfoldAdminTextInputWidget)


class CompleteRefundForm(forms.Form):
    refund_reference = forms.CharField(
        max_length=100, widget=UnfoldAdminTextInputWidget,
        help_text="UTR / bank reference / gateway refund id.",
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)


class FailRefundForm(forms.Form):
    failure_reason = forms.CharField(widget=UnfoldAdminTextareaWidget)


class StaffActionMixin:
    """Shared helpers for state-changing admin actions: a confirmation step
    for bulk actions, the confirmed-action page for detail actions, and an
    admin history (LogEntry) record of who ran each action."""

    def _has_orders_perm(self, request, codename):
        return request.user.has_perm(f"orders.{codename}")

    def _confirm_bulk(self, request, queryset, action, message, button_label):
        """Return a confirmation page until the POST carries confirm=yes;
        then return None so the caller performs the action."""
        if request.POST.get("confirm") == "yes":
            return None
        context = {
            **self.admin_site.each_context(request),
            "title": "Please confirm",
            "opts": self.model._meta,
            "queryset": queryset,
            "action": action,
            "message": message,
            "button_label": button_label,
            "button_class": "bg-red-600",
            "back_url": request.get_full_path(),
        }
        return TemplateResponse(request, "admin/orders/confirm_bulk_action.html", context)

    def _action_page(self, request, obj, title, form, message, button_label, back_url,
                     button_class="bg-primary-600"):
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.model._meta,
            "form": form,
            "message": message,
            "rows": [],
            "button_label": button_label,
            "button_class": button_class,
            "back_url": back_url,
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)

    def _record(self, request, obj, message):
        self.log_change(request, obj, message)


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


def _awaiting_incoming():
    """True while any item of the order has an active incoming (booked)
    allocation, i.e. waits for stock that has not arrived."""
    return Exists(StockReservationAllocation.objects.filter(
        reservation__order_item__order=OuterRef("pk"),
        allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
        incoming_status=StockReservationAllocation.INCOMING_ACTIVE,
    ))


class AwaitingIncomingFilter(admin.SimpleListFilter):
    title = "awaiting incoming"
    parameter_name = "awaiting_incoming"

    def lookups(self, request, model_admin):
        return (("yes", "Yes"), ("no", "No"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(awaiting_incoming=True)
        if self.value() == "no":
            return queryset.filter(awaiting_incoming=False)
        return queryset


@admin.register(Order)
class OrderAdmin(StaffActionMixin, ModelAdmin):
    list_display = (
        "order_number", "user", "channel", "status", "payment_method", "total",
        "is_guest_order_display", "guest_email", "awaiting_incoming_display", "created_at",
    )
    list_filter = ("channel", "status", "payment_method", AwaitingIncomingFilter, "created_at")
    search_fields = (
        "order_number", "user__mobile_number", "user__email", "guest_email",
        "user__first_name", "user__last_name",
        "shipping_address__mobile", "shipping_address__full_name",
    )
    date_hierarchy = "created_at"
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
        "allocation_breakdown",
        "business_events",
        "admin_history",
    )
    inlines = [OrderItemInline, ShippingAddressInline]
    # Shipping is a detail action only (ship_order): it must capture a
    # tracking number per order, which a bulk action cannot.
    actions = ["verify_cod_orders", "pack_orders", "mark_delivered", "cancel_orders"]
    actions_list = ["new_in_store_sale"]
    actions_detail = ["ship_order"]

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
        ("Stock allocation", {
            "fields": ("allocation_breakdown",),
        }),
        ("Replacement", {
            "fields": ("is_replacement", "original_order"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "delivered_at", "tracking_number"),
        }),
        ("Timeline", {
            "fields": ("business_events", "admin_history"),
            "description": (
                "Business events come from the order's own records. Admin action "
                "history lists actions run in this admin; changes made through the "
                "API or services directly do not appear there."
            ),
        }),
    )

    @admin.display(description="Guest order", boolean=True)
    def is_guest_order_display(self, obj):
        return obj.is_guest_order

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(awaiting_incoming=_awaiting_incoming())

    @admin.display(description="Awaiting incoming", boolean=True, ordering="awaiting_incoming")
    def awaiting_incoming_display(self, obj):
        return obj.awaiting_incoming

    @admin.display(description="Stock allocation")
    def allocation_breakdown(self, obj):
        """Per-item physical / incoming allocation rows (read-only)."""
        allocations = (
            StockReservationAllocation.objects.filter(reservation__order_item__order=obj)
            .select_related(
                "reservation__order_item__variant",
                "purchase_order_line__purchase_order",
            )
            .order_by("reservation__order_item_id", "pk")
        )
        rows = []
        for allocation in allocations:
            action_link = "—"
            if allocation.allocation_type == StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE:
                source = "Incoming"
                po_number = allocation.purchase_order_line.purchase_order.po_number
                amount = f"{allocation.units} pcs"
                state = allocation.get_incoming_status_display()
                if allocation.incoming_status == StockReservationAllocation.INCOMING_ACTIVE:
                    action_link = format_html(
                        '<a href="{}">Reallocate</a>',
                        reverse(
                            "admin:inventory_stockreservationallocation_reallocate",
                            args=[allocation.pk],
                        ),
                    )
            else:
                source = "Physical"
                po_number = "—"
                if allocation.allocation_type == StockReservationAllocation.ALLOCATION_PARTIAL_LOT:
                    amount = f"{allocation.ml_amount} ml"
                else:
                    amount = f"{allocation.units} pcs"
                state = allocation.reservation.get_status_display()
            rows.append((
                allocation.reservation.order_item.variant, source, po_number, amount, state,
                action_link,
            ))
        if not rows:
            return "—"
        return format_html(
            "<table><thead><tr><th>Item</th><th>Source</th><th>PO</th><th>Quantity</th>"
            "<th>Status</th><th>Action</th></tr></thead><tbody>{}</tbody></table>",
            format_html_join(
                "", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                rows,
            ),
        )

    @admin.display(description="Business events")
    def business_events(self, obj):
        """Events derived from data the order already stores (no history
        model). Shipping and cancellation times are not stored on the order;
        they appear in the admin action history when done in the admin."""
        events = [(obj.created_at, f"Order placed ({obj.get_channel_display()})", obj.created_by)]
        if obj.cod_verified_at:
            events.append((obj.cod_verified_at, "COD verified", obj.cod_verified_by))
        consumed = (
            StockMovement.objects.filter(source_order_item__order=obj)
            .select_related("performed_by").order_by("created_at").first()
        )
        if consumed:
            events.append((consumed.created_at, "Stock consumed (packed / sold)", consumed.performed_by))
        if obj.tracking_number:
            events.append((None, f"Shipped, tracking {obj.tracking_number}", None))
        if obj.delivered_at:
            events.append((obj.delivered_at, "Delivered", None))
        for return_request in obj.returns.all():
            events.append((
                return_request.created_at,
                f"Return #{return_request.pk} requested (now {return_request.get_status_display()})",
                None,
            ))
            for refund in return_request.refunds.select_related("processed_by"):
                if refund.processed_at:
                    events.append((
                        refund.processed_at,
                        f"Refund {refund.get_refund_status_display().lower()} ({refund.refund_amount})",
                        refund.processed_by,
                    ))
        dated = sorted((e for e in events if e[0]), key=lambda e: e[0])
        undated = [e for e in events if not e[0]]
        return format_html(
            "<table><tbody>{}</tbody></table>",
            format_html_join(
                "", "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
                ((when or "time not recorded", label, who or "—") for when, label, who in dated + undated),
            ),
        )

    @admin.display(description="Admin action history")
    def admin_history(self, obj):
        """LogEntry rows for this order and its returns and refunds."""
        return_ids = list(obj.returns.values_list("pk", flat=True))
        refund_ids = list(Refund.objects.filter(return_request__order=obj).values_list("pk", flat=True))
        targets = Q(content_type=ContentType.objects.get_for_model(Order), object_id=str(obj.pk))
        if return_ids:
            targets |= Q(
                content_type=ContentType.objects.get_for_model(Return),
                object_id__in=[str(pk) for pk in return_ids],
            )
        if refund_ids:
            targets |= Q(
                content_type=ContentType.objects.get_for_model(Refund),
                object_id__in=[str(pk) for pk in refund_ids],
            )
        entries = LogEntry.objects.filter(targets).select_related("user").order_by("-action_time")
        rows = format_html_join(
            "", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            (
                (entry.action_time, entry.user, entry.object_repr, entry.get_change_message())
                for entry in entries
            ),
        )
        return format_html("<table><tbody>{}</tbody></table>", rows) if entries else "—"

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

    # --- state-changing actions ---
    # Each needs its own custom permission (orders.0011); view or change
    # permission alone never runs one. Bulk actions that move or release
    # stock show a confirmation page first.

    def has_verify_cod_order_permission(self, request):
        return self._has_orders_perm(request, "verify_cod_order")

    def has_pack_order_permission(self, request):
        return self._has_orders_perm(request, "pack_order")

    def has_deliver_order_permission(self, request):
        return self._has_orders_perm(request, "deliver_order")

    def has_cancel_order_permission(self, request):
        return self._has_orders_perm(request, "cancel_order")

    def has_ship_order_permission(self, request, object_id=None):
        if not self._has_orders_perm(request, "ship_order"):
            return False
        if object_id is None:
            return True
        return Order.objects.filter(pk=object_id, status=Order.STATUS_PROCESSING).exists()

    def _run_order_action(self, request, queryset, service_fn, error_cls, verb, log_message):
        count = 0
        for order in queryset:
            try:
                service_fn(order)
            except error_cls as exc:
                self.message_user(request, f"{order.order_number}: {exc}", level=messages.WARNING)
            else:
                self._record(request, order, log_message)
                count += 1
        if count:
            self.message_user(request, f"{verb} {count} order(s).", level=messages.SUCCESS)

    @admin.action(
        description="Verify selected COD orders (confirms reservation, advances to Confirmed)",
        permissions=["verify_cod_order"],
    )
    def verify_cod_orders(self, request, queryset):
        self._run_order_action(
            request, queryset,
            lambda order: order_services.verify_cod_order(order, verified_by=request.user),
            order_services.CODVerificationError, "Verified", "Verified COD order.",
        )

    @admin.action(
        description="Pack selected orders (consumes reserved inventory, advances to Processing)",
        permissions=["pack_order"],
    )
    def pack_orders(self, request, queryset):
        confirmation = self._confirm_bulk(
            request, queryset, "pack_orders",
            "Pack these orders? Their reserved stock is consumed and they move to Processing.",
            "Pack orders",
        )
        if confirmation:
            return confirmation
        self._run_order_action(
            request, queryset,
            lambda order: order_services.pack_order(order, performed_by=request.user),
            order_services.OrderPackingError, "Packed", "Packed order.",
        )

    @admin.action(description="Mark selected orders as Delivered", permissions=["deliver_order"])
    def mark_delivered(self, request, queryset):
        confirmation = self._confirm_bulk(
            request, queryset, "mark_delivered",
            "Mark these orders as delivered? This starts the return window.",
            "Mark delivered",
        )
        if confirmation:
            return confirmation
        self._run_order_action(
            request, queryset, order_services.mark_order_delivered,
            order_services.OrderTransitionError, "Marked delivered", "Marked order delivered.",
        )

    @admin.action(
        description="Cancel selected orders (releases reserved inventory)",
        permissions=["cancel_order"],
    )
    def cancel_orders(self, request, queryset):
        confirmation = self._confirm_bulk(
            request, queryset, "cancel_orders",
            "Cancel these orders? Their reserved stock is released. This cannot be undone.",
            "Cancel orders",
        )
        if confirmation:
            return confirmation
        self._run_order_action(
            request, queryset, order_services.cancel_order,
            order_services.OrderTransitionError, "Cancelled", "Cancelled order.",
        )

    @action(description="Ship order", url_path="ship", permissions=["ship_order"], icon="local_shipping")
    def ship_order(self, request, object_id):
        """GET shows the tracking-number form; only a valid POST ships the
        order, through order_services.mark_order_shipped()."""
        order = get_object_or_404(Order, pk=object_id)
        change_url = reverse("admin:orders_order_change", args=[order.pk])
        form = ShipOrderForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            tracking_number = form.cleaned_data["tracking_number"]
            try:
                order_services.mark_order_shipped(order, tracking_number=tracking_number)
            except order_services.OrderTransitionError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self._record(request, order, f"Shipped order (tracking {tracking_number}).")
                self.message_user(
                    request, f"{order.order_number} marked shipped.", level=messages.SUCCESS,
                )
            return redirect(change_url)
        return self._action_page(
            request, order, f"Ship {order.order_number}", form,
            "Enter the courier tracking number. The order moves to Shipped. Stock is not changed.",
            "Ship order", change_url,
        )


class ReturnItemInline(admin.TabularInline):
    model = ReturnItem
    extra = 0
    fields = ("order_item", "reason", "reason_notes", "requested_quantity", "received_quantity")
    readonly_fields = ("order_item", "reason", "reason_notes", "requested_quantity")
    can_delete = False

    # received_quantity is a plain field only until the goods arrive: it must
    # be set before mark_return_received(). After that the inspection form
    # (finalize_return_item) is the only place that may change it.
    RECEIVED_QUANTITY_EDITABLE_STATUSES = (Return.STATUS_APPROVED, Return.STATUS_IN_TRANSIT)

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is None or obj.status not in self.RECEIVED_QUANTITY_EDITABLE_STATUSES:
            readonly += ("received_quantity",)
        return readonly


@admin.register(Return)
class ReturnAdmin(StaffActionMixin, ModelAdmin):
    list_display = ("id", "order", "status", "created_at")
    list_filter = ("status", "created_at")
    date_hierarchy = "created_at"
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

    def has_process_return_permission(self, request):
        return self._has_orders_perm(request, "process_return")

    def has_receive_return_permission(self, request):
        return self._has_orders_perm(request, "receive_return")

    def has_process_refund_permission(self, request):
        return self._has_orders_perm(request, "process_refund")

    def _run_action(self, request, queryset, service_fn, error_cls, verb):
        count = 0
        for return_request in queryset:
            try:
                service_fn(return_request)
            except error_cls as exc:
                self.message_user(
                    request, f"Return #{return_request.pk}: {exc}", level=messages.WARNING
                )
            else:
                self._record(request, return_request, f"{verb} return.")
                count += 1
        if count:
            self.message_user(request, f"{verb} {count} return(s).", level=messages.SUCCESS)

    @admin.action(description="Approve selected returns", permissions=["process_return"])
    def approve_returns(self, request, queryset):
        self._run_action(
            request, queryset, order_services.approve_return,
            order_services.ReturnTransitionError, "Approved",
        )

    @admin.action(description="Reject selected returns", permissions=["process_return"])
    def reject_returns(self, request, queryset):
        confirmation = self._confirm_bulk(
            request, queryset, "reject_returns",
            "Reject these return requests? The customer's return is closed.",
            "Reject returns",
        )
        if confirmation:
            return confirmation
        self._run_action(
            request, queryset, order_services.reject_return,
            order_services.ReturnTransitionError, "Rejected",
        )

    @admin.action(description="Cancel selected returns", permissions=["process_return"])
    def cancel_returns(self, request, queryset):
        self._run_action(
            request, queryset, order_services.cancel_return,
            order_services.ReturnTransitionError, "Cancelled",
        )

    @admin.action(description="Mark selected returns as In Transit", permissions=["receive_return"])
    def mark_in_transit(self, request, queryset):
        self._run_action(
            request, queryset, order_services.mark_return_in_transit,
            order_services.ReturnTransitionError, "Marked in-transit",
        )

    @admin.action(
        description="Mark selected returns as Received (requires received_quantity set on every line)",
        permissions=["receive_return"],
    )
    def mark_received(self, request, queryset):
        self._run_action(
            request, queryset, order_services.mark_return_received,
            order_services.ReturnTransitionError, "Marked received",
        )

    @admin.action(description="Start inspection on selected returns", permissions=["receive_return"])
    def start_inspection(self, request, queryset):
        self._run_action(
            request, queryset, order_services.start_inspection,
            order_services.ReturnTransitionError, "Started inspection on",
        )

    @admin.action(
        description="Create refund for selected returns (completed returns only)",
        permissions=["process_refund"],
    )
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
            except order_services.RefundError as exc:
                self.message_user(
                    request, f"Return #{return_request.pk}: {exc}", level=messages.WARNING
                )
            else:
                self._record(request, return_request, "Created refund.")
                count += 1
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
    RefundAdmin.save_model() never calls form.save()/obj.save(). The only
    field it offers is refund_method, routed through set_refund_method().
    Processing, completion and failure are confirmed detail actions
    (process_refund_detail / complete_refund_detail / fail_refund_detail),
    so refund_amount can never be hand-edited and a completed refund's
    figures can never be silently overwritten."""

    class Meta:
        model = Refund
        fields = ["refund_method"]


@admin.register(Refund)
class RefundAdmin(StaffActionMixin, ModelAdmin):
    list_display = (
        "id", "return_request", "refund_amount", "refund_status",
        "refund_method", "refund_reference", "processed_at",
    )
    list_filter = ("refund_status", "refund_method")
    date_hierarchy = "created_at"
    search_fields = ("return_request__order__order_number", "refund_reference")
    form = RefundProcessingForm
    actions = ["mark_processing"]
    actions_detail = ["process_refund_detail", "complete_refund_detail", "fail_refund_detail"]

    def has_add_permission(self, request):
        # Refunds are only ever created via create_refund_for_return()
        # (the "Create refund" action on ReturnAdmin) — never a raw add
        # form, since the amount must come from the calculation, not a
        # hand-typed figure.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _refund_in_status(self, object_id, status):
        if object_id is None:
            return True
        return Refund.objects.filter(pk=object_id, refund_status=status).exists()

    def has_process_refund_permission(self, request, object_id=None):
        return self._has_orders_perm(request, "process_refund") and self._refund_in_status(
            object_id, Refund.STATUS_PENDING
        )

    def has_complete_refund_permission(self, request, object_id=None):
        # Money movement: completing or failing a refund needs its own
        # permission, separate from creating/processing one.
        return self._has_orders_perm(request, "complete_refund") and self._refund_in_status(
            object_id, Refund.STATUS_PROCESSING
        )

    def get_fields(self, request, obj=None):
        return [
            "return_request", "refund_amount", "refund_status", "refund_method",
            "refund_reference", "failure_reason", "processed_at", "processed_by",
            "approved_by", "notes",
        ]

    def get_readonly_fields(self, request, obj=None):
        readonly = [
            "return_request", "refund_amount", "refund_status", "refund_reference",
            "failure_reason", "processed_at", "processed_by", "approved_by", "notes",
        ]
        # The method is chosen only while the refund is pending, and only by
        # staff who may process refunds; once processing, it is committed.
        method_editable = (
            obj is not None
            and obj.refund_status == Refund.STATUS_PENDING
            and self._has_orders_perm(request, "process_refund")
        )
        if not method_editable:
            readonly.append("refund_method")
        return readonly

    def save_model(self, request, obj, form, change):
        if "refund_method" not in form.cleaned_data:
            return
        new_method = form.cleaned_data["refund_method"]
        if new_method == Refund.objects.get(pk=obj.pk).refund_method:
            self.message_user(request, "No change.", level=messages.INFO)
            return
        try:
            order_services.set_refund_method(obj, new_method)
        except order_services.RefundError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
        else:
            self._record(request, obj, f"Set refund method to {new_method}.")
            self.message_user(request, "Refund method updated.", level=messages.SUCCESS)

    def _change_url(self, refund):
        return reverse("admin:orders_refund_change", args=[refund.pk])

    def _mark_processing(self, request, refund):
        try:
            order_services.mark_refund_processing(refund)
        except order_services.RefundError as exc:
            self.message_user(request, f"Refund #{refund.pk}: {exc}", level=messages.WARNING)
            return False
        self._record(request, refund, "Marked refund processing.")
        return True

    @admin.action(description="Mark selected refunds as Processing", permissions=["process_refund"])
    def mark_processing(self, request, queryset):
        confirmation = self._confirm_bulk(
            request, queryset, "mark_processing",
            "Mark these refunds as processing? The refund method can no longer be changed.",
            "Mark processing",
        )
        if confirmation:
            return confirmation
        count = sum(1 for refund in queryset if self._mark_processing(request, refund))
        if count:
            self.message_user(request, f"Marked {count} refund(s) as processing.", level=messages.SUCCESS)

    @action(
        description="Mark processing", url_path="process", permissions=["process_refund"],
        icon="pending",
    )
    def process_refund_detail(self, request, object_id):
        refund = get_object_or_404(Refund, pk=object_id)
        if request.method == "POST":
            if self._mark_processing(request, refund):
                self.message_user(request, "Refund marked processing.", level=messages.SUCCESS)
            return redirect(self._change_url(refund))
        return self._action_page(
            request, refund, f"Mark refund #{refund.pk} as processing", forms.Form(),
            f"Start paying {refund.refund_amount} by {refund.get_refund_method_display() or '—'}. "
            "The refund method can no longer be changed.",
            "Mark processing", self._change_url(refund),
        )

    @action(
        description="Complete refund", url_path="complete", permissions=["complete_refund"],
        icon="task_alt",
    )
    def complete_refund_detail(self, request, object_id):
        refund = get_object_or_404(Refund, pk=object_id)
        form = CompleteRefundForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                order_services.complete_refund(
                    refund, refund_reference=form.cleaned_data["refund_reference"],
                    processed_by=request.user, notes=form.cleaned_data["notes"],
                )
            except order_services.RefundError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self._record(request, refund, "Completed refund.")
                self.message_user(request, "Refund marked completed.", level=messages.SUCCESS)
            return redirect(self._change_url(refund))
        return self._action_page(
            request, refund, f"Complete refund #{refund.pk}", form,
            f"Confirm that {refund.refund_amount} has been paid to the customer. "
            "A completed refund cannot be changed; later corrections need a refund adjustment.",
            "Complete refund", self._change_url(refund), button_class="bg-red-600",
        )

    @action(
        description="Mark failed", url_path="fail", permissions=["complete_refund"], icon="error",
    )
    def fail_refund_detail(self, request, object_id):
        refund = get_object_or_404(Refund, pk=object_id)
        form = FailRefundForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                order_services.fail_refund(
                    refund, failure_reason=form.cleaned_data["failure_reason"],
                    processed_by=request.user,
                )
            except order_services.RefundError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self._record(request, refund, "Marked refund failed.")
                self.message_user(request, "Refund marked failed.", level=messages.WARNING)
            return redirect(self._change_url(refund))
        return self._action_page(
            request, refund, f"Mark refund #{refund.pk} as failed", form,
            "Record why the payment failed. The amount becomes claimable again by a later refund.",
            "Mark failed", self._change_url(refund), button_class="bg-red-600",
        )


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
