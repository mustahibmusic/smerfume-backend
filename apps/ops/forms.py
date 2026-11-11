from django import forms
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryStock, StockMovement, StockReservation, Warehouse
from apps.orders import payments as order_payments
from apps.orders.forms import (
    BaseInStoreLineFormSet,
    InStoreSaleForm,
    InStoreSaleLineForm,
    RecordPaymentForm,
)
from apps.orders.models import Order, Refund, Return


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


# ── Sales: presentation variants of the shared orders forms ─────────────────
# Same fields and validation as apps.orders.forms (used by Django Admin);
# only the widgets differ, and the warehouse is chosen by public id so no
# database id reaches the page.


class OpsInStoreSaleForm(InStoreSaleForm):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.order_by("name"), to_field_name="public_id",
        help_text="Stock location the items leave from.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer_mobile"].widget = forms.TextInput(
            attrs={"inputmode": "tel", "autocomplete": "off", "maxlength": 15},
        )
        self.fields["customer_name"].widget = forms.TextInput(attrs={"maxlength": 150})
        self.fields["discount_amount"].widget = forms.NumberInput(attrs={"step": "0.01", "min": "0"})
        self.fields["notes"].widget = forms.Textarea(attrs={"rows": 2})


class OpsInStoreSaleLineForm(InStoreSaleLineForm):
    """A scanner/typing friendly line: the item box takes a SKU, or an exact
    barcode which is turned into its SKU before the shared validation."""

    sku = forms.CharField(
        max_length=64, label="Item",
        widget=forms.TextInput(attrs={
            "autocomplete": "off", "data-sale-item": "",
            "placeholder": "Scan barcode, or type SKU / product name",
        }),
    )
    quantity = forms.IntegerField(
        min_value=1, max_value=1000, widget=forms.NumberInput(attrs={"min": "1", "max": "1000"}),
    )

    def clean_sku(self):
        raw = self.cleaned_data["sku"].strip()
        if raw and not ProductVariant.objects.filter(sku=raw).exists():
            by_barcode = ProductVariant.objects.filter(barcode=raw).only("sku").first()
            if by_barcode is not None:
                self.cleaned_data["sku"] = by_barcode.sku
        return super().clean_sku()


OpsInStoreSaleLineFormSet = forms.formset_factory(
    OpsInStoreSaleLineForm, formset=BaseInStoreLineFormSet, extra=1, max_num=50, can_delete=True,
)


class ReturnFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Order number")
    status = forms.ChoiceField(required=False, choices=_with_all(Return.STATUS_CHOICES))


class RefundFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Order number or reference")
    refund_status = forms.ChoiceField(
        required=False, choices=_with_all(Refund.STATUS_CHOICES), label="Status",
    )
    refund_method = forms.ChoiceField(
        required=False, choices=_with_all(Refund.METHOD_CHOICES), label="Method",
    )


class OpsRecordPaymentForm(RecordPaymentForm):
    """Record Payment with plain widgets (native date picker, no future
    dates offered). Fields and validation are the shared RecordPaymentForm;
    the payment service re-checks everything under the order lock."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate().isoformat()
        self.fields["amount"].widget = forms.NumberInput(attrs={"step": "0.01", "min": "0.01"})
        self.fields["payment_date"].widget = forms.DateInput(attrs={"type": "date", "max": today})
        self.fields["method"].widget = forms.Select(choices=self.fields["method"].choices)
        self.fields["reference"].widget = forms.TextInput(attrs={"maxlength": 100, "autocomplete": "off"})
        self.fields["notes"].widget = forms.Textarea(attrs={"rows": 3})


# ── Inventory filters ───────────────────────────────────────────────────────
# Warehouses are chosen by public id, never by database id.


def _warehouse_filter():
    return forms.ModelChoiceField(
        queryset=Warehouse.objects.order_by("name"), to_field_name="public_id",
        required=False, empty_label="All",
    )


class StockFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    stock_type = forms.ChoiceField(
        required=False, choices=_with_all(InventoryStock.STOCK_TYPE_CHOICES), label="Stock type",
    )
    warehouse = _warehouse_filter()
    low_stock = forms.BooleanField(required=False, label="At or below reorder level")


class PartialLotFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="SKU")
    warehouse = _warehouse_filter()
    state = forms.ChoiceField(
        required=False, label="State",
        choices=[("", "All"), ("open", "Open"), ("depleted", "Depleted")],
    )


class MovementFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="SKU or notes")
    movement_type = forms.ChoiceField(
        required=False, choices=_with_all(StockMovement.MOVEMENT_TYPE_CHOICES), label="Movement",
    )
    reason = forms.ChoiceField(required=False, choices=_with_all(StockMovement.REASON_CHOICES))
    warehouse = _warehouse_filter()


class ReservationFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Order number or SKU")
    status = forms.ChoiceField(
        required=False, label="Status",
        choices=[("", "All"), ("active", "Active (held or confirmed)")] + list(StockReservation.STATUS_CHOICES),
    )
    purpose = forms.ChoiceField(required=False, choices=_with_all(StockReservation.PURPOSE_CHOICES))
    warehouse = _warehouse_filter()
