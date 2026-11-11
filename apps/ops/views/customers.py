"""Customers and Users pages for the Smerfume Panel (read-only).

Customers are user accounts with role "customer" and no admin
privileges. The detail page shows
contact details, order history with paid / balance / payment status from
apps.orders.payments (the same functions Django Admin uses), and links to
the customer's returns and refunds. Each section needs its own orders
permission.

Users lists every account with operational fields only. Password hashes,
OTP records, session tokens and the permission lists are never loaded
into a page. There are no account write services, so nothing here writes;
account changes stay in Django Admin and its own permission checks.
URLs use the user's public_id.
"""

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.views.generic import ListView, TemplateView

from apps.accounts.models import CustomerProfile
from apps.ops import presenters
from apps.ops.access import has_ops_access
from apps.ops.forms import CustomerFilterForm, UserFilterForm
from apps.ops.mixins import FilteredListMixin, OpsAccessMixin
from apps.orders import payments as order_payments
from apps.orders.models import Order, Refund, Return

User = get_user_model()

VIEW_CUSTOMER = "accounts.view_customerprofile"
VIEW_USER = "accounts.view_user"
RECENT_LIMIT = 50

# Only these columns are read for list and detail pages: never password,
# tokens or OTP data.
SAFE_FIELDS = (
    "id", "public_id", "username", "first_name", "last_name", "email", "mobile_number", "role",
    "is_active", "is_staff", "is_superuser", "last_login", "date_joined",
)


def customers_queryset():
    """Customer accounts: role "customer" and no admin privileges (the
    same test Django Admin uses to tell customer accounts from staff)."""
    return User.objects.filter(role="customer", is_staff=False, is_superuser=False).only(*SAFE_FIELDS)


def is_customer(account):
    return account.role == "customer" and not account.is_staff and not account.is_superuser


def _search(queryset, q):
    return queryset.filter(
        Q(email__icontains=q) | Q(mobile_number__icontains=q) | Q(first_name__icontains=q)
        | Q(last_name__icontains=q) | Q(username__icontains=q)
    )


def _state(queryset, state):
    if state == "active":
        return queryset.filter(is_active=True)
    if state == "inactive":
        return queryset.filter(is_active=False)
    return queryset


class CustomerListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/customers/?q=&state= (name, mobile, email)."""

    permission_required = (VIEW_CUSTOMER,)
    nav_active = "customers"
    template_name = "ops/customers/customer_list.html"
    filter_form_class = CustomerFilterForm

    def base_queryset(self):
        return (
            customers_queryset()
            .annotate(order_count=Count("orders"))
            .order_by("-date_joined", "-id")
        )

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = _search(queryset, data["q"])
        return _state(queryset, data.get("state"))


class CustomerDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/customers/<public_id>/: contact, orders, returns, refunds."""

    permission_required = (VIEW_CUSTOMER,)
    nav_active = "customers"
    template_name = "ops/customers/customer_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        customer = get_object_or_404(customers_queryset(), public_id=kwargs["public_id"])
        profile = CustomerProfile.objects.filter(user=customer).first()
        can_view_orders = user.has_perm("orders.view_order")
        can_view_returns = user.has_perm("orders.view_return")
        can_view_refunds = user.has_perm("orders.view_refund")
        orders = []
        if can_view_orders:
            orders = [
                presenters.order_row(order) for order in order_payments.annotate_payment_summary(
                    Order.objects.filter(user=customer)
                ).order_by("-created_at", "-id")[:RECENT_LIMIT]
            ]
        context.update({
            "customer": customer,
            "profile": profile,
            "can_view_orders": can_view_orders,
            "can_view_returns": can_view_returns,
            "can_view_refunds": can_view_refunds,
            "order_count": Order.objects.filter(user=customer).count(),
            "orders": orders,
            "orders_search": customer.mobile_number or customer.email or "",
            "returns": Return.objects.filter(order__user=customer).select_related("order")
            .order_by("-created_at", "-id")[:RECENT_LIMIT] if can_view_returns else [],
            "refunds": Refund.objects.filter(return_request__order__user=customer)
            .select_related("return_request__order").order_by("-created_at", "-id")[:RECENT_LIMIT]
            if can_view_refunds else [],
        })
        return context


class UserListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/users/?q=&role=&state=&staff= (operational fields only)."""

    permission_required = (VIEW_USER,)
    nav_active = "users"
    template_name = "ops/customers/user_list.html"
    filter_form_class = UserFilterForm

    def base_queryset(self):
        return User.objects.only(*SAFE_FIELDS).order_by("-date_joined", "-id")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            queryset = _search(queryset, data["q"])
        if data.get("role"):
            queryset = queryset.filter(role=data["role"])
        if data.get("staff") == "yes":
            queryset = queryset.filter(is_staff=True)
        elif data.get("staff") == "no":
            queryset = queryset.filter(is_staff=False)
        return _state(queryset, data.get("state"))


class UserDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/users/<public_id>/: account status, role, groups, access."""

    permission_required = (VIEW_USER,)
    nav_active = "users"
    template_name = "ops/customers/user_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = get_object_or_404(User.objects.only(*SAFE_FIELDS), public_id=kwargs["public_id"])
        context.update({
            "account": account,
            "groups": list(account.groups.order_by("name").values_list("name", flat=True)),
            "has_ops_access": has_ops_access(account),
            "can_view_customer": is_customer(account) and self.request.user.has_perm(VIEW_CUSTOMER),
        })
        return context
