from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.ops.mixins import OpsAccessMixin
from apps.ops.navigation import nav_for


class OpsHomeView(OpsAccessMixin, TemplateView):
    """GET /ops/: redirect to the first section the user may open.

    No dashboard yet. A user with /ops/ access but no visible section
    sees an empty-state page.
    """

    template_name = "ops/home.html"

    def get(self, request, *args, **kwargs):
        items = nav_for(request.user)
        if items:
            return redirect(items[0]["url"])
        return super().get(request, *args, **kwargs)


class OpsNotFoundView(OpsAccessMixin, TemplateView):
    """Catch-all for unknown /ops/ paths: branded 404 (after the access gate)."""

    template_name = "ops/404.html"

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context, status=404)
