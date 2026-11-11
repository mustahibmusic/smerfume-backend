"""Login / logout for /ops/ (Django session auth, same User model)."""

from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.urls import reverse_lazy

from apps.ops.access import OPS_ACCESS_PERMISSION


class OpsAuthenticationForm(AuthenticationForm):
    """Standard authentication, then refuse accounts without /ops/ access.

    Inactive accounts are already refused by the authentication backend
    with the generic invalid-login message.
    """

    error_messages = {
        **AuthenticationForm.error_messages,
        "no_ops_access": "This account does not have access to Smerfume Operations.",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.has_perm(OPS_ACCESS_PERMISSION):
            raise ValidationError(self.error_messages["no_ops_access"], code="no_ops_access")


class OpsLoginView(LoginView):
    """GET shows the form; POST signs in and redirects to a safe ``next``
    (same host only, checked by Django) or the ops home."""

    template_name = "ops/login.html"
    authentication_form = OpsAuthenticationForm
    redirect_authenticated_user = True
    next_page = reverse_lazy("ops:home")


class OpsLogoutView(LogoutView):
    """POST only (Django refuses GET with 405). Ends the session."""

    next_page = reverse_lazy("ops:login")
