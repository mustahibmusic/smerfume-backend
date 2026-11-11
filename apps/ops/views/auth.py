"""Login / logout for the Smerfume Panel (Django session auth)."""

import posixpath
from urllib.parse import unquote, urlsplit

from django.contrib import messages
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.urls import reverse, reverse_lazy

from apps.ops.access import OPS_ACCESS_PERMISSION
from apps.ops.mixins import OpsAccessMixin


class OpsAuthenticationForm(AuthenticationForm):
    """Standard authentication, then refuse accounts without /ops/ access.

    Inactive accounts are already refused by the authentication backend
    with the generic invalid-login message.
    """

    error_messages = {
        **AuthenticationForm.error_messages,
        "no_ops_access": "This account does not have access to the Smerfume Panel.",
    }

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.has_perm(OPS_ACCESS_PERMISSION):
            raise ValidationError(self.error_messages["no_ops_access"], code="no_ops_access")


def is_ops_destination(url):
    """True when ``url`` (already same-host checked) is a Smerfume Panel
    page other than login/logout. Dot segments are resolved first, so
    ``/ops/../admin/`` does not count as an /ops/ page."""
    if not url:
        return False
    path = posixpath.normpath(unquote(urlsplit(url).path))
    if not path.endswith("/"):
        path += "/"
    if not path.startswith(reverse("ops:home")):
        return False
    return not path.startswith((reverse("ops:login"), reverse("ops:logout")))


class OpsLoginView(LoginView):
    """GET shows the form; POST signs in.

    Redirect after login: a same-host ``next`` that is an /ops/ page,
    otherwise /ops/. Never Django Admin, for any user (superusers too).
    """

    template_name = "ops/login.html"
    authentication_form = OpsAuthenticationForm
    redirect_authenticated_user = True
    next_page = reverse_lazy("ops:home")

    def get_redirect_url(self):
        url = super().get_redirect_url()  # "" unless same host and safe
        return url if is_ops_destination(url) else ""


class OpsLogoutView(LogoutView):
    """POST only (Django refuses GET with 405). Ends the session."""

    next_page = reverse_lazy("ops:login")


class OpsPasswordChangeView(OpsAccessMixin, PasswordChangeView):
    """GET/POST /ops/profile/password/: change your own password.

    Django's PasswordChangeForm (old password + validated new password,
    AUTH_PASSWORD_VALIDATORS applied); Django keeps the current session
    signed in. Needs core.access_ops only; acts on the signed-in user only.
    """

    template_name = "ops/password_change.html"
    success_url = reverse_lazy("ops:profile")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Your password was changed.")
        return response
