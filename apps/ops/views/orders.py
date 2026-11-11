"""Read-only order views for /ops/.

Totals are the order's own frozen fields. Paid, balance due and payment
status come from apps.orders.payments, the same functions Django Admin
uses. Nothing here writes to the database.
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import ListView, TemplateView

from apps.ops import presenters
from apps.ops.access import has_admin_access
from apps.ops.forms import OrderFilterForm
from apps.ops.mixins import OpsAccessMixin
from apps.orders import payments as order_payments
from apps.orders.models import Order

VIEW_ORDER = "orders.view_order"


class OrderListView(OpsAccessMixin, ListView):
    """GET /ops/orders/?q=&status=&channel=&payment_status=&page=

    Newest first, 25 per page. ``q`` matches order number, customer
    name/mobile/email and shipping name/mobile. Invalid filter values are
    reported and ignored. An out-of-range or non-numeric ``page`` returns
    the branded 404 (Django paginator behaviour).
    """

    permission_required = (VIEW_ORDER,)
    nav_active = "orders"
    template_name = "ops/orders/list.html"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = OrderFilterForm(self.request.GET or None)
        queryset = order_payments.annotate_payment_summary(
            Order.objects.select_related("user", "shipping_address")
        ).order_by("-created_at", "-id")
        if not self.filter_form.is_bound:
            return queryset
        self.filter_form.is_valid()
        data = self.filter_form.cleaned_data
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(order_number__icontains=q)
                | Q(user__mobile_number__icontains=q)
                | Q(user__email__icontains=q)
                | Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
                | Q(guest_email__icontains=q)
                | Q(shipping_address__mobile__icontains=q)
                | Q(shipping_address__full_name__icontains=q)
            )
        for field in ("status", "channel", "payment_status"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context.update({
            "filter_form": self.filter_form,
            "rows": [presenters.order_row(order) for order in context["object_list"]],
            "query_string": params.urlencode(),
            "is_filtered": any(value for value in params.values()),
        })
        return context


class OrderDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/orders/<order_number>/ (read-only). Unknown number: 404."""

    permission_required = (VIEW_ORDER,)
    nav_active = "orders"
    template_name = "ops/orders/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = get_object_or_404(
            Order.objects.select_related("user", "shipping_address", "created_by", "original_order"),
            order_number=kwargs["order_number"],
        )
        summary = order_payments.payment_summary(order)
        admin_url = None
        if has_admin_access(self.request) and self.request.user.has_perm(VIEW_ORDER):
            # Django Admin's own object URL: the only place a PK appears.
            admin_url = reverse("admin:orders_order_change", args=[order.pk])
        context.update({
            "order": order,
            "summary": summary,
            "payment_status_label": presenters.payment_status_label(summary["payment_status"]),
            "payment_status_tone": presenters.PAYMENT_STATUS_TONES.get(
                summary["payment_status"], "muted"
            ),
            "status_tone": presenters.ORDER_STATUS_TONES.get(order.status, "muted"),
            "customer": presenters.customer_name(order),
            "contact": presenters.customer_contact(order),
            "address": presenters.shipping_address(order),
            "items": order.items.select_related("variant__edition__product__brand").order_by("pk"),
            "payments": order.payments.select_related("recorded_by").order_by("payment_date", "id"),
            "returns": order.returns.prefetch_related("refunds").order_by("created_at", "id"),
            "admin_url": admin_url,
        })
        return context
