from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User
from unfold.admin import ModelAdmin

# Unregister to clear any existing 'auth.User' registration
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass

@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    # ... your existing list_display and fieldsets ...

    def get_model_perms(self, request):
        """
        This is the "Senior" way to move a model to another section
        without breaking the App Registry.
        """
        perms = super().get_model_perms(request)
        return perms