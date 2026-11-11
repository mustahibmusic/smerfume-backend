import logging

from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse

from apps.ops.access import has_admin_access, has_ops_access
from apps.ops.navigation import nav_for

logger = logging.getLogger(__name__)


def ops_context(request, active=""):
    """Shared layout context: sidebar items, and the Django Admin switch
    (None unless the user may open Django Admin)."""
    return {
        "ops_nav": nav_for(request.user, active),
        "admin_switch_url": reverse("admin:index") if has_admin_access(request) else None,
    }


def render_forbidden(request):
    return render(request, "ops/403.html", ops_context(request), status=403)


def render_not_found(request):
    return render(request, "ops/404.html", ops_context(request), status=404)


class OpsAccessMixin:
    """Gate for every /ops/ page.

    Anonymous: redirect to the ops login. Authenticated without
    ``core.access_ops`` or without the view's ``permission_required``:
    branded 403. Http404 raised by the view: branded 404.
    """

    permission_required = ()
    nav_active = ""

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("ops:login"))
        if not has_ops_access(user) or not user.has_perms(self.permission_required):
            logger.info("ops access denied: user=%s path=%s", user.pk, request.path)
            return render_forbidden(request)
        try:
            return super().dispatch(request, *args, **kwargs)
        except Http404:
            return render_not_found(request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(ops_context(self.request, self.nav_active))
        return context
