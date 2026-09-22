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


@admin.register(CustomerProfile)
class CustomerProfileAdmin(ModelAdmin):
    list_display = ("user", "first_name", "last_name", "mobile", "email")
    search_fields = ("user__email", "user__mobile_number", "first_name", "last_name", "mobile", "email")
    autocomplete_fields = ("user",)
