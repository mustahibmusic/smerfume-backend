"""Staff forms for orders workflows, shared by Django Admin and the
Smerfume Panel (/ops/). Presentation and input validation only: every
business rule stays in apps.orders.services / apps.orders.payments, which
re-check with the rows locked."""

from django import forms
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import serializers as drf_serializers
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminSingleDateWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

from apps.catalog.models import ProductVariant
from apps.inventory.models import Warehouse
from apps.inventory.selectors import in_store_available

from . import payments as order_payments
from .models import OrderPayment, Refund, RefundAdjustment, ReturnItem
from .serializers import NOTE_MAX_LENGTH, validate_mobile_number


# ── In-store sale form (DEC-008) ─────────────────────────────────────────────

ITEM_SEARCH_LIMIT = 20


def counter_item_rows(variants, warehouse, query=""):
    """JSON rows for the in-store item picker: name, SKU, barcode, physical
    stock in `warehouse` and the current selling price. The SKU is the row
    id the sale form submits; database ids are never exposed."""
    variants = list(variants)
    stock = in_store_available([v.pk for v in variants], warehouse)
    wanted = query.strip().lower()
    return [
        {
            "id": v.sku, "text": str(v), "sku": v.sku, "barcode": v.barcode,
            "stock": stock[v.pk], "price": str(v.selling_price),
            "exact": bool(wanted) and wanted in (v.sku.lower(), (v.barcode or "").lower()),
        }
        for v in variants
    ]


def search_counter_items(query, limit=ITEM_SEARCH_LIMIT):
    """Active variants matching every word of `query` against brand,
    product, edition, SKU or barcode. Exact barcode first, then exact SKU,
    then the rest by name. Returns up to limit + 1 (the extra one only
    tells the caller there are more)."""
    terms = query.split()[:5]
    if not terms:
        return []
    variants = ProductVariant.objects.filter(is_active=True).select_related("edition__product__brand")
    for term in terms:
        variants = variants.filter(
            Q(edition__product__name__icontains=term) | Q(edition__product__brand__name__icontains=term)
            | Q(edition__name__icontains=term) | Q(sku__icontains=term) | Q(barcode__icontains=term)
        )
    whole = query.strip()
    rank = Case(
        When(barcode__iexact=whole, then=Value(0)), When(sku__iexact=whole, then=Value(1)),
        default=Value(2), output_field=IntegerField(),
    )
    return list(variants.annotate(rank=rank).order_by(
        "rank", "edition__product__brand__name", "edition__product__name", "edition__name", "size_ml", "sku",
    )[:limit + 1])


class InStoreSaleForm(forms.Form):
    customer_mobile = forms.CharField(
        max_length=15, widget=UnfoldAdminTextInputWidget,
        help_text="Required. Finds the customer, or creates one if new.",
    )
    customer_name = forms.CharField(
        max_length=150, required=False, widget=UnfoldAdminTextInputWidget,
        help_text="Only used when a new customer is created.",
    )
    # One key per opened form: a double-click or resubmit of the same page
    # returns the order already recorded instead of creating another.
    submission_key = forms.UUIDField(
        widget=forms.HiddenInput,
        error_messages={"required": "This page is out of date. Reload it and enter the sale again."},
    )
    discount_amount = forms.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, initial=0, required=False,
        widget=UnfoldAdminDecimalFieldWidget,
        help_text="Order-level discount, spread across lines. Cannot exceed the subtotal.",
    )
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.order_by("name"), widget=UnfoldAdminSelectWidget,
        help_text="Stock location the items leave from.",
    )
    notes = forms.CharField(
        required=False, max_length=NOTE_MAX_LENGTH, widget=UnfoldAdminTextareaWidget(attrs={"rows": 2}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].initial = Warehouse.objects.filter(is_default=True).first()

    def clean_customer_mobile(self):
        try:
            return validate_mobile_number(self.cleaned_data["customer_mobile"])
        except drf_serializers.ValidationError as exc:
            raise forms.ValidationError([str(e) for e in exc.detail]) from exc



class InStoreSaleLineForm(forms.Form):
    # A searchable select on the page (in-store-sale/items/); it submits the
    # SKU. Its only option is the chosen item (see attach_item_labels).
    sku = forms.CharField(
        max_length=64, widget=forms.Select(attrs={"class": "counter-item", "data-theme": "admin-autocomplete"}),
    )
    # No initial value: blank extra rows must stay "unchanged" so the
    # formset skips them instead of reporting required-field errors.
    quantity = forms.IntegerField(
        min_value=1, max_value=1000, widget=UnfoldAdminIntegerFieldWidget,
    )

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        variant = ProductVariant.objects.filter(sku=sku).first()
        if variant is None:
            raise forms.ValidationError(f"Unknown SKU '{sku}'.")
        self.cleaned_data["variant"] = variant
        return sku


class _BaseLineFormSet(forms.BaseFormSet):
    """Sale lines. The page adds rows from empty_form and removes them by
    ticking DELETE, so indices and TOTAL_FORMS stay valid; deleted rows are
    skipped, whatever they contain."""

    def live_forms(self):
        return [
            form for form in self.forms
            if not self._should_delete_form(form) and form.cleaned_data.get("variant")
        ]

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not self.live_forms():
            raise forms.ValidationError("Add at least one item.")

    def attach_item_labels(self):
        """Give each row's item select one option: its chosen SKU, labelled
        with the product name, so a re-rendered form shows what was picked."""
        skus = {(form["sku"].value() or "").strip() for form in self.forms} - {""}
        names = {
            variant.sku: str(variant)
            for variant in ProductVariant.objects.filter(sku__in=skus).select_related("edition__product__brand")
        }
        for form in self.forms:
            sku = (form["sku"].value() or "").strip()
            form.fields["sku"].widget.choices = [("", "")] + ([(sku, names.get(sku, sku))] if sku else [])

    def lines(self):
        return [(form.cleaned_data["variant"], form.cleaned_data["quantity"]) for form in self.live_forms()]

    def check_stock(self, warehouse):
        """Add an error under each line asking for more than the warehouse
        holds physically (rows of one SKU count together). Display check
        only: the locked reservation at sale time stays authoritative."""
        forms_by_variant = {}
        for form in self.live_forms():
            forms_by_variant.setdefault(form.cleaned_data["variant"], []).append(form)
        available = in_store_available([v.pk for v in forms_by_variant], warehouse)
        for variant, line_forms in forms_by_variant.items():
            units = available.get(variant.pk, 0)
            if sum(f.cleaned_data["quantity"] for f in line_forms) > units:
                noun = "unit is" if units == 1 else "units are"
                line_forms[-1].add_error("quantity", f"Only {units} {noun} available in {warehouse}.")
        return not any(form.errors for form in self.live_forms())


# Public name for UIs that need their own line-row widgets (the Smerfume
# Panel) but the same line rules.
BaseInStoreLineFormSet = _BaseLineFormSet

InStoreSaleLineFormSet = forms.formset_factory(
    InStoreSaleLineForm, formset=_BaseLineFormSet, extra=1, max_num=50, can_delete=True,
)


class ShipOrderForm(forms.Form):
    # Required here even though mark_order_shipped() accepts none: shipping
    # from the admin must always record the courier tracking number.
    tracking_number = forms.CharField(max_length=100, widget=UnfoldAdminTextInputWidget)


class CompleteRefundForm(forms.Form):
    refund_reference = forms.CharField(
        max_length=100, widget=UnfoldAdminTextInputWidget,
        help_text="UTR / bank reference / gateway refund id.",
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)


class FailRefundForm(forms.Form):
    failure_reason = forms.CharField(widget=UnfoldAdminTextareaWidget)


class StaffReturnForm(forms.Form):
    """One quantity / reason / notes row per order item. Eligibility (window,
    quantity caps, in-store) is left entirely to create_return()."""

    def __init__(self, *args, order_items=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.order_items = list(order_items)
        reasons = [("", "—")] + list(ReturnItem.REASON_CHOICES)
        for item in self.order_items:
            self.fields[f"quantity_{item.pk}"] = forms.IntegerField(
                label=f"{item.variant} (bought {item.quantity})", min_value=0, initial=0,
                required=False, widget=UnfoldAdminIntegerFieldWidget,
            )
            self.fields[f"reason_{item.pk}"] = forms.ChoiceField(
                label="Reason", choices=reasons, required=False, widget=UnfoldAdminSelectWidget,
            )
            self.fields[f"notes_{item.pk}"] = forms.CharField(
                label="Notes", required=False, widget=UnfoldAdminTextInputWidget,
            )

    def clean(self):
        cleaned = super().clean()
        items = []
        for item in self.order_items:
            quantity = cleaned.get(f"quantity_{item.pk}") or 0
            if not quantity:
                continue
            reason = cleaned.get(f"reason_{item.pk}")
            if not reason:
                self.add_error(f"reason_{item.pk}", "Choose a reason for this item.")
                continue
            items.append({
                "order_item": item, "requested_quantity": quantity, "reason": reason,
                "reason_notes": cleaned.get(f"notes_{item.pk}", ""),
            })
        if not items and not self.errors:
            raise forms.ValidationError("Enter a quantity for at least one item.")
        cleaned["items"] = items
        return cleaned


class RecordPaymentForm(forms.Form):
    """DEC-013 Record Payment. Field checks here are for clear messages; the
    payment service re-checks everything with the order locked."""

    amount = forms.DecimalField(
        max_digits=10, decimal_places=2, label="Amount received (₹)",
        widget=UnfoldAdminDecimalFieldWidget,
        help_text="Defaults to the balance due. Enter less for a part payment.",
    )
    payment_date = forms.DateField(label="Payment date", widget=UnfoldAdminSingleDateWidget)
    method = forms.ChoiceField(
        label="Payment mode", choices=[("", "Choose payment mode")] + OrderPayment.METHOD_CHOICES,
        widget=UnfoldAdminSelectWidget,
    )
    reference = forms.CharField(
        required=False, max_length=order_payments.REFERENCE_MAX_LENGTH, label="Reference",
        widget=UnfoldAdminTextInputWidget,
        help_text=(
            "Optional for cash. Required for UPI, card and net banking: UTR, slip or "
            "transaction number. Never enter a card number or CVV."
        ),
    )
    notes = forms.CharField(
        required=False, max_length=order_payments.NOTES_MAX_LENGTH, label="Notes",
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
    )
    # Balance due when the page was opened: an out-of-date page is sent back
    # for review. A convenience only; the locked recalculation decides.
    expected_balance = forms.DecimalField(max_digits=12, decimal_places=2, widget=forms.HiddenInput)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("The amount must be greater than zero.")
        return amount

    def clean_payment_date(self):
        payment_date = self.cleaned_data["payment_date"]
        if payment_date > timezone.localdate():
            raise forms.ValidationError("The payment date cannot be in the future.")
        return payment_date

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("method")
        if method:
            try:
                cleaned["reference"] = order_payments.validate_reference(method, cleaned.get("reference"))
            except order_payments.OrderPaymentError as exc:
                self.add_error("reference", str(exc))
        return cleaned


def stale_payment_form(form, data, balance_due):
    """Duplicate/stale-submit guard for Record Payment: if the balance due
    changed since the page was opened (another payment, or this one already
    saved by a double submit), return a re-bound form showing the current
    balance and an error, so nothing is recorded; else None. A convenience
    only: record_order_payment() still decides under the order lock."""
    if form.cleaned_data["expected_balance"] == balance_due:
        return None
    fresh = data.copy()
    fresh["expected_balance"] = str(balance_due)
    stale = form.__class__(fresh)
    stale.is_valid()
    stale.add_error(None, "Payments changed since this page was opened. Check the balance due and save again.")
    return stale


class ReturnItemInspectionForm(forms.ModelForm):
    """A normal-looking ModelForm bound to real ReturnItem fields — but
    ReturnItemAdmin.save_model() below never calls form.save()/obj.save().
    The only way these fields are ever persisted is through
    finalize_return_item(), which performs the inventory mutation and the
    field write in one atomic operation. This form exists purely to render
    editable widgets and produce cleaned_data for that call."""

    # The model allows these null (before inspection); the form requires
    # them, since inspection is meaningless without a real answer for each.
    received_quantity = forms.IntegerField(min_value=0)
    disposition = forms.ChoiceField(choices=ReturnItem.DISPOSITION_CHOICES)
    resolution = forms.ChoiceField(choices=ReturnItem.RESOLUTION_CHOICES)

    class Meta:
        model = ReturnItem
        fields = ["received_quantity", "disposition", "resolution", "remaining_quantity_ml", "inspection_notes"]

    def clean(self):
        cleaned = super().clean()
        disposition = cleaned.get("disposition")
        remaining_ml = cleaned.get("remaining_quantity_ml")
        if disposition == ReturnItem.DISPOSITION_RESTOCKED_PARTIAL and not remaining_ml:
            self.add_error("remaining_quantity_ml", "Required when disposition is restocked_partial.")
        if disposition != ReturnItem.DISPOSITION_RESTOCKED_PARTIAL and remaining_ml:
            self.add_error("remaining_quantity_ml", "Only valid when disposition is restocked_partial.")
        return cleaned


class RefundMethodForm(forms.Form):
    """Choose a pending refund's method; routed through set_refund_method()."""

    refund_method = forms.ChoiceField(
        choices=[("", "Choose refund method")] + list(Refund.METHOD_CHOICES), label="Refund method",
    )


class RefundAdjustmentEntryForm(forms.ModelForm):
    """Amount and reason for create_refund_adjustment(); never saved directly."""

    class Meta:
        model = RefundAdjustment
        fields = ["adjustment_amount", "reason"]


class ReturnReceivedQuantitiesForm(forms.Form):
    """One received-quantity field per return line, for
    services.record_return_received_quantities()."""

    def __init__(self, *args, return_items=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.return_items = list(return_items)
        for item in self.return_items:
            self.fields[f"received_{item.public_id}"] = forms.IntegerField(
                label=f"{item.order_item.variant} (approved {item.approved_quantity})",
                min_value=0, max_value=32767, initial=item.received_quantity,
            )

    def quantities(self):
        return {item: self.cleaned_data[f"received_{item.public_id}"] for item in self.return_items}
