"""Read-only queries for the Smerfume Panel Home dashboard (DEC-019).

Every function only combines domain selectors and payment helpers:

- order sets: ``apps.orders.selectors``
- customer payments (CPAY, balance due): ``apps.orders.payments``
- stock and reservations: ``apps.inventory.selectors``
- PO payments, overdue POs and booked incoming: ``apps.purchases.selectors``

No business rule is defined here, and nothing is written. The Home view
calls a function only after checking the permissions of its widget, so a
widget the user may not see never runs its query.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.inventory import selectors as inventory_selectors
from apps.inventory.models import InventoryStock, StockReservation
from apps.orders import payments as order_payments
from apps.orders import selectors as order_selectors
from apps.orders.models import Order, OrderPayment, Refund, Return
from apps.purchases import selectors as purchase_selectors
from apps.purchases.models import GoodsReceipt, PurchaseOrderPayment, ReceiptDiscrepancy

ZERO = Decimal("0.00")
CHART_DAYS = 30
TABLE_ROWS = 5


def reservation_warning_cutoff(now=None):
    """Reservations created before this are past the dashboard warning age
    (OPS_DASHBOARD_RESERVATION_WARNING_HOURS). Alerting only: not an
    expiry or release TTL."""
    now = now or timezone.now()
    return now - timedelta(hours=settings.OPS_DASHBOARD_RESERVATION_WARNING_HOURS)


def _money(value):
    return value if value is not None else ZERO


def _retail_stock():
    return InventoryStock.objects.filter(stock_type=InventoryStock.STOCK_TYPE_RETAIL)


# ── KPI cards ────────────────────────────────────────────────────────────────

def order_value_today():
    """Sales today and orders today: Order.total of today's orders, every
    status except cancelled (order value, not net sales), split by channel."""
    orders = order_selectors.order_value_orders().filter(created_at__date=timezone.localdate())
    rows = orders.order_by().values("channel").annotate(total=Sum("total"), count=Count("id"))
    by_channel = {row["channel"]: row for row in rows}
    channels = [
        {
            "key": key,
            "label": label,
            "total": _money(by_channel.get(key, {}).get("total")),
            "count": by_channel.get(key, {}).get("count", 0),
        }
        for key, label in Order.CHANNEL_CHOICES
    ]
    return {
        "total": sum((channel["total"] for channel in channels), ZERO),
        "count": sum(channel["count"] for channel in channels),
        "channels": channels,
    }


def collected_today():
    """CPAY rows with today's payment date (OrderPayment ledger only)."""
    totals = OrderPayment.objects.filter(payment_date=timezone.localdate()).aggregate(
        total=Sum("amount"), count=Count("id"),
    )
    return {"total": _money(totals["total"]), "count": totals["count"]}


def in_store_balance_due():
    """Balance due on in-store orders open for manual Record Payment
    (DEC-013). Online and COD orders are not counted: their collections are
    not yet posted to OrderPayment."""
    totals = order_payments.orders_open_for_manual_payment().aggregate(
        total=Sum("balance_due"), count=Count("id"),
    )
    return {"total": _money(totals["total"]), "count": totals["count"]}


def to_fulfil():
    """Orders still to pack and send, by status."""
    rows = order_selectors.orders_to_fulfil().order_by().values("status").annotate(count=Count("id"))
    counts = {row["status"]: row["count"] for row in rows}
    labels = dict(Order.STATUS_CHOICES)
    statuses = [
        {"key": status, "label": labels[status], "count": counts.get(status, 0)}
        for status in order_selectors.FULFILMENT_STATUSES
    ]
    return {"count": sum(item["count"] for item in statuses), "statuses": statuses}


def stock_health():
    """Physical retail stock: rows at or below reorder level, and rows with
    no free stock."""
    retail = _retail_stock()
    return {
        "low": inventory_selectors.low_stock(retail).count(),
        "out": inventory_selectors.out_of_stock(retail).count(),
    }


def stock_incoming():
    """Booked incoming (not physical) for retail stock: total units, and
    how many low-stock rows have some on the way."""
    keys = purchase_selectors.incoming_sellable_by_stock_key()
    low_keys = inventory_selectors.low_stock(_retail_stock()).values_list("variant_id", "warehouse_id")
    return {
        "units": sum(keys.values(), ZERO),
        "low_with_incoming": sum(1 for key in low_keys if key in keys),
    }


def vendor_outstanding():
    """Operational amount still to pay on payable POs (DEC-010 summary).
    Not the accounting liability; Zoho Books is authoritative."""
    totals = purchase_selectors.outstanding_purchase_orders().aggregate(
        total=Sum("remaining_db"), count=Count("id"),
    )
    return {
        "total": _money(totals["total"]),
        "count": totals["count"],
        "unclassified": purchase_selectors.unclassified_payable_purchase_orders().count(),
    }


# ── Chart ────────────────────────────────────────────────────────────────────

def order_value_chart(days=CHART_DAYS):
    """Daily order value (Order.total, cancelled excluded) for the last
    ``days`` local days, split by channel. Bar heights are percentages of
    the busiest day."""
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    rows = (
        order_selectors.order_value_orders()
        .filter(created_at__date__gte=start)
        .annotate(day=TruncDate("created_at"))
        .order_by()
        .values("day", "channel")
        .annotate(total=Sum("total"))
    )
    totals = {(row["day"], row["channel"]): row["total"] for row in rows}
    series = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        segments = [
            {"key": key, "label": label, "total": _money(totals.get((day, key)))}
            for key, label in Order.CHANNEL_CHOICES
        ]
        series.append({"date": day, "segments": segments, "total": sum((s["total"] for s in segments), ZERO)})
    peak = max((day["total"] for day in series), default=ZERO)
    for day in series:
        day["height"] = round(day["total"] * 100 / peak, 1) if peak else 0
        for segment in day["segments"]:
            segment["share"] = round(segment["total"] * 100 / day["total"], 1) if day["total"] else 0
    return {
        "days": series,
        "peak": peak,
        "total": sum((day["total"] for day in series), ZERO),
        "channels": [{"key": key, "label": label} for key, label in Order.CHANNEL_CHOICES],
        "start": start,
        "end": today,
    }


# ── Exceptions ───────────────────────────────────────────────────────────────

def low_stock_without_incoming():
    low = inventory_selectors.low_stock(_retail_stock())
    return {"count": inventory_selectors.exclude_stock_keys(
        low, purchase_selectors.incoming_sellable_by_stock_key(),
    ).count()}


def overdue_purchase_orders():
    return {"count": purchase_selectors.overdue_purchase_orders().count()}


def open_receipt_discrepancies():
    return {"count": ReceiptDiscrepancy.objects.filter(status=ReceiptDiscrepancy.STATUS_OPEN).count()}


def draft_goods_receipts():
    return {"count": GoodsReceipt.objects.filter(status=GoodsReceipt.STATUS_DRAFT).count()}


def draft_supplier_payments():
    totals = PurchaseOrderPayment.objects.filter(status=PurchaseOrderPayment.STATUS_DRAFT).aggregate(
        total=Sum("amount"), count=Count("id"),
    )
    return {"count": totals["count"], "amount": _money(totals["total"])}


def _returns(status):
    return {"count": Return.objects.filter(status=status).count()}


def returns_received():
    return _returns(Return.STATUS_RECEIVED)


def returns_inspection_pending():
    return _returns(Return.STATUS_INSPECTION_PENDING)


def _refunds(status):
    totals = Refund.objects.filter(refund_status=status).aggregate(total=Sum("refund_amount"), count=Count("id"))
    return {"count": totals["count"], "amount": _money(totals["total"])}


def refunds_pending():
    return _refunds(Refund.STATUS_PENDING)


def refunds_processing():
    return _refunds(Refund.STATUS_PROCESSING)


def reservations_past_warning_age():
    return {"count": inventory_selectors.active_reservations_created_before(
        StockReservation.objects.all(), reservation_warning_cutoff(),
    ).count()}


def cod_awaiting_verification():
    return {"count": order_selectors.orders_awaiting_cod_verification().count()}


def booked_incoming_mismatches():
    """POs with at least one PO line whose active incoming allocations
    exceed what it can still deliver (first five shown)."""
    purchase_orders = []
    for line in purchase_selectors.incoming_allocation_mismatches():
        if line.purchase_order not in purchase_orders:
            purchase_orders.append(line.purchase_order)
    return {"count": len(purchase_orders), "purchase_orders": purchase_orders[:TABLE_ROWS]}


# ── Tables ───────────────────────────────────────────────────────────────────

def oldest_outstanding_in_store_orders(limit=TABLE_ROWS):
    """In-store orders with a balance due, oldest first."""
    return list(
        order_payments.orders_open_for_manual_payment()
        .select_related("user", "shipping_address")
        .order_by("created_at", "id")[:limit]
    )


def orders_waiting_to_ship(limit=TABLE_ROWS):
    """Confirmed or processing orders, oldest first."""
    return list(
        order_selectors.orders_to_fulfil()
        .select_related("user", "shipping_address")
        .annotate(item_count=Count("items"))
        .order_by("created_at", "id")[:limit]
    )
