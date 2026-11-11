from django import forms

from apps.orders import payments as order_payments
from apps.orders.models import Order


def _with_all(choices):
    return [("", "All"), *choices]


class OrderFilterForm(forms.Form):
    """GET filters for the ops order list. Invalid values are reported and
    ignored; only cleaned values reach the query."""

    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    status = forms.ChoiceField(required=False, choices=_with_all(Order.STATUS_CHOICES))
    channel = forms.ChoiceField(required=False, choices=_with_all(Order.CHANNEL_CHOICES))
    payment_status = forms.ChoiceField(
        required=False, choices=_with_all(order_payments.STATUS_CHOICES), label="Payment",
    )
