"""Inventory pages for the Smerfume Panel.

Same data and permissions as the Django Admin inventory pages. Reads use
the models and apps.inventory.selectors; every write is an
apps.inventory.services call (adjust_inventory_stock, set_reorder_level,
transfer_stock_type, adjust_partial_lot, reallocate_incoming_allocation).
Rows are addressed by public id; warehouses are read-only here
(configuration stays in Django Admin).
"""

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.inventory import selectors as inventory_selectors
from apps.inventory.forms import (
    PartialLotAdjustmentForm,
    ReorderLevelForm,
    StockAdjustmentForm,
    StockTypeTransferForm,
)
from apps.inventory.models import (
    InventoryStock,
    PartialBottleLot,
    StockMovement,
    StockReservation,
    StockReservationAllocation,
    Warehouse,
)
from apps.inventory.services import adjustment as adjustment_services
from apps.inventory.services import reservation as reservation_services
from apps.ops.forms import (
    MovementFilterForm,
    PartialLotFilterForm,
    ReservationFilterForm,
    StockFilterForm,
)
from apps.ops.dashboard_selectors import reservation_warning_cutoff
from apps.ops.mixins import FilteredListMixin, OpsAccessMixin
from apps.purchases import selectors as purchase_selectors

VARIANT_RELATED = "variant__edition__product__brand"
VIEW_PO = "purchases.view_purchaseorder"


def _variant_search(prefix=""):
    def build(q):
        return (
            Q(**{f"{prefix}variant__sku__icontains": q})
            | Q(**{f"{prefix}variant__barcode__iexact": q})
            | Q(**{f"{prefix}variant__edition__name__icontains": q})
            | Q(**{f"{prefix}variant__edition__product__name__icontains": q})
            | Q(**{f"{prefix}variant__edition__product__brand__name__icontains": q})
        )
    return build


def _post_result(request, service_call, success):
    try:
        service_call()
    except adjustment_services.InventoryAdjustmentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, success)


def _form_errors_to_messages(request, form):
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for error in errors:
            messages.error(request, f"{label}: {error}" if label else error)


# ── Stock ────────────────────────────────────────────────────────────────────


class StockListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/inventory/stock/?q=&stock_type=&warehouse=&low_stock=on&no_incoming=on

    ``no_incoming`` (users who may view purchase orders only) drops rows
    with booked incoming units for the same item and warehouse.
    """

    permission_required = ("inventory.view_inventorystock",)
    nav_active = "stock"
    template_name = "ops/inventory/stock_list.html"
    filter_form_class = StockFilterForm

    def base_queryset(self):
        return InventoryStock.objects.select_related(VARIANT_RELATED, "warehouse").order_by(
            "variant__edition__product__brand__name", "variant__edition__product__name",
            "variant__size_ml", "variant__sku", "stock_type",
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(_variant_search()(data["q"]))
        if data.get("stock_type"):
            queryset = queryset.filter(stock_type=data["stock_type"])
        if data.get("warehouse"):
            queryset = queryset.filter(warehouse=data["warehouse"])
        if data.get("low_stock"):
            queryset = inventory_selectors.low_stock(queryset)
        if data.get("no_incoming") and self.request.user.has_perm(VIEW_PO):
            queryset = inventory_selectors.exclude_stock_keys(
                queryset, purchase_selectors.incoming_sellable_by_stock_key(),
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_filter_incoming"] = self.request.user.has_perm(VIEW_PO)
        return context


def _stock_or_404(public_id):
    return get_object_or_404(
        InventoryStock.objects.select_related(VARIANT_RELATED, "warehouse"), public_id=public_id,
    )


def _stock_url(stock):
    return reverse("ops:stock_detail", args=[stock.public_id])


class StockDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/inventory/stock/<public_id>/: figures, recent movements of
    this row, and the forms this user may use."""

    permission_required = ("inventory.view_inventorystock",)
    nav_active = "stock"
    template_name = "ops/inventory/stock_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        stock = _stock_or_404(kwargs["public_id"])
        user = self.request.user
        can_change = user.has_perm("inventory.change_inventorystock")
        can_transfer = (
            user.has_perm("inventory.transfer_stock_type")
            and stock.stock_type in adjustment_services.ALLOWED_STOCK_TYPE_TRANSFERS
        )
        context.update({
            "stock": stock,
            "is_low": inventory_selectors.low_stock(InventoryStock.objects.filter(pk=stock.pk)).exists(),
            "movements": StockMovement.objects.filter(
                variant=stock.variant, warehouse=stock.warehouse, stock_type=stock.stock_type,
            ).select_related("performed_by").order_by("-created_at", "-id")[:20],
            "adjust_form": StockAdjustmentForm() if can_change else None,
            "reorder_form": ReorderLevelForm(initial={"reorder_level": stock.reorder_level})
            if can_change else None,
            "transfer_form": StockTypeTransferForm(stock=stock) if can_transfer else None,
            "can_view_movements": user.has_perm("inventory.view_stockmovement"),
        })
        return context


class StockAdjustView(OpsAccessMixin, View):
    """POST: signed quantity change through adjust_inventory_stock()."""

    permission_required = ("inventory.view_inventorystock", "inventory.change_inventorystock")
    http_method_names = ["post"]

    def post(self, request, public_id):
        stock = _stock_or_404(public_id)
        form = StockAdjustmentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            _post_result(request, lambda: adjustment_services.adjust_inventory_stock(
                stock, quantity_delta=data["quantity_delta"], reason=data["reason"],
                notes=data["notes"], performed_by=request.user,
            ), f"Adjusted by {data['quantity_delta']}.")
        else:
            _form_errors_to_messages(request, form)
        return redirect(_stock_url(stock))


class StockReorderLevelView(OpsAccessMixin, View):
    """POST: set or clear the reorder level through set_reorder_level()."""

    permission_required = ("inventory.view_inventorystock", "inventory.change_inventorystock")
    http_method_names = ["post"]

    def post(self, request, public_id):
        stock = _stock_or_404(public_id)
        form = ReorderLevelForm(request.POST)
        if form.is_valid():
            level = form.cleaned_data["reorder_level"]
            _post_result(
                request, lambda: adjustment_services.set_reorder_level(stock, level), "Reorder level saved.",
            )
        else:
            _form_errors_to_messages(request, form)
        return redirect(_stock_url(stock))


class StockTransferView(OpsAccessMixin, View):
    """POST: move free units to another stock type through
    transfer_stock_type() (approved directions only)."""

    permission_required = ("inventory.view_inventorystock", "inventory.transfer_stock_type")
    http_method_names = ["post"]

    def post(self, request, public_id):
        stock = _stock_or_404(public_id)
        form = StockTypeTransferForm(request.POST, stock=stock)
        if form.is_valid():
            data = form.cleaned_data
            _post_result(request, lambda: adjustment_services.transfer_stock_type(
                stock, to_type=data["to_type"], quantity=data["quantity"], reason=data["reason"],
                notes=data["notes"], performed_by=request.user,
            ), f"Moved {data['quantity']} unit(s) to {data['to_type']}.")
        else:
            _form_errors_to_messages(request, form)
        return redirect(_stock_url(stock))


# ── Partial bottles ──────────────────────────────────────────────────────────


class PartialLotListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/inventory/partial-bottles/?q=&warehouse=&state="""

    permission_required = ("inventory.view_partialbottlelot",)
    nav_active = "partial_bottles"
    template_name = "ops/inventory/partial_list.html"
    filter_form_class = PartialLotFilterForm

    def base_queryset(self):
        return PartialBottleLot.objects.select_related(VARIANT_RELATED, "warehouse").order_by(
            "is_depleted", "-opened_at", "-id",
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(_variant_search()(data["q"]))
        if data.get("warehouse"):
            queryset = queryset.filter(warehouse=data["warehouse"])
        if data.get("state") == "open":
            queryset = queryset.filter(is_depleted=False)
        elif data.get("state") == "depleted":
            queryset = queryset.filter(is_depleted=True)
        return queryset


def _lot_or_404(public_id):
    return get_object_or_404(
        PartialBottleLot.objects.select_related(VARIANT_RELATED, "warehouse", "source_return_item"),
        public_id=public_id,
    )


class PartialLotDetailView(OpsAccessMixin, TemplateView):
    permission_required = ("inventory.view_partialbottlelot",)
    nav_active = "partial_bottles"
    template_name = "ops/inventory/partial_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lot = _lot_or_404(kwargs["public_id"])
        context.update({
            "lot": lot,
            "movements": lot.movements.select_related("performed_by").order_by("-created_at", "-id")[:20],
            "adjust_form": PartialLotAdjustmentForm()
            if self.request.user.has_perm("inventory.change_partialbottlelot") else None,
        })
        return context


class PartialLotAdjustView(OpsAccessMixin, View):
    """POST: signed ml change through adjust_partial_lot()."""

    permission_required = ("inventory.view_partialbottlelot", "inventory.change_partialbottlelot")
    http_method_names = ["post"]

    def post(self, request, public_id):
        lot = _lot_or_404(public_id)
        form = PartialLotAdjustmentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            _post_result(request, lambda: adjustment_services.adjust_partial_lot(
                lot, ml_delta=data["ml_delta"], reason=data["reason"], notes=data["notes"],
                performed_by=request.user,
            ), f"Adjusted by {data['ml_delta']} ml.")
        else:
            _form_errors_to_messages(request, form)
        return redirect(reverse("ops:partial_lot_detail", args=[lot.public_id]))


# ── Stock movements (read-only ledger) ───────────────────────────────────────


class MovementListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/inventory/movements/?q=&movement_type=&reason=&warehouse="""

    permission_required = ("inventory.view_stockmovement",)
    nav_active = "stock_movements"
    template_name = "ops/inventory/movement_list.html"
    paginate_by = 50

    filter_form_class = MovementFilterForm

    def base_queryset(self):
        return StockMovement.objects.select_related(VARIANT_RELATED, "warehouse", "performed_by").order_by(
            "-created_at", "-id",
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(_variant_search()(data["q"]) | Q(notes__icontains=data["q"]))
        for field in ("movement_type", "reason", "warehouse"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset


# ── Reservations (read-only, plus incoming reallocation) ─────────────────────


class ReservationListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/inventory/reservations/?q=&status=&purpose=&warehouse=&past_warning_age=on

    ``past_warning_age``: active reservations older than
    OPS_DASHBOARD_RESERVATION_WARNING_HOURS (a dashboard warning, not an
    expiry rule).
    """

    permission_required = ("inventory.view_stockreservation",)
    nav_active = "reservations"
    template_name = "ops/inventory/reservation_list.html"
    filter_form_class = ReservationFilterForm

    def base_queryset(self):
        return StockReservation.objects.select_related(
            VARIANT_RELATED, "warehouse", "order_item__order",
        ).order_by("-created_at", "-id")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = queryset.filter(
                Q(order_item__order__order_number__icontains=data["q"]) | _variant_search()(data["q"])
            )
        if data.get("past_warning_age"):
            queryset = inventory_selectors.active_reservations_created_before(
                queryset, reservation_warning_cutoff(),
            )
        status = data.get("status")
        if status == "active":
            queryset = inventory_selectors.active_reservations(queryset)
        elif status:
            queryset = queryset.filter(status=status)
        for field in ("purpose", "warehouse"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["reservation_warning_hours"] = settings.OPS_DASHBOARD_RESERVATION_WARNING_HOURS
        return context


class ReservationDetailView(OpsAccessMixin, TemplateView):
    permission_required = ("inventory.view_stockreservation",)
    nav_active = "reservations"
    template_name = "ops/inventory/reservation_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        reservation = get_object_or_404(
            StockReservation.objects.select_related(VARIANT_RELATED, "warehouse", "order_item__order"),
            public_id=kwargs["public_id"],
        )
        allocations = list(reservation.allocations.select_related(
            "partial_lot", "purchase_order_line__purchase_order",
        ).order_by("created_at", "id"))
        can_reallocate = self.request.user.has_perm("inventory.reallocate_incoming_allocation")
        for allocation in allocations:
            allocation.can_reallocate = can_reallocate and (
                allocation.allocation_type == StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE
                and allocation.incoming_status == StockReservationAllocation.INCOMING_ACTIVE
            )
        context.update({
            "reservation": reservation,
            "allocations": allocations,
            "can_view_order": self.request.user.has_perm("orders.view_order"),
        })
        return context


class AllocationReallocateView(OpsAccessMixin, View):
    """POST /ops/inventory/allocations/<public_id>/reallocate/ through
    reservation_services.reallocate_incoming_allocation(). A failure
    changes nothing and is reported."""

    permission_required = ("inventory.view_stockreservation", "inventory.reallocate_incoming_allocation")
    http_method_names = ["post"]

    def post(self, request, public_id):
        allocation = get_object_or_404(
            StockReservationAllocation.objects.select_related("reservation__variant"),
            public_id=public_id,
            allocation_type=StockReservationAllocation.ALLOCATION_INCOMING_PO_LINE,
        )
        try:
            reservation_services.reallocate_incoming_allocation(allocation, request.user)
        except (
            reservation_services.InsufficientStockError,
            reservation_services.InvalidReservationStateError,
        ) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request, f"{allocation.units} incoming unit(s) of {allocation.reservation.variant} reallocated.",
            )
        return redirect(reverse("ops:reservation_detail", args=[allocation.reservation.public_id]))


# ── Warehouses (read-only) ───────────────────────────────────────────────────


class WarehouseListView(OpsAccessMixin, ListView):
    """GET /ops/inventory/warehouses/ (read-only; edited in Django Admin)."""

    permission_required = ("inventory.view_warehouse",)
    nav_active = "warehouses"
    template_name = "ops/inventory/warehouse_list.html"

    def get_queryset(self):
        return Warehouse.objects.annotate(
            stock_rows=Count("inventory_stocks", distinct=True),
            open_lots=Count("partial_lots", filter=Q(partial_lots__is_depleted=False), distinct=True),
        ).order_by("-is_default", "name")
