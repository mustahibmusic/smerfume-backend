"""Record Payment (DEC-013) for the Smerfume Panel.

Same workflow as Django Admin's Record payment: offered only for orders in
apps.orders.payments.orders_open_for_manual_payment() (in-store, not
cancelled, balance due > 0) and to users with orders.add_orderpayment; the
one write path is apps.orders.payments.record_order_payment(), which
re-checks amount, balance, date and cancellation under the order lock and
refreshes the legacy payment summary.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from apps.ops import presenters
from apps.ops.forms import OpsRecordPaymentForm
from apps.ops.mixins import OpsAccessMixin, render_forbidden
from apps.orders import payments as order_payments
from apps.orders.forms import stale_payment_form
from apps.orders.models import Order

ADD_PAYMENT = "orders.add_orderpayment"


def can_record_payment(order, user):
    """Display and access rule, the same as Django Admin's."""
    return user.has_perm(ADD_PAYMENT) and order_payments.orders_open_for_manual_payment(
        Order.objects.filter(pk=order.pk)
    ).exists()


class OrderRecordPaymentView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/orders/<order_number>/record-payment/.

    GET changes nothing. A valid POST records one payment and returns to
    the order page. A page opened before another payment was saved (or a
    double submit) is sent back with the current balance, recording
    nothing."""

    permission_required = ("orders.view_order", ADD_PAYMENT)
    nav_active = "orders"
    template_name = "ops/orders/record_payment.html"

    def _load(self, order_number):
        order = get_object_or_404(Order.objects.select_related("user"), order_number=order_number)
        if not can_record_payment(order, self.request.user):
            return order, render_forbidden(self.request)
        return order, None

    def _detail_url(self, order):
        return reverse("ops:order_detail", args=[order.order_number])

    def get(self, request, order_number):
        order, refused = self._load(order_number)
        if refused:
            return refused
        summary = order_payments.payment_summary(order)
        form = OpsRecordPaymentForm(initial={
            "amount": summary["balance_due"],
            "expected_balance": summary["balance_due"],
            "payment_date": timezone.localdate(),
        })
        return self.render_to_response(self.get_context_data(order=order, summary=summary, form=form))

    def post(self, request, order_number):
        order, refused = self._load(order_number)
        if refused:
            return refused
        summary = order_payments.payment_summary(order)
        form = OpsRecordPaymentForm(request.POST)
        if form.is_valid():
            stale = stale_payment_form(form, request.POST, summary["balance_due"])
            if stale is not None:
                form = stale
            else:
                data = form.cleaned_data
                try:
                    payment = order_payments.record_order_payment(
                        order, method=data["method"], amount=data["amount"],
                        reference=data["reference"], payment_date=data["payment_date"],
                        notes=data["notes"], recorded_by=request.user,
                    )
                except order_payments.OrderPaymentError as exc:
                    form.add_error(None, str(exc))
                else:
                    balance = order_payments.payment_summary(order)["balance_due"]
                    messages.success(
                        request, f"{payment.payment_number} recorded: ₹{payment.amount}. Balance due ₹{balance}.",
                    )
                    return redirect(self._detail_url(order))
        return self.render_to_response(self.get_context_data(order=order, summary=summary, form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status = kwargs["summary"]["payment_status"]
        context.update({
            "payment_status_label": presenters.payment_status_label(status),
            "payment_status_tone": presenters.PAYMENT_STATUS_TONES.get(status, "muted"),
        })
        return context
