"""Common list-page interaction for the Smerfume Panel: row actions, quick
view, current-page multi-select and explicitly registered bulk actions.

Presentation plumbing only. Each list decides its actions from existing
rules (apps.purchases.rules, model permissions and statuses the services
also enforce); every write still goes through a domain service, and the
server re-checks permission and lifecycle on every POST.

- ``RowAction``: one action on one row. ``kind`` is ``quick``, ``view``,
  ``edit``, ``delete`` or ``domain``. POST actions carry a confirmation
  text; ``delete`` is always destructive and always confirmed.
- ``BulkAction``: a registered bulk action. ``allowed(user)`` gates the
  action, ``eligible(obj, user)`` gates each row, ``run(obj, user)``
  calls the service. There is no generic bulk delete.
- ``ActionListMixin``: attaches ``obj.ops_key`` and ``obj.ops_actions``
  to each row of the page and puts the bulk bar context in place.
- ``BulkActionView``: POST endpoint running one registered action on the
  selected rows of the current page, reporting each refusal.
"""

from dataclasses import dataclass, field
from typing import Callable

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.ops.mixins import OpsAccessMixin, render_forbidden

MAX_BULK_ROWS = 100


@dataclass(frozen=True)
class RowAction:
    label: str
    url: str
    kind: str = "domain"           # quick | view | edit | delete | domain
    method: str = "get"            # get (link) | post (form)
    confirm: str = ""              # required text for POST actions
    fields: tuple = ()             # extra (name, value) POST fields
    danger: bool = False

    @property
    def is_post(self):
        return self.method == "post"


def quick_view(url):
    """Quick view from the object's own detail page (read-only drawer)."""
    return RowAction("Quick view", url, kind="quick")


def row_quick_view():
    """Quick view built from the row's own cells (lists without a detail page)."""
    return RowAction("Quick view", "", kind="quick")


def delete_action(label, url, confirm, fields=()):
    return RowAction(label, url, kind="delete", method="post", confirm=confirm, fields=fields, danger=True)


@dataclass(frozen=True)
class BulkAction:
    key: str
    label: str
    allowed: Callable
    eligible: Callable
    run: Callable
    errors: tuple = field(default=(Exception,))
    confirm: str = ""
    danger: bool = False
    done: str = "Done"


class ActionListMixin:
    """For list views. Subclasses set ``ops_key_field`` (the public
    identifier used in URLs and bulk POSTs), ``bulk_url_name`` and
    ``bulk_actions``, and implement ``row_actions(obj, user)``."""

    ops_key_field = "public_id"
    bulk_url_name = ""
    bulk_actions = ()

    def row_actions(self, obj, user):
        return []

    def prepare_rows(self, objects, user):
        """Hook: precompute per-page data (one query, not one per row)."""

    def allowed_bulk_actions(self, user):
        return [action for action in self.bulk_actions if action.allowed(user)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        self.prepare_rows(list(context["object_list"]), user)
        for obj in context["object_list"]:
            obj.ops_key = str(getattr(obj, self.ops_key_field))
            obj.ops_actions = self.row_actions(obj, user)
        context.update({
            "ops_bulk_actions": self.allowed_bulk_actions(user),
            "ops_bulk_url": reverse(self.bulk_url_name) if self.bulk_url_name else "",
            "ops_selectable": True,
        })
        return context


class BulkActionView(OpsAccessMixin, View):
    """POST <list>/bulk/ with ``action``, ``keys`` (selected rows on the
    current page) and ``next`` (the list URL with its filters). Runs the
    registered action row by row through its service; rows that are not
    eligible or that the service refuses are reported, never forced."""

    http_method_names = ["post"]
    list_view = None          # the ActionListMixin list view class
    list_view_url_name = ""   # where to return to
    permission_required = ()

    def queryset(self):
        raise NotImplementedError

    def post(self, request):
        list_view = self.list_view
        action = next((a for a in list_view.bulk_actions if a.key == request.POST.get("action")), None)
        if action is None or not action.allowed(request.user):
            return render_forbidden(request)
        keys = [key for key in request.POST.getlist("keys") if key][:MAX_BULK_ROWS]
        objects = list(self.queryset().filter(**{f"{list_view.ops_key_field}__in": keys}))
        done, problems = 0, []
        for obj in objects:
            label = str(getattr(obj, list_view.ops_key_field))
            if not action.eligible(obj, request.user):
                problems.append(f"{label}: not available for this row.")
                continue
            try:
                action.run(obj, request.user)
            except action.errors as exc:
                problems.append(f"{label}: {exc}")
            else:
                done += 1
        if done:
            messages.success(request, f"{action.done}: {done} row(s).")
        for problem in problems:
            messages.warning(request, problem)
        if not objects:
            messages.info(request, "Select at least one row.")
        return redirect(self._back(request))

    def _back(self, request):
        target = request.POST.get("next", "")
        list_url = reverse(self.list_view_url_name)
        if target.startswith(list_url) and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
        ):
            return target
        return list_url
