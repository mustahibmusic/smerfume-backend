"""Refunds for the Smerfume Panel.

Same workflow and permissions as Django Admin's refund pages. Every write
is an apps.orders.services call (set_refund_method,
mark_refund_processing, complete_refund, fail_refund,
create_refund_adjustment). Refunds are addressed by public id.
"""

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.ops.forms import RefundFilterForm
from apps.ops.mixins import OpsAccessMixin
from apps.orders import services as order_services
from apps.orders.forms import (
    CompleteRefundForm,
    FailRefundForm,
    RefundAdjustmentEntryForm,
    RefundMethodForm,
)
from apps.orders.models import Refund

VIEW_REFUND = "orders.view_refund"
PROCESS_REFUND = "orders.process_refund"
COMPLETE_REFUND = "orders.complete_refund"
ADD_ADJUSTMENT = "orders.add_refundadjustment"


def _refund_or_404(public_id):
    return get_object_or_404(
        Refund.objects.select_related("return_request__order", "processed_by", "approved_by"),
        public_id=public_id,
    )


def _detail_url(refund):
    return reverse("ops:refund_detail", args=[refund.public_id])


class RefundListView(OpsAccessMixin, ListView):
    """GET /ops/refunds/?q=&refund_status=&refund_method=&page=."""

    permission_required = (VIEW_REFUND,)
    nav_active = "refunds"
    template_name = "ops/refunds/list.html"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = RefundFilterForm(self.request.GET or None)
        queryset = Refund.objects.select_related("return_request__order").order_by("-created_at", "-id")
        if self.filter_form.is_bound:
            self.filter_form.is_valid()
            data = self.filter_form.cleaned_data
            if data.get("q"):
                queryset = queryset.filter(
                    Q(return_request__order__order_number__icontains=data["q"])
                    | Q(refund_reference__icontains=data["q"])
                )
            for field in ("refund_status", "refund_method"):
                if data.get(field):
                    queryset = queryset.filter(**{field: data[field]})
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


class RefundDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/refunds/<public_id>/ with the forms this user may use in
    the refund's current status (display only; each POST re-checks)."""

    permission_required = (VIEW_REFUND,)
    nav_active = "refunds"
    template_name = "ops/refunds/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        refund = _refund_or_404(kwargs["public_id"])
        user = self.request.user
        pending = refund.refund_status == Refund.STATUS_PENDING
        processing = refund.refund_status == Refund.STATUS_PROCESSING
        completed = refund.refund_status == Refund.STATUS_COMPLETED
        can_process = pending and user.has_perm(PROCESS_REFUND)
        can_complete = processing and user.has_perm(COMPLETE_REFUND)
        context.update({
            "refund": refund,
            "order": refund.return_request.order,
            "adjustments": refund.adjustments.select_related("approved_by").order_by("created_at", "id"),
            "method_form": RefundMethodForm(initial={"refund_method": refund.refund_method})
            if can_process else None,
            "can_mark_processing": can_process,
            "complete_form": CompleteRefundForm() if can_complete else None,
            "fail_form": FailRefundForm() if can_complete else None,
            "adjustment_form": RefundAdjustmentEntryForm()
            if completed and user.has_perm(ADD_ADJUSTMENT) else None,
            "can_view_return": user.has_perm("orders.view_return"),
        })
        return context


class _RefundPostView(OpsAccessMixin, View):
    """POST-only refund action: validate the form, call one service,
    report the outcome as a message and return to the refund page."""

    http_method_names = ["post"]
    form_class = None

    def post(self, request, public_id):
        refund = _refund_or_404(public_id)
        form = self.form_class(request.POST) if self.form_class else None
        if form is not None and not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect(_detail_url(refund))
        try:
            message = self.run(request, refund, form.cleaned_data if form else {})
        except order_services.RefundError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, message)
        return redirect(_detail_url(refund))


class RefundMethodView(_RefundPostView):
    permission_required = (VIEW_REFUND, PROCESS_REFUND)
    form_class = RefundMethodForm

    def run(self, request, refund, data):
        order_services.set_refund_method(refund, data["refund_method"])
        return "Refund method saved."


class RefundProcessingView(_RefundPostView):
    permission_required = (VIEW_REFUND, PROCESS_REFUND)

    def run(self, request, refund, data):
        order_services.mark_refund_processing(refund)
        return "Refund marked processing. The refund method can no longer be changed."


class RefundCompleteView(_RefundPostView):
    permission_required = (VIEW_REFUND, COMPLETE_REFUND)
    form_class = CompleteRefundForm

    def run(self, request, refund, data):
        order_services.complete_refund(
            refund, refund_reference=data["refund_reference"], processed_by=request.user,
            notes=data["notes"],
        )
        return "Refund marked completed."


class RefundFailView(_RefundPostView):
    permission_required = (VIEW_REFUND, COMPLETE_REFUND)
    form_class = FailRefundForm

    def run(self, request, refund, data):
        order_services.fail_refund(refund, failure_reason=data["failure_reason"], processed_by=request.user)
        return "Refund marked failed."


class RefundAdjustmentView(_RefundPostView):
    permission_required = (VIEW_REFUND, ADD_ADJUSTMENT)
    form_class = RefundAdjustmentEntryForm

    def run(self, request, refund, data):
        order_services.create_refund_adjustment(
            refund, data["adjustment_amount"], data["reason"], approved_by=request.user,
        )
        return "Refund adjustment recorded."
