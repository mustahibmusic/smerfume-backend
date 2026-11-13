from django import forms
from django.utils import timezone

from apps.accounts import countries
from apps.accounts.mobile import InvalidMobileNumber, combine_mobile
from apps.accounts.models import User
from apps.catalog.models import ProductVariant
from apps.inventory.models import InventoryStock, StockMovement, StockReservation, Supplier, Warehouse
from apps.orders import payments as order_payments
from apps.orders.forms import (
    BaseInStoreLineFormSet,
    InStoreSaleForm,
    InStoreSaleLineForm,
    RecordPaymentForm,
)
from apps.orders.models import Order, Refund, Return
from apps.purchases.models import (
    PAYMENT_STATUS_CHOICES,
    GoodsReceipt,
    PaymentAccount,
    PurchaseOrder,
    PurchaseOrderPayment,
    ReceiptDiscrepancy,
)


def _with_all(choices):
    return [("", "All"), *choices]


# Order list payment filter for "unpaid or partially paid" (DEC-019).
PAYMENT_OUTSTANDING = "outstanding"
# PO list payment filter: payable POs with money still to pay (DEC-019),
# same set as the Home "Vendor outstanding" card.
PO_PAYMENT_OUTSTANDING = "outstanding"


class OrderFilterForm(forms.Form):
    """GET filters for the ops order list. Invalid values are reported and
    ignored; only cleaned values reach the query."""

    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    status = forms.ChoiceField(required=False, choices=_with_all(Order.STATUS_CHOICES))
    channel = forms.ChoiceField(required=False, choices=_with_all(Order.CHANNEL_CHOICES))
    payment_status = forms.ChoiceField(
        required=False, label="Payment",
        choices=_with_all([(PAYMENT_OUTSTANDING, "Outstanding (unpaid or partially paid)"),
                           *order_payments.STATUS_CHOICES]),
    )
    date_from = forms.DateField(
        required=False, label="From", widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False, label="To", widget=forms.DateInput(attrs={"type": "date"}),
    )
    cod_unverified = forms.BooleanField(required=False, label="Awaiting COD verification")
    exclude_cancelled = forms.BooleanField(required=False, label="Exclude cancelled")

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end date is before the start date.")
        return cleaned


# ── Sales: presentation variants of the shared orders forms ─────────────────
# Same fields and validation as apps.orders.forms (used by Django Admin);
# only the widgets differ, and the warehouse is chosen by public id so no
# database id reaches the page.


class OpsInStoreSaleForm(InStoreSaleForm):
    """The mobile is entered as a country (dial code) and a national number;
    clean() combines them into the one canonical stored number before the
    shared sale path sees it."""

    customer_country = forms.ChoiceField(
        choices=(), initial=countries.DEFAULT_COUNTRY, required=False, label="Country code",
    )
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.order_by("name"), to_field_name="public_id",
        help_text="Stock location the items leave from.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer_country"].choices = countries.country_choices()
        self.fields["customer_mobile"].label = "Mobile number"
        self.fields["customer_mobile"].help_text = "Without the country code. Finds the customer, or creates one."
        self.fields["customer_mobile"].widget = forms.TextInput(
            attrs={"inputmode": "tel", "autocomplete": "off", "maxlength": 15},
        )
        self.fields["customer_name"].help_text = "Search existing customers, or type a new customer's name."
        self.fields["customer_name"].widget = forms.TextInput(attrs={"maxlength": 150})
        self.fields["discount_amount"].widget = forms.NumberInput(attrs={"step": "0.01", "min": "0"})
        self.fields["notes"].widget = forms.Textarea(attrs={"rows": 2})

    def clean_customer_mobile(self):
        # Combined with the country in clean(); the shared Indian default
        # must not be applied to a national number of another country.
        return self.cleaned_data["customer_mobile"].strip()

    def clean(self):
        cleaned = super().clean()
        country = cleaned.get("customer_country") or countries.DEFAULT_COUNTRY
        national = cleaned.get("customer_mobile")
        if national:
            try:
                cleaned["customer_mobile"] = combine_mobile(countries.dial_code_for(country), national)
            except InvalidMobileNumber as exc:
                self.add_error("customer_mobile", str(exc))
        return cleaned


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
    # Shown and applied only to users who may view purchase orders.
    no_incoming = forms.BooleanField(required=False, label="No booked incoming")


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
    past_warning_age = forms.BooleanField(required=False, label="Active, past dashboard warning age")


# ── Purchases filters ───────────────────────────────────────────────────────
# Vendors and accounts are chosen by business code / public id.


class VendorFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Search")
    state = forms.ChoiceField(
        required=False, label="State", choices=[("", "All"), ("active", "Active"), ("inactive", "Inactive")],
    )


class PurchaseOrderFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="PO, vendor or SKU")
    status = forms.ChoiceField(required=False, choices=_with_all(PurchaseOrder.STATUS_CHOICES))
    vendor = forms.ModelChoiceField(
        queryset=Supplier.objects.order_by("name"), to_field_name="vendor_code", required=False,
        empty_label="All",
    )
    payment_status = forms.ChoiceField(
        required=False, label="Payment",
        choices=_with_all([(PO_PAYMENT_OUTSTANDING, "Outstanding (payable, money to pay)"),
                           *PAYMENT_STATUS_CHOICES]),
    )
    overdue = forms.BooleanField(required=False, label="Overdue only")


class GoodsReceiptFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="GRN, PO or vendor")
    status = forms.ChoiceField(required=False, choices=_with_all(GoodsReceipt.STATUS_CHOICES))
    receipt_type = forms.ChoiceField(
        required=False, choices=_with_all(GoodsReceipt.TYPE_CHOICES), label="Type",
    )


class PaymentLedgerFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Payment #, PO, vendor or reference")
    status = forms.ChoiceField(required=False, choices=_with_all(PurchaseOrderPayment.STATUS_CHOICES))
    entry_type = forms.ChoiceField(
        required=False, choices=_with_all(PurchaseOrderPayment.TYPE_CHOICES), label="Entry",
    )
    method = forms.ChoiceField(
        required=False, choices=_with_all(PurchaseOrderPayment.METHOD_CHOICES), label="Mode",
    )
    paid_through = forms.ModelChoiceField(
        queryset=PaymentAccount.objects.order_by("name"), to_field_name="public_id", required=False,
        empty_label="All", label="Paid through",
    )


class DiscrepancyFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="GRN, PO or SKU")
    status = forms.ChoiceField(required=False, choices=_with_all(ReceiptDiscrepancy.STATUS_CHOICES))
    discrepancy_type = forms.ChoiceField(
        required=False, choices=_with_all(ReceiptDiscrepancy.TYPE_CHOICES), label="Type",
    )


class BookedChangeFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="PO, SKU or note")


# ── Customers and users ──────────────────────────────────────────────────────

ACCOUNT_STATE_CHOICES = [("", "All"), ("active", "Active"), ("inactive", "Inactive")]


class CustomerFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Name, mobile or email")
    state = forms.ChoiceField(required=False, choices=ACCOUNT_STATE_CHOICES)


class UserFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True, label="Name, mobile, email or username")
    role = forms.ChoiceField(required=False, choices=_with_all(User.ROLE_CHOICES))
    staff = forms.ChoiceField(
        required=False, label="Admin staff", choices=[("", "All"), ("yes", "Staff"), ("no", "Not staff")],
    )
    state = forms.ChoiceField(required=False, choices=ACCOUNT_STATE_CHOICES)
