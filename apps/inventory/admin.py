from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.widgets import (
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
)

from .models import (
    DecantSource,
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockReservationAllocation,
    StockTransaction,
    Supplier,
    Warehouse,
)
from .services import adjustment as adjustment_services
from .services import reservation as reservation_services


@admin.register(Warehouse)
class WarehouseAdmin(ModelAdmin):
    list_display = ("name", "city", "is_default", "is_active")
    search_fields = ("name", "city")


@admin.register(Supplier)
class SupplierAdmin(ModelAdmin):
    """Vendor Master. Safe configuration CRUD. Vendors are deactivated,
    never deleted, so stock history keeps its supplier links."""

    list_display = (
        "vendor_code", "name", "gstin", "city", "phone", "payment_terms_days", "is_active",
    )
    list_filter = ("is_active", "gst_treatment", "state")
    search_fields = ("vendor_code", "name", "legal_name", "gstin", "phone", "email")
    readonly_fields = ("vendor_code",)
    fieldsets = (
        ("Basic details", {"fields": ("vendor_code", "name", "legal_name", "is_active")}),
        ("Contact & address", {"fields": (
            "contact_person", "phone", "email",
            "address_line1", "address_line2", "city", "state", "state_code", "pincode",
            "country", "address",
        )}),
        ("Tax details", {"fields": ("gstin", "pan", "gst_treatment")}),
        ("Commercial", {"fields": ("payment_terms_days", "notes")}),
        ("Integration", {"fields": ("external_accounting_id",), "classes": ("collapse",)}),
    )

    def has_delete_permission(self, request, obj=None):
        return False


# Reasons a manual adjustment may record. Purchases come only from goods
# receipts and opening stock only from opening receipts.
MANUAL_ADJUSTMENT_REASONS = [
    choice for choice in StockMovement.REASON_CHOICES
    if choice[0] not in (StockMovement.REASON_PURCHASE, StockMovement.REASON_OPENING_STOCK)
]


class InventoryStockAdjustmentForm(forms.ModelForm):
    """InventoryStockAdmin makes `quantity` read-only and routes any change
    through these extra fields instead; save_model() never calls a raw
    form.save(). Rows are never added here: they come from goods receipts
    (including opening receipts) and stock-type transfers."""

    quantity_delta = forms.DecimalField(
        required=False,
        help_text="Signed adjustment to apply on save (e.g. +2 stocktaking gain, -2 to write off). "
                   "Leave blank to make no change.",
    )
    reason = forms.ChoiceField(choices=MANUAL_ADJUSTMENT_REASONS, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea)

    class Meta:
        model = InventoryStock
        fields = ["variant", "warehouse", "stock_type", "quantity", "reorder_level"]


class LowStockFilter(admin.SimpleListFilter):
    """Rows with a reorder level whose free stock (quantity - reserved) is
    at or below it."""

    title = "low stock"
    parameter_name = "low_stock"

    def lookups(self, request, model_admin):
        return (("yes", "At or below reorder level"),)

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(
                reorder_level__isnull=False,
                quantity__lte=F("quantity_reserved") + F("reorder_level"),
            )
        return queryset


class ActiveReservationFilter(admin.SimpleListFilter):
    title = "active"
    parameter_name = "active"

    def lookups(self, request, model_admin):
        return (("yes", "Held or confirmed"),)

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(
                status__in=[StockReservation.STATUS_HELD, StockReservation.STATUS_CONFIRMED]
            )
        return queryset


TRANSFER_REASONS = (
    StockMovement.REASON_MARKETING_USE,
    StockMovement.REASON_DAMAGED_GOODS,
    StockMovement.REASON_STOCKTAKING_RESULTS,
    StockMovement.REASON_STOCK_WRITTEN_OFF,
)


class StockTypeTransferForm(forms.Form):
    to_type = forms.ChoiceField(label="Move to", widget=UnfoldAdminSelectWidget)
    quantity = forms.IntegerField(min_value=1, widget=UnfoldAdminIntegerFieldWidget)
    reason = forms.ChoiceField(
        choices=[c for c in StockMovement.REASON_CHOICES if c[0] in TRANSFER_REASONS],
        widget=UnfoldAdminSelectWidget,
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)

    def __init__(self, *args, stock, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = adjustment_services.ALLOWED_STOCK_TYPE_TRANSFERS.get(stock.stock_type, set())
        self.fields["to_type"].choices = [
            c for c in InventoryStock.STOCK_TYPE_CHOICES if c[0] in allowed
        ]


@admin.register(InventoryStock)
class InventoryStockAdmin(ModelAdmin):
    list_display = (
        "variant", "warehouse", "stock_type", "quantity", "quantity_reserved",
        "available", "reorder_level",
    )
    list_filter = ("stock_type", "warehouse", LowStockFilter)
    search_fields = (
        "variant__sku", "variant__edition__name", "variant__edition__product__name",
        "variant__edition__product__brand__name",
    )
    autocomplete_fields = ("variant",)
    form = InventoryStockAdjustmentForm
    actions_detail = ["transfer_stock_type"]

    def has_transfer_stock_type_permission(self, request, object_id=None):
        if not request.user.has_perm("inventory.transfer_stock_type"):
            return False
        if object_id is None:
            return True
        return InventoryStock.objects.filter(
            pk=object_id, stock_type__in=list(adjustment_services.ALLOWED_STOCK_TYPE_TRANSFERS),
        ).exists()

    @action(
        description="Transfer stock type", url_path="transfer",
        permissions=["transfer_stock_type"], icon="swap_horiz",
    )
    def transfer_stock_type(self, request, object_id):
        """GET shows the form; only POST moves free units, through
        adjustment_services.transfer_stock_type()."""
        stock = get_object_or_404(InventoryStock.objects.select_related("variant", "warehouse"), pk=object_id)
        change_url = reverse("admin:inventory_inventorystock_change", args=[stock.pk])
        form = StockTypeTransferForm(request.POST or None, stock=stock)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            try:
                adjustment_services.transfer_stock_type(
                    stock, to_type=data["to_type"], quantity=data["quantity"],
                    reason=data["reason"], notes=data["notes"], performed_by=request.user,
                )
            except adjustment_services.InventoryAdjustmentError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.log_change(request, stock, f"Transferred {data['quantity']} to {data['to_type']}.")
                self.message_user(
                    request, f"Moved {data['quantity']} unit(s) to {data['to_type']}.", level=messages.SUCCESS,
                )
            return redirect(change_url)
        context = {
            **self.admin_site.each_context(request),
            "title": f"Transfer stock type: {stock.variant} [{stock.stock_type}]",
            "opts": self.model._meta,
            "form": form,
            "message": (
                f"Free units: {stock.available} (reserved units never move). "
                "Damaged stock cannot be moved back, and testers or promotional stock "
                "cannot return to retail."
            ),
            "rows": [],
            "button_label": "Transfer",
            "button_class": "bg-primary-600",
            "back_url": change_url,
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)

    def has_add_permission(self, request):
        # Opening stock is an opening goods receipt; other new rows come
        # from purchase receipts and stock-type transfers.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None):
        return [
            "variant", "warehouse", "stock_type", "quantity", "quantity_reserved",
            "reorder_level", "quantity_delta", "reason", "notes",
        ]

    def get_readonly_fields(self, request, obj=None):
        # quantity_reserved is never manually adjustable — it only ever
        # changes as a side effect of reservation/consumption. quantity on
        # an EXISTING row is locked here; the only way to change it is the
        # quantity_delta field below, via adjust_inventory_stock().
        return ["variant", "warehouse", "stock_type", "quantity", "quantity_reserved"]

    def save_model(self, request, obj, form, change):
        # reorder_level is plain configuration (no ledger effect); quantity
        # changes only through adjust_inventory_stock() below.
        reorder_level = form.cleaned_data.get("reorder_level")
        if reorder_level != InventoryStock.objects.get(pk=obj.pk).reorder_level:
            InventoryStock.objects.filter(pk=obj.pk).update(reorder_level=reorder_level)
            self.message_user(request, "Reorder level updated.", level=messages.SUCCESS)
        delta = form.cleaned_data.get("quantity_delta")
        if not delta:
            self.message_user(request, "No quantity_delta given — quantity unchanged.", level=messages.INFO)
            return
        reason = form.cleaned_data.get("reason")
        if not reason:
            self.message_user(request, "A reason is required to adjust quantity.", level=messages.ERROR)
            return
        try:
            adjustment_services.adjust_inventory_stock(
                obj, quantity_delta=delta, reason=reason,
                notes=form.cleaned_data.get("notes", ""), performed_by=request.user,
            )
            self.message_user(request, f"Adjusted by {delta}.", level=messages.SUCCESS)
        except adjustment_services.InventoryAdjustmentError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)

    @admin.display(description="Available")
    def available(self, obj):
        return obj.available


class PartialBottleLotAdjustmentForm(forms.ModelForm):
    ml_delta = forms.DecimalField(
        required=False,
        help_text="Signed adjustment to remaining_ml (e.g. -5 for evaporation/spillage write-off). "
                   "Leave blank to make no change.",
    )
    reason = forms.ChoiceField(choices=MANUAL_ADJUSTMENT_REASONS, required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea)

    class Meta:
        model = PartialBottleLot
        fields = []


@admin.register(PartialBottleLot)
class PartialBottleLotAdmin(ModelAdmin):
    list_display = (
        "variant", "warehouse", "remaining_ml", "reserved_ml", "opened_at", "is_depleted",
    )
    list_filter = ("is_depleted", "warehouse")
    search_fields = ("variant__sku",)
    autocomplete_fields = ("variant",)
    form = PartialBottleLotAdjustmentForm

    def has_add_permission(self, request):
        # A lot only ever originates from a real physical event — a bottle
        # opened during fulfillment, or a restocked_partial return
        # disposition. There is no legitimate "initial" lot to bootstrap
        # the way InventoryStock needs for a brand-new product, so this
        # stays fully disabled rather than needing an add/change split.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None):
        return [
            "variant", "warehouse", "remaining_ml", "reserved_ml", "opened_at", "is_depleted",
            "source_transaction", "source_return_item", "ml_delta", "reason", "notes",
        ]

    def get_readonly_fields(self, request, obj=None):
        return [
            "variant", "warehouse", "remaining_ml", "reserved_ml", "opened_at", "is_depleted",
            "source_transaction", "source_return_item",
        ]

    def save_model(self, request, obj, form, change):
        delta = form.cleaned_data.get("ml_delta")
        if not delta:
            self.message_user(request, "No ml_delta given — nothing changed.", level=messages.INFO)
            return
        reason = form.cleaned_data.get("reason")
        if not reason:
            self.message_user(request, "A reason is required to adjust remaining_ml.", level=messages.ERROR)
            return
        try:
            adjustment_services.adjust_partial_lot(
                obj, ml_delta=delta, reason=reason,
                notes=form.cleaned_data.get("notes", ""), performed_by=request.user,
            )
            self.message_user(request, f"Adjusted by {delta}ml.", level=messages.SUCCESS)
        except adjustment_services.InventoryAdjustmentError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)


@admin.register(DecantSource)
class DecantSourceAdmin(ModelAdmin):
    list_display = ("decant_variant", "source_variant", "decant_volume_ml")
    autocomplete_fields = ("decant_variant", "source_variant")


@admin.register(StockTransaction)
class StockTransactionAdmin(ModelAdmin):
    list_display = ("transaction_type", "performed_by", "created_at")
    list_filter = ("transaction_type",)
    readonly_fields = ("transaction_type", "performed_by", "notes", "created_at")

    def has_add_permission(self, request):
        # Always created as a side effect of a real inventory operation
        # (bottle opening, return disposition) — never directly.
        return False

    def has_change_permission(self, request, obj=None):
        # Ledger header: view only, for everyone including superusers.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(ModelAdmin):
    list_display = (
        "created_at", "variant", "warehouse", "stock_type", "movement_type",
        "quantity_delta", "reason",
    )
    list_filter = ("movement_type", "reason", "warehouse")
    search_fields = ("variant__sku", "notes")
    date_hierarchy = "created_at"
    autocomplete_fields = ("variant", "supplier", "partial_lot")
    readonly_fields = (
        "transaction_group", "variant", "warehouse", "stock_type", "movement_type",
        "quantity_delta", "reason", "notes", "supplier", "partial_lot",
        "source_order_item", "source_return_item", "performed_by", "created_at",
    )

    def has_add_permission(self, request):
        # The append-only ledger — every row is written by a service
        # (reservation consumption, return disposition, manual adjustment),
        # never typed in directly.
        return False

    def has_change_permission(self, request, obj=None):
        # Fully read-only, for everyone including superusers: no field,
        # including ledger links such as source_receipt_line (used by GRN
        # reversals), may be edited by hand.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockReservation)
class StockReservationAdmin(ModelAdmin):
    """Read-only — reservations are entirely order-driven
    (apps.inventory.services.reservation). Registered for support/
    debugging visibility only; no field here is ever meant to be
    hand-edited, and doing so would desynchronize it from the
    InventoryStock/PartialBottleLot rows it's holding capacity against."""

    list_display = ("order_item", "variant", "warehouse", "purpose", "quantity", "status", "created_at")
    list_filter = (ActiveReservationFilter, "status", "purpose", "warehouse")
    search_fields = ("order_item__order__order_number", "variant__sku")
    autocomplete_fields = ("variant",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockReservationAllocation)
class StockReservationAllocationAdmin(ModelAdmin):
    """Read-only, same reasoning as StockReservationAdmin — these rows are
    the exact record of what release_reservation()/consume_reservation()
    must replay; editing them would corrupt that replay."""

    list_display = (
        "reservation", "allocation_type", "units", "partial_lot", "ml_amount",
        "purchase_order_line", "incoming_status", "replacement", "split_from",
    )
    list_filter = ("allocation_type", "incoming_status")
    list_select_related = (
        "reservation", "partial_lot", "purchase_order_line__variant__edition__product__brand",
        "replacement", "split_from",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "<int:object_id>/reallocate/",
                self.admin_site.admin_view(self.reallocate_view),
                name="inventory_stockreservationallocation_reallocate",
            ),
            *super().get_urls(),
        ]

    def reallocate_view(self, request, object_id):
        """Reallocate one active incoming allocation (spec §5.6) through
        reservation_services.reallocate_incoming_allocation. GET shows a
        confirmation page; only POST changes anything. A failure changes
        nothing and names the quantity that could not be covered."""
        if not request.user.has_perm("inventory.reallocate_incoming_allocation"):
            raise PermissionDenied
        allocation = get_object_or_404(
            StockReservationAllocation.objects.select_related(
                "reservation__order_item__order", "reservation__variant",
                "purchase_order_line__purchase_order",
            ),
            pk=object_id,
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
        )
        order = allocation.reservation.order_item.order
        order_url = reverse("admin:orders_order_change", args=[order.pk])
        if request.method == "POST":
            try:
                reservation_services.reallocate_incoming_allocation(allocation, request.user)
            except (
                reservation_services.InsufficientStockError,
                reservation_services.InvalidReservationStateError,
            ) as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(
                    request,
                    f"{order.order_number}: {allocation.units} incoming unit(s) of "
                    f"{allocation.reservation.variant} reallocated.",
                    level=messages.SUCCESS,
                )
            return redirect(order_url)
        po_number = allocation.purchase_order_line.purchase_order.po_number
        context = {
            **self.admin_site.each_context(request),
            "title": f"Reallocate incoming units for {order.order_number}",
            "opts": self.model._meta,
            "form": forms.Form(),
            "message": (
                f"Move {allocation.units} unit(s) of {allocation.reservation.variant} off "
                f"{po_number}. Free physical stock is used first, then other vendor-confirmed "
                "incoming stock. If the full quantity cannot be covered, nothing changes and "
                "the order is not cancelled."
            ),
            "rows": [f"Current status: {allocation.get_incoming_status_display()}"],
            "button_label": "Reallocate",
            "button_class": "bg-primary-600",
            "back_url": order_url,
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)
