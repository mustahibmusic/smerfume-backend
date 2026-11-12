"""Smerfume Panel Home (/ops/): the operational dashboard (DEC-019).

Each widget names the permissions it needs and the dashboard_selectors
function that computes it. A widget is built only when the user has all
of its permissions; otherwise it is neither rendered nor queried.
"""

from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from apps.ops import dashboard_selectors, presenters
from apps.ops.mixins import OpsAccessMixin
from apps.ops.navigation import quick_links

VIEW_ORDER = "orders.view_order"
VIEW_ORDER_PAYMENT = "orders.view_orderpayment"
VIEW_RETURN = "orders.view_return"
VIEW_REFUND = "orders.view_refund"
VIEW_STOCK = "inventory.view_inventorystock"
VIEW_RESERVATION = "inventory.view_stockreservation"
VIEW_PO = "purchases.view_purchaseorder"
VIEW_GRN = "purchases.view_goodsreceipt"
VIEW_DISCREPANCY = "purchases.view_receiptdiscrepancy"
VIEW_SUPPLIER_PAYMENT = "purchases.view_purchaseorderpayment"


@dataclass(frozen=True)
class Widget:
    key: str
    permissions: tuple
    selector: str  # function name in apps.ops.dashboard_selectors


# KPI cards, chart and tables.
WIDGETS = (
    Widget("order_value_today", (VIEW_ORDER,), "order_value_today"),
    Widget("collected_today", (VIEW_ORDER_PAYMENT,), "collected_today"),
    Widget("in_store_balance_due", (VIEW_ORDER,), "in_store_balance_due"),
    Widget("to_fulfil", (VIEW_ORDER,), "to_fulfil"),
    Widget("stock_health", (VIEW_STOCK,), "stock_health"),
    Widget("stock_incoming", (VIEW_STOCK, VIEW_PO), "stock_incoming"),
    Widget("vendor_outstanding", (VIEW_PO, VIEW_SUPPLIER_PAYMENT), "vendor_outstanding"),
    Widget("order_value_chart", (VIEW_ORDER,), "order_value_chart"),
    Widget("oldest_outstanding", (VIEW_ORDER,), "oldest_outstanding_in_store_orders"),
    Widget("waiting_to_ship", (VIEW_ORDER,), "orders_waiting_to_ship"),
)


@dataclass(frozen=True)
class ExceptionRow:
    key: str
    label: str
    permissions: tuple
    selector: str
    url_name: str = ""
    query: tuple = ()
    tone: str = "warn"


EXCEPTIONS = (
    ExceptionRow(
        "low_no_incoming", "Low stock, no booked incoming", (VIEW_STOCK, VIEW_PO),
        "low_stock_without_incoming", "ops:stock_list",
        (("stock_type", "retail"), ("low_stock", "on"), ("no_incoming", "on")), "danger",
    ),
    ExceptionRow(
        "overdue_pos", "Overdue purchase orders", (VIEW_PO,), "overdue_purchase_orders",
        "ops:po_list", (("overdue", "on"),),
    ),
    ExceptionRow(
        "open_discrepancies", "Open receipt discrepancies", (VIEW_DISCREPANCY,),
        "open_receipt_discrepancies", "ops:discrepancy_list", (("status", "open"),),
    ),
    ExceptionRow(
        "draft_grns", "Draft goods receipts", (VIEW_GRN,), "draft_goods_receipts",
        "ops:grn_list", (("status", "draft"),), "info",
    ),
    ExceptionRow(
        "draft_supplier_payments", "Draft supplier payments", (VIEW_SUPPLIER_PAYMENT,),
        "draft_supplier_payments", "ops:payment_list", (("status", "draft"),), "info",
    ),
    ExceptionRow(
        "returns_received", "Returns received, to inspect", (VIEW_RETURN,), "returns_received",
        "ops:return_list", (("status", "received"),),
    ),
    ExceptionRow(
        "returns_inspection", "Returns awaiting inspection", (VIEW_RETURN,), "returns_inspection_pending",
        "ops:return_list", (("status", "inspection_pending"),),
    ),
    ExceptionRow(
        "refunds_pending", "Refunds pending", (VIEW_REFUND,), "refunds_pending",
        "ops:refund_list", (("refund_status", "pending"),),
    ),
    ExceptionRow(
        "refunds_processing", "Refunds processing", (VIEW_REFUND,), "refunds_processing",
        "ops:refund_list", (("refund_status", "processing"),), "info",
    ),
    ExceptionRow(
        "aged_reservations", "Reservations past warning age", (VIEW_RESERVATION,),
        "reservations_past_warning_age", "ops:reservation_list", (("past_warning_age", "on"),),
    ),
    ExceptionRow(
        "cod_unverified", "COD orders awaiting verification", (VIEW_ORDER,), "cod_awaiting_verification",
        "ops:order_list", (("cod_unverified", "on"),),
    ),
    # Rows link to each affected PO instead of one list.
    ExceptionRow(
        "booked_mismatch", "Booked incoming over-allocated", (VIEW_PO,), "booked_incoming_mismatches",
        tone="danger",
    ),
)


@dataclass(frozen=True)
class QuickAction:
    key: str
    label: str
    permissions: tuple
    url_name: str
    icon: str
    description: str
    query: tuple = ()


# Each action needs the permissions its target page checks.
QUICK_ACTIONS = (
    QuickAction(
        "in_store_sale", "New in-store sale", ("orders.add_order",), "ops:in_store_sale", "sale",
        "Record a walk-in counter sale.",
    ),
    QuickAction(
        "record_payment", "Record payment", (VIEW_ORDER, "orders.add_orderpayment"), "ops:order_list",
        "wallet", "In-store orders with a balance due.",
        (("channel", "in_store"), ("payment_status", "outstanding")),
    ),
    QuickAction(
        "new_po", "New purchase order", (VIEW_PO, "purchases.add_purchaseorder"), "ops:po_new", "po",
        "Start a draft PO for a vendor.",
    ),
    QuickAction(
        "receive_goods", "Receive goods", (VIEW_PO, VIEW_GRN, "purchases.add_goodsreceipt"), "ops:po_list",
        "receipt", "Issued POs ready to receive.", (("status", "issued"),),
    ),
    QuickAction(
        "new_return", "New return", (VIEW_ORDER, "orders.process_return"), "ops:order_list", "returns",
        "Find a delivered order to return.", (("status", "delivered"),),
    ),
    QuickAction(
        "stock_adjustment", "Stock adjustment", (VIEW_STOCK, "inventory.change_inventorystock"),
        "ops:stock_list", "stock", "Pick a stock row to adjust.",
    ),
)


def _url(url_name, query=()):
    url = reverse(url_name)
    return f"{url}?{urlencode(query)}" if query else url


def _order_list(**params):
    return _url("ops:order_list", tuple(params.items()))


def build_exceptions(user):
    """Exception rows the user may see, with a count above zero."""
    rows = []
    for row in EXCEPTIONS:
        if not user.has_perms(row.permissions):
            continue
        data = getattr(dashboard_selectors, row.selector)()
        if data["count"]:
            rows.append({
                "key": row.key, "label": row.label, "tone": row.tone, **data,
                "url": _url(row.url_name, row.query) if row.url_name else None,
            })
    return rows


class OpsHomeView(OpsAccessMixin, TemplateView):
    """GET /ops/: Smerfume Panel Home dashboard (needs core.access_ops).

    KPI cards, a 30-day order value chart, an exceptions panel, two top-5
    tables and quick actions (DEC-019). Every widget is permission-gated:
    without its permissions it is not rendered and its query never runs.
    Read only: GET changes nothing.
    """

    template_name = "ops/home.html"
    nav_active = "home"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        widgets = {
            widget.key: getattr(dashboard_selectors, widget.selector)()
            for widget in WIDGETS
            if user.has_perms(widget.permissions)
        }
        today = timezone.localdate().isoformat()
        context.update({
            "widgets": widgets,
            "exceptions": build_exceptions(user),
            "exceptions_visible": any(user.has_perms(row.permissions) for row in EXCEPTIONS),
            "quick_actions": [
                {**asdict(action), "url": _url(action.url_name, action.query)}
                for action in QUICK_ACTIONS
                if user.has_perms(action.permissions)
            ],
            "quick_links": quick_links(user),
            "oldest_outstanding_rows": [
                presenters.order_row(order) for order in widgets.get("oldest_outstanding", [])
            ],
            "waiting_rows": [
                presenters.order_row_basic(order) for order in widgets.get("waiting_to_ship", [])
            ],
            "links": {
                "today_orders": _order_list(date_from=today, date_to=today),
                "in_store_outstanding": _order_list(channel="in_store", payment_status="outstanding"),
                "confirmed": _order_list(status="confirmed"),
                "processing": _order_list(status="processing"),
                "low_stock": _url("ops:stock_list", (("stock_type", "retail"), ("low_stock", "on"))),
                "po_unpaid": _url("ops:po_list", (("payment_status", "unpaid"),)),
                "po_partially_paid": _url("ops:po_list", (("payment_status", "partially_paid"),)),
            },
            "reservation_warning_hours": settings.OPS_DASHBOARD_RESERVATION_WARNING_HOURS,
        })
        return context


class OpsNotFoundView(OpsAccessMixin, TemplateView):
    """Catch-all for unknown /ops/ paths: branded 404 (after the access gate)."""

    template_name = "ops/404.html"

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context, status=404)


class OpsProfileView(OpsAccessMixin, TemplateView):
    """GET /ops/profile/: the signed-in user's own account, read-only.

    Needs core.access_ops only and shows nobody else's data. Account
    changes (name, password) stay in Django Admin; this page only adds the
    per-browser theme choice.
    """

    template_name = "ops/profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["groups"] = list(self.request.user.groups.order_by("name").values_list("name", flat=True))
        return context
