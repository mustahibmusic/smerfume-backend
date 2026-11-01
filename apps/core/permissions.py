from rest_framework.permissions import BasePermission, IsAuthenticated


class ApiDocsPermission(BasePermission):
    """OpenAPI schema, Swagger and ReDoc: open in local development
    (settings.API_DOCS_PUBLIC), otherwise active staff (Django is_staff)."""

    def has_permission(self, request, view):
        from django.conf import settings

        if settings.API_DOCS_PUBLIC:
            return True
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_staff)


class IsCustomer(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == "customer"


class IsStaffMember(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role in ("staff", "admin")


class IsAdminUser(IsAuthenticated):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == "admin"
