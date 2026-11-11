"""Returns for the Smerfume Panel.

Same workflow and permissions as Django Admin's return pages. Every write
is an apps.orders.services call (create_return, the status transitions,
record_return_received_quantities, finalize_return_item,
create_refund_for_return); this module only renders and routes.
Returns, return items and orders are addressed by public id / order
number, never by database id.
"""

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.ops.forms import ReturnFilterForm
from apps.ops.mixins import OpsAccessMixin, render_forbidden
from apps.orders import services as order_services
from apps.orders.forms import ReturnItemInspectionForm, ReturnReceivedQuantitiesForm, StaffReturnForm
from apps.orders.models import Order, Return, ReturnItem

VIEW_RETURN = "orders.view_return"
PROCESS_RETURN = "orders.process_return"
RECEIVE_RETURN = "orders.receive_return"
PROCESS_REFUND = "orders.process_refund"
CHANGE_RETURN_ITEM = "orders.change_returnitem"

# action key -> (service, error class, permission, statuses it applies to,
# button label, success message, confirmation text or "")
RETURN_ACTIONS = {
    "approve": (
        order_services.approve_return, order_services.ReturnTransitionError, PROCESS_RETURN,
        (Return.STATUS_REQUESTED,), "Approve", "Return approved.", "",
    ),
    "reject": (
        order_services.reject_return, order_services.ReturnTransitionError, PROCESS_RETURN,
        (Return.STATUS_REQUESTED,), "Reject", "Return rejected.",
        "Reject this return request? The customer's return is closed.",
    ),
    "cancel": (
        order_services.cancel_return, order_services.ReturnTransitionError, PROCESS_RETURN,
        (Return.STATUS_REQUESTED, Return.STATUS_APPROVED, Return.STATUS_IN_TRANSIT),
        "Cancel return", "Return cancelled.", "Cancel this return?",
    ),
    "in_transit": (
        order_services.mark_return_in_transit, order_services.ReturnTransitionError, RECEIVE_RETURN,
        (Return.STATUS_APPROVED,), "Mark in transit", "Return marked in transit.", "",
    ),
    "received": (
        order_services.mark_return_received, order_services.ReturnTransitionError, RECEIVE_RETURN,
        (Return.STATUS_IN_TRANSIT,), "Mark received", "Return marked received.", "",
    ),
    "start_inspection": (
        order_services.start_inspection, order_services.ReturnTransitionError, RECEIVE_RETURN,
        (Return.STATUS_RECEIVED,), "Start inspection", "Inspection started.", "",
    ),
    "create_refund": (
        None, order_services.RefundError, PROCESS_REFUND,
        (Return.STATUS_COMPLETED,), "Create refund",
        "Refund created. Choose its refund method before marking it processing.", "",
    ),
}


def _return_or_404(public_id):
    return get_object_or_404(Return.objects.select_related("order"), public_id=public_id)


def _detail_url(return_request):
    return reverse("ops:return_detail", args=[return_request.public_id])


class ReturnListView(OpsAccessMixin, ListView):
    """GET /ops/returns/?q=&status=&page= (newest first, 25 per page)."""

    permission_required = (VIEW_RETURN,)
    nav_active = "returns"
    template_name = "ops/returns/list.html"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = ReturnFilterForm(self.request.GET or None)
        queryset = (
            Return.objects.select_related("order")
            .annotate(line_count=Count("items", distinct=True))
            .order_by("-created_at", "-id")
        )
        if self.filter_form.is_bound:
            self.filter_form.is_valid()
            data = self.filter_form.cleaned_data
            if data.get("q"):
                queryset = queryset.filter(order__order_number__icontains=data["q"])
            if data.get("status"):
                queryset = queryset.filter(status=data["status"])
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        context.update({
            "filter_form": self.filter_form,
            "query_string": params.urlencode(),
            "is_filtered": any(value for value in params.values()),
        })
        return context


class ReturnDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/returns/<public_id>/: lines, refunds and the actions this
    user may run in the current status. Buttons are display only; each
    action re-checks permission and the service re-checks status."""

    permission_required = (VIEW_RETURN,)
    nav_active = "returns"
    template_name = "ops/returns/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return_request = _return_or_404(kwargs["public_id"])
        user = self.request.user
        items = list(
            return_request.items.select_related("order_item__variant__edition__product__brand")
            .order_by("created_at", "id")
        )
        actions = [
            {"key": key, "label": spec[4], "confirm": spec[6]}
            for key, spec in RETURN_ACTIONS.items()
            if return_request.status in spec[3] and user.has_perm(spec[2])
        ]
        can_record_received = (
            return_request.status in order_services.RECEIVED_QUANTITY_EDITABLE_STATUSES
            and user.has_perm(RECEIVE_RETURN) and user.has_perm(CHANGE_RETURN_ITEM)
        )
        can_inspect = (
            return_request.status == Return.STATUS_INSPECTION_PENDING and user.has_perm(CHANGE_RETURN_ITEM)
        )
        context.update({
            "return": return_request,
            "items": items,
            "refunds": return_request.refunds.order_by("created_at", "id"),
            "actions": actions,
            "received_form": (
                ReturnReceivedQuantitiesForm(return_items=items) if can_record_received else None
            ),
            "can_inspect": can_inspect,
            "can_view_refunds": user.has_perm("orders.view_refund"),
        })
        return context


class ReturnActionView(OpsAccessMixin, View):
    """POST /ops/returns/<public_id>/<action>/: one status action through
    its service. Needs the action's own permission (as in admin)."""

    permission_required = (VIEW_RETURN,)
    http_method_names = ["post"]

    def post(self, request, public_id, action):
        spec = RETURN_ACTIONS.get(action)
        return_request = _return_or_404(public_id)
        if spec is None:
            return redirect(_detail_url(return_request))
        service, error_cls, permission, _statuses, _label, success, _confirm = spec
        if not request.user.has_perm(permission):
            return render_forbidden(request)
        try:
            if action == "create_refund":
                # Method left unset, as in admin: staff choose it explicitly.
                refund = order_services.create_refund_for_return(return_request, approved_by=request.user)
            else:
                service(return_request)
        except error_cls as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, success)
            if action == "create_refund" and request.user.has_perm("orders.view_refund"):
                return redirect(reverse("ops:refund_detail", args=[refund.public_id]))
        return redirect(_detail_url(return_request))


class ReturnReceivedQuantitiesView(OpsAccessMixin, View):
    """POST /ops/returns/<public_id>/received-quantities/ through
    services.record_return_received_quantities()."""

    permission_required = (VIEW_RETURN, RECEIVE_RETURN, CHANGE_RETURN_ITEM)
    http_method_names = ["post"]

    def post(self, request, public_id):
        return_request = _return_or_404(public_id)
        items = list(return_request.items.select_related("order_item__variant__edition__product__brand"))
        form = ReturnReceivedQuantitiesForm(request.POST, return_items=items)
        if not form.is_valid():
            messages.error(request, "Enter a whole number of zero or more for every line.")
        else:
            try:
                order_services.record_return_received_quantities(return_request, form.quantities())
            except order_services.ReturnTransitionError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Received quantities saved.")
        return redirect(_detail_url(return_request))


class ReturnItemInspectView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/returns/<public_id>/items/<item_public_id>/inspect/:
    record inspection through finalize_return_item(), which moves stock and
    finalizes the line atomically. The form is never saved directly."""

    permission_required = (VIEW_RETURN, CHANGE_RETURN_ITEM)
    nav_active = "returns"
    template_name = "ops/returns/inspect.html"

    def _item(self, public_id, item_public_id):
        return get_object_or_404(
            ReturnItem.objects.select_related(
                "return_request__order", "order_item__variant__edition__product__brand",
            ),
            public_id=item_public_id, return_request__public_id=public_id,
        )

    def get(self, request, public_id, item_public_id):
        item = self._item(public_id, item_public_id)
        form = ReturnItemInspectionForm(instance=item)
        return self.render_to_response(self.get_context_data(item=item, form=form))

    def post(self, request, public_id, item_public_id):
        item = self._item(public_id, item_public_id)
        form = ReturnItemInspectionForm(request.POST, instance=ReturnItem.objects.get(pk=item.pk))
        if form.is_valid():
            data = form.cleaned_data
            try:
                order_services.finalize_return_item(
                    item,
                    received_quantity=data["received_quantity"],
                    disposition=data["disposition"],
                    resolution=data["resolution"],
                    remaining_quantity_ml=data.get("remaining_quantity_ml"),
                    inspection_notes=data.get("inspection_notes", ""),
                    inspected_by=request.user,
                )
            except order_services.ReturnInspectionError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Return item inspected and finalized.")
                return redirect(_detail_url(item.return_request))
        return self.render_to_response(self.get_context_data(item=item, form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["return"] = kwargs["item"].return_request
        return context


class OrderCreateReturnView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/orders/<order_number>/return/: staff-created return
    (phone/WhatsApp) through create_return(), so the customer rules apply
    exactly (delivered, within the window, up to the quantity bought)."""

    permission_required = ("orders.view_order", PROCESS_RETURN)
    nav_active = "orders"
    template_name = "ops/returns/create.html"

    def _order(self, order_number):
        return get_object_or_404(Order, order_number=order_number)

    def _form(self, order, data=None):
        order_items = order.items.select_related("variant__edition__product__brand").order_by("pk")
        return StaffReturnForm(data, order_items=order_items)

    def get(self, request, order_number):
        order = self._order(order_number)
        return self.render_to_response(self.get_context_data(order=order, form=self._form(order)))

    def post(self, request, order_number):
        order = self._order(order_number)
        form = self._form(order, request.POST)
        if form.is_valid():
            try:
                return_request = order_services.create_return(
                    order, form.cleaned_data["items"], requested_by=request.user,
                )
            except order_services.ReturnEligibilityError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Return created (requested).")
                if request.user.has_perm(VIEW_RETURN):
                    return redirect(_detail_url(return_request))
                return redirect(reverse("ops:order_detail", args=[order.order_number]))
        return self.render_to_response(self.get_context_data(order=order, form=form))


def order_can_create_return(order, user):
    """Display rule for the order page's "Create return" button, as in
    admin: delivered online orders, process_return. create_return() still
    applies every eligibility rule on submit."""
    return (
        user.has_perm(PROCESS_RETURN)
        and order.status == Order.STATUS_DELIVERED
        and order.channel != Order.CHANNEL_IN_STORE
    )

