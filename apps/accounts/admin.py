from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from apps.orders.models import Order, Return, ShippingAddress

from .models import CustomerProfile, User

RECENT_LIMIT = 20


def customer_activity(user):
    """Read-only summary of one customer's orders, returns and the shipping
    addresses used on their orders, with links to the full records."""
    if user is None or user.pk is None:
        return "—"
    orders = list(Order.objects.filter(user=user).order_by("-created_at")[:RECENT_LIMIT])
    returns = list(
        Return.objects.filter(order__user=user).select_related("order").order_by("-created_at")[:RECENT_LIMIT]
    )
    addresses = (
        ShippingAddress.objects.filter(order__user=user)
        .values_list("full_name", "mobile", "address_line1", "city", "state", "pincode")
        .distinct()
    )
    all_orders_url = f"{reverse('admin:orders_order_changelist')}?user__id__exact={user.pk}"

    order_rows = format_html_join(
        "", "<tr><td><a href=\"{}\">{}</a></td><td>{}</td><td>{}</td><td>{}</td></tr>",
        (
            (reverse("admin:orders_order_change", args=[o.pk]), o.order_number,
             o.get_status_display(), o.total, o.created_at.date())
            for o in orders
        ),
    )
    return_rows = format_html_join(
        "", "<tr><td><a href=\"{}\">Return #{}</a></td><td>{}</td><td>{}</td></tr>",
        (
            (reverse("admin:orders_return_change", args=[r.pk]), r.pk,
             r.order.order_number, r.get_status_display())
            for r in returns
        ),
    )
    address_rows = format_html_join(
        "", "<li>{}, {}, {}, {}, {} - {}</li>", addresses,
    )
    return format_html(
        '<p><a href="{}">View all orders</a></p>'
        "<h4>Recent orders</h4><table><tbody>{}</tbody></table>"
        "<h4>Returns</h4><table><tbody>{}</tbody></table>"
        "<h4>Shipping addresses used</h4><ul>{}</ul>",
        all_orders_url, order_rows or "—", return_rows or "—", address_rows or "—",
    )

# Unregister to clear any existing 'auth.User' registration
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    list_display = (
        "email", "mobile_number", "role", "full_name", "is_active", "is_staff", "date_joined",
    )
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("email", "mobile_number", "username", "first_name", "last_name")
    ordering = ("-date_joined",)
    readonly_fields = ("public_id", "date_joined", "last_login", "customer_activity")

    fieldsets = (
        (None, {"fields": ("public_id", "username", "email", "mobile_number", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Role & permissions", {
            "fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
        }),
        ("Timestamps", {"fields": ("last_login", "date_joined")}),
        ("Customer activity", {"fields": ("customer_activity",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "mobile_number", "role", "password1", "password2"),
        }),
    )

    # Fields that grant admin or API privileges. Only a superuser may set
    # them; role="admin"/"staff" unlocks the staff APIs (core.permissions).
    PRIVILEGE_FIELDS = ("is_superuser", "is_staff", "groups", "user_permissions", "role")

    def _is_privileged(self, user):
        return user.is_superuser or user.is_staff or user.role in ("staff", "admin")

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += self.PRIVILEGE_FIELDS
        return readonly

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None and not request.user.is_superuser:
            # New users created by non-superusers are always customers.
            return tuple(
                (name, {**opts, "fields": tuple(f for f in opts["fields"] if f != "role")})
                for name, opts in fieldsets
            )
        return fieldsets

    def has_change_permission(self, request, obj=None):
        # A non-superuser may manage customer accounts only. Editing a staff
        # or superuser account (email, mobile, password) would allow taking
        # over that account, so it is refused, including the password page.
        if obj is not None and not request.user.is_superuser and self._is_privileged(obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not request.user.is_superuser and self._is_privileged(obj):
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description="Orders, returns and addresses")
    def customer_activity(self, obj):
        return customer_activity(obj)


@admin.register(CustomerProfile)
class CustomerProfileAdmin(ModelAdmin):
    list_display = ("user", "first_name", "last_name", "mobile", "email")
    search_fields = ("user__email", "user__mobile_number", "first_name", "last_name", "mobile", "email")
    autocomplete_fields = ("user",)
    readonly_fields = ("customer_activity",)

    @admin.display(description="Orders, returns and addresses")
    def customer_activity(self, obj):
        return customer_activity(obj.user if obj else None)
