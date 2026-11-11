import uuid

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_GET
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.widgets import UnfoldAdminSelect2Widget

from apps.catalog.models import ProductVariant
from apps.inventory.models import StockMovement, StockReservationAllocation, Warehouse
from apps.inventory.services.reservation import InsufficientStockError

from . import payments as order_payments
from . import services as order_services
from .forms import (
    ITEM_SEARCH_LIMIT,
    CompleteRefundForm,
    FailRefundForm,
    InStoreSaleForm,
    InStoreSaleLineFormSet,
    RecordPaymentForm,
    ReturnItemInspectionForm,
    ShipOrderForm,
    StaffReturnForm,
    counter_item_rows,
    search_counter_items,
    stale_payment_form,
)
from .models import (
    Order,
    OrderItem,
    OrderPayment,
    Refund,
    RefundAdjustment,
    Return,
    ReturnItem,
    ShippingAddress,
)


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


class PaymentStatusFilter(admin.SimpleListFilter):
    """DEC-013 payment status, derived from OrderPayment rows."""

    title = "payment status"
    parameter_name = "payment_status"

    def lookups(self, request, model_admin):
        return order_payments.STATUS_CHOICES

    def queryset(self, request, queryset):
        if self.value() in dict(order_payments.STATUS_CHOICES):
            return queryset.filter(payment_status=self.value())
        return queryset


_PAYMENT_STATUS_LABELS = dict(order_payments.STATUS_CHOICES)


@admin.register(Order)
class OrderAdmin(StaffActionMixin, ModelAdmin):
    list_display = (
        "order_number", "user", "channel", "status", "payment_method", "total",
        "total_paid_display", "balance_due_display", "payment_status_display",
        "is_guest_order_display", "guest_email", "awaiting_incoming_display", "created_at",
    )
    list_filter = (
        "channel", "status", "payment_method", PaymentStatusFilter, AwaitingIncomingFilter, "created_at",
    )
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
        "payments_received",
        "allocation_breakdown",
        "business_events",
        "admin_history",
    )
    inlines = [OrderItemInline, ShippingAddressInline]
    # Shipping is a detail action only (ship_order): it must capture a
    # tracking number per order, which a bulk action cannot.
    actions = ["verify_cod_orders", "pack_orders", "mark_delivered", "cancel_orders"]
    actions_list = ["new_in_store_sale"]
    actions_detail = ["record_payment", "ship_order", "create_return"]

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
            "description": (
                "Payment method and reference here are summaries kept for compatibility. "
                "Amounts come from Payments received."
            ),
        }),
        ("Payments", {
            "fields": ("payments_received",),
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
        return order_payments.annotate_payment_summary(
            super().get_queryset(request).annotate(awaiting_incoming=_awaiting_incoming())
        )

    @admin.display(description="Paid", ordering="total_paid")
    def total_paid_display(self, obj):
        return obj.total_paid

    @admin.display(description="Balance due", ordering="balance_due")
    def balance_due_display(self, obj):
        return obj.balance_due

    @admin.display(description="Payment status", ordering="payment_status")
    def payment_status_display(self, obj):
        return _PAYMENT_STATUS_LABELS.get(obj.payment_status, obj.payment_status)

    @admin.display(description="Payments received")
    def payments_received(self, obj):
        """DEC-013 payment rows and the totals derived from them (read-only)."""
        if obj is None or obj.pk is None:
            return "—"
        rows = [
            (
                payment.payment_date.strftime("%d/%m/%Y"),
                payment.payment_number,
                payment.get_method_display(),
                payment.reference or "—",
                payment.amount,
                payment.recorded_by or "—",
            )
            for payment in obj.payments.select_related("recorded_by")
        ]
        summary = order_payments.payment_summary(obj)
        cell = '<td class="px-3 py-2">{}</td>'
        table = format_html(
            '<table class="text-sm w-full"><thead><tr class="text-left">'
            '<th class="px-3 py-2">Date</th><th class="px-3 py-2">Payment #</th>'
            '<th class="px-3 py-2">Payment mode</th><th class="px-3 py-2">Reference</th>'
            '<th class="px-3 py-2 text-right">Amount (₹)</th><th class="px-3 py-2">Recorded by</th>'
            "</tr></thead><tbody>{}</tbody></table>",
            format_html_join(
                "", "<tr>" + cell * 4 + '<td class="px-3 py-2 text-right">{}</td>' + cell + "</tr>", rows,
            ) if rows else format_html('<tr><td class="px-3 py-2" colspan="6">{}</td></tr>', "No payments recorded."),
        )
        totals = format_html_join(
            "", '<div class="flex justify-between py-1"><dt>{}</dt><dd>{}</dd></div>',
            (
                ("Order total", f"₹{obj.total}"),
                ("Total paid", f"₹{summary['total_paid']}"),
                ("Balance due", f"₹{summary['balance_due']}"),
                ("Refunded", f"₹{summary['refunded']}"),
                ("Payment status", _PAYMENT_STATUS_LABELS[summary["payment_status"]]),
            ),
        )
        return format_html('{}<dl class="max-w-sm ml-auto mt-3 px-3 text-sm">{}</dl>', table, totals)

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

    def get_urls(self):
        return [
            path(
                "in-store-sale/items/", self.admin_site.admin_view(self.in_store_item_search),
                name="orders_order_in_store_items",
            ),
        ] + super().get_urls()

    @method_decorator(require_GET)
    def in_store_item_search(self, request):
        """Read-only JSON for the in-store item picker. ?q= searches (max 20,
        exact barcode/SKU first); ?skus=A,B refreshes stock for lines
        already chosen. Stock is physical, in ?warehouse=<id>."""
        if not request.user.has_perm("orders.add_order"):
            raise PermissionDenied
        try:
            warehouse = Warehouse.objects.get(pk=int(request.GET.get("warehouse", "")))
        except (ValueError, Warehouse.DoesNotExist):
            return JsonResponse({"error": "Choose a valid warehouse."}, status=400)
        if "skus" in request.GET:
            skus = [s.strip() for s in request.GET["skus"].split(",") if s.strip()][:50]
            variants = ProductVariant.objects.filter(sku__in=skus).select_related("edition__product__brand")
            return JsonResponse({"results": counter_item_rows(variants, warehouse), "more": False})
        query = request.GET.get("q", "")
        variants = search_counter_items(query)
        return JsonResponse({
            "results": counter_item_rows(variants[:ITEM_SEARCH_LIMIT], warehouse, query),
            "more": len(variants) > ITEM_SEARCH_LIMIT,
        })

    def has_new_in_store_sale_permission(self, request, object_id=None):
        # A method, not the dotted "orders.add_order": Unfold's system check
        # queries auth_permission for dotted action permissions, which fails
        # on an empty database before the first migrate.
        return request.user.has_perm("orders.add_order")

    @action(
        description="New in-store sale",
        url_path="in-store-sale",
        permissions=["new_in_store_sale"],
        icon="point_of_sale",
    )
    def new_in_store_sale(self, request):
        """Record a walk-in counter sale through
        order_services.create_in_store_order() — the same service the staff
        API uses. Any failure re-renders the form and creates nothing. The
        sale records no payment (DEC-013): staff then use Record payment on
        the order page this redirects to."""
        if request.method == "POST":
            form = InStoreSaleForm(request.POST)
            formset = InStoreSaleLineFormSet(request.POST, prefix="lines")
            if form.is_valid() and formset.is_valid() and formset.check_stock(form.cleaned_data["warehouse"]):
                data = form.cleaned_data
                lines = formset.lines()
                try:
                    order = order_services.create_in_store_order(
                        customer_mobile=data["customer_mobile"],
                        customer_name=data["customer_name"],
                        lines=lines,
                        discount_amount=data["discount_amount"] or 0,
                        warehouse=data["warehouse"],
                        notes=data["notes"],
                        staff_user=request.user,
                        submission_key=data["submission_key"],
                    )
                except order_services.DuplicateInStoreSale as exc:
                    self.message_user(request, str(exc), level=messages.INFO)
                    return redirect(reverse("admin:orders_order_change", args=[exc.order.pk]))
                except (order_services.InStoreSaleError, InsufficientStockError) as exc:
                    form.add_error(None, str(exc))
                else:
                    self.message_user(
                        request,
                        f"In-store sale {order.order_number} recorded (total {order.total}). "
                        "Record the customer's payment with Record payment.",
                        level=messages.SUCCESS,
                    )
                    return redirect(reverse("admin:orders_order_change", args=[order.pk]))
        else:
            form = InStoreSaleForm(initial={"submission_key": uuid.uuid4()})
            formset = InStoreSaleLineFormSet(prefix="lines")

        formset.attach_item_labels()
        context = {
            **self.admin_site.each_context(request),
            "title": "New in-store sale",
            "opts": self.model._meta,
            "form": form,
            "formset": formset,
            # jQuery + Select2 for the item picker, as Unfold's own select uses.
            "media": form.media + UnfoldAdminSelect2Widget().media,
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

    def has_record_payment_permission(self, request, object_id=None):
        # Manual Record Payment is for in-store orders only (OrderPayment
        # itself is channel-neutral; online gateway/COD capture is deferred).
        # Hidden when nothing is left to pay or the order is cancelled;
        # record_order_payment() refuses both again under the order lock.
        # Unfold checks this on the button and on direct GET/POST access.
        if not self._has_orders_perm(request, "add_orderpayment"):
            return False
        if object_id is None:
            return True
        return order_payments.orders_open_for_manual_payment(Order.objects.filter(pk=object_id)).exists()

    def has_ship_order_permission(self, request, object_id=None):
        if not self._has_orders_perm(request, "ship_order"):
            return False
        if object_id is None:
            return True
        return Order.objects.filter(pk=object_id, status=Order.STATUS_PROCESSING).exists()

    def has_process_return_permission(self, request, object_id=None):
        # Shown only on delivered online orders; create_return() still
        # applies every eligibility rule on submit.
        if not self._has_orders_perm(request, "process_return"):
            return False
        if object_id is None:
            return True
        return Order.objects.filter(
            pk=object_id, status=Order.STATUS_DELIVERED,
        ).exclude(channel=Order.CHANNEL_IN_STORE).exists()

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

    @action(
        description="Create return", url_path="create-return", permissions=["process_return"],
        icon="assignment_return",
    )
    def create_return(self, request, object_id):
        """Staff-created return (phone/WhatsApp). Calls
        order_services.create_return() unchanged, so the customer rules
        apply exactly; any failure creates nothing."""
        order = get_object_or_404(Order, pk=object_id)
        change_url = reverse("admin:orders_order_change", args=[order.pk])
        order_items = order.items.select_related("variant__edition__product__brand").order_by("pk")
        form = StaffReturnForm(request.POST or None, order_items=order_items)
        if request.method == "POST" and form.is_valid():
            try:
                return_request = order_services.create_return(
                    order, form.cleaned_data["items"], requested_by=request.user,
                )
            except order_services.ReturnEligibilityError as exc:
                form.add_error(None, str(exc))
            else:
                self._record(request, order, f"Created return #{return_request.pk} for the customer.")
                ReturnAdmin(Return, self.admin_site)._record(
                    request, return_request, "Created by staff for the customer.",
                )
                self.message_user(
                    request, f"Return #{return_request.pk} created (requested).", level=messages.SUCCESS,
                )
                return redirect(reverse("admin:orders_return_change", args=[return_request.pk]))
        return self._action_page(
            request, order, f"Create return for {order.order_number}", form,
            "Enter what the customer is returning. The same rules as a customer return apply: "
            "delivered orders only, within the return window, up to the quantity bought.",
            "Create return", change_url,
        )

    @action(
        description="Record payment", url_path="record-payment", permissions=["record_payment"],
        icon="payments",
    )
    def record_payment(self, request, object_id):
        """DEC-013 Record Payment: one customer payment per save, through
        order_payments.record_order_payment(). GET changes nothing."""
        order = get_object_or_404(Order.objects.select_related("user"), pk=object_id)
        change_url = reverse("admin:orders_order_change", args=[order.pk])
        summary = order_payments.payment_summary(order)
        if request.method == "POST":
            form = RecordPaymentForm(request.POST)
            if form.is_valid():
                data = form.cleaned_data
                stale = stale_payment_form(form, request.POST, summary["balance_due"])
                if stale is not None:
                    form = stale
                else:
                    try:
                        payment = order_payments.record_order_payment(
                            order, method=data["method"], amount=data["amount"],
                            reference=data["reference"], payment_date=data["payment_date"],
                            notes=data["notes"], recorded_by=request.user,
                        )
                    except order_payments.OrderPaymentError as exc:
                        form.add_error(None, str(exc))
                    else:
                        balance = order_payments.payment_summary(order)["balance_due"]
                        self._record(request, order, f"Recorded payment {payment.payment_number} ({payment.amount}).")
                        self.message_user(
                            request,
                            f"{payment.payment_number} recorded: ₹{payment.amount}. Balance due ₹{balance}.",
                            level=messages.SUCCESS,
                        )
                        return redirect(change_url)
        else:
            form = RecordPaymentForm(initial={
                "amount": summary["balance_due"],
                "expected_balance": summary["balance_due"],
                "payment_date": timezone.localdate(),
            })
        context = {
            **self.admin_site.each_context(request),
            "title": f"Record payment for {order.order_number}",
            "opts": self.model._meta,
            "order": order,
            "summary": summary,
            "status_label": _PAYMENT_STATUS_LABELS[summary["payment_status"]],
            "form": form,
            "back_url": change_url,
        }
        return TemplateResponse(request, "admin/orders/record_payment.html", context)

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


@admin.register(OrderPayment)
class OrderPaymentAdmin(ModelAdmin):
    """DEC-013 customer payments: view only, for everyone including
    superusers. Rows are recorded by the sale flow or the order's Record
    payment action, never here."""

    list_display = (
        "payment_number", "order", "payment_date", "method", "reference", "amount", "recorded_by", "created_at",
    )
    list_filter = ("method", "payment_date")
    search_fields = ("payment_number", "order__order_number", "reference")
    list_select_related = ("order", "recorded_by")
    date_hierarchy = "payment_date"
    readonly_fields = (
        "payment_number", "order", "method", "amount", "reference", "payment_date", "notes",
        "recorded_by", "created_at",
    )
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        # The shipping-refund override changes what the customer is paid;
        # warehouse staff edit this page only to record received quantities.
        if not self._has_orders_perm(request, "process_refund"):
            readonly += ("base_shipping_refund_override",)
        return readonly

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
