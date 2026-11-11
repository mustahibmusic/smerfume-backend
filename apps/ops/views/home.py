from django.views.generic import TemplateView

from apps.ops.mixins import OpsAccessMixin
from apps.ops.navigation import quick_links


class OpsHomeView(OpsAccessMixin, TemplateView):
    """GET /ops/: Smerfume Panel home (needs core.access_ops only).

    Welcome line and quick-access cards for implemented pages the user may
    open. No analytics and no domain queries.
    """

    template_name = "ops/home.html"
    nav_active = "home"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["quick_links"] = quick_links(self.request.user)
        return context


class OpsNotFoundView(OpsAccessMixin, TemplateView):
    """Catch-all for unknown /ops/ paths: branded 404 (after the access gate)."""

    template_name = "ops/404.html"

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context, status=404)


class OpsProfileView(OpsAccessMixin, TemplateView):
    """GET /ops/profile/: the signed-in user's own account, read-only.

    Needs core.access_ops only and shows nobody else's data. Account
    changes (name, password) stay in Django Admin; this page only adds the
    per-browser theme choice.
    """

    template_name = "ops/profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["groups"] = list(self.request.user.groups.order_by("name").values_list("name", flat=True))
        return context
