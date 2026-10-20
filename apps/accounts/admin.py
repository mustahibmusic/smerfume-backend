from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.admin import ModelAdmin

from .models import CustomerProfile, User

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
    readonly_fields = ("public_id", "date_joined", "last_login")

    fieldsets = (
        (None, {"fields": ("public_id", "username", "email", "mobile_number", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Role & permissions", {
            "fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
        }),
        ("Timestamps", {"fields": ("last_login", "date_joined")}),
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


@admin.register(CustomerProfile)
class CustomerProfileAdmin(ModelAdmin):
    list_display = ("user", "first_name", "last_name", "mobile", "email")
    search_fields = ("user__email", "user__mobile_number", "first_name", "last_name", "mobile", "email")
    autocomplete_fields = ("user",)
