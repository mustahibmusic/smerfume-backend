"""Staff forms for purchase workflows, shared by Django Admin and the
Smerfume Panel (/ops/). Input validation and display only: every rule is
re-checked by apps.purchases.services with the rows locked."""

from decimal import Decimal

from django import forms
from django.db.models import Q
from django.forms.models import ModelChoiceIterator
from django.utils import timezone
from django.utils.safestring import mark_safe
from unfold.widgets import (
    DATETIME_CLASSES,
    UnfoldAdminDateWidget,
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

from apps.inventory.models import Warehouse

from . import selectors
from .models import (
    PaymentAccount,
    PurchaseOrder,
    PurchaseOrderPayment,
    ReceiptDiscrepancy,
    SupplierPaymentAttachment,
)


class CancelPurchaseOrderForm(forms.Form):
    reason = forms.CharField(widget=UnfoldAdminTextareaWidget, label="Reason for cancelling")


class ClosePurchaseOrderForm(forms.Form):
    reason = forms.CharField(widget=UnfoldAdminTextareaWidget, label="Reason for closing")


class ConfirmBookedLineForm(forms.Form):
    """One PO line on the Confirm booked quantity page. original_total is
    the value the page was loaded with, so a stale page is refused."""

    line = forms.IntegerField(widget=forms.HiddenInput)
    original_total = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    confirmed_total = forms.IntegerField(
        min_value=0, label="New confirmed total",
        widget=UnfoldAdminIntegerFieldWidget(attrs={"class": "max-w-24", "data-confirmed-total": ""}),
    )


class _BaseConfirmBookedFormSet(forms.BaseFormSet):
    """Exactly one row per line of the purchase order, no more, no fewer."""

    def __init__(self, *args, po_lines, **kwargs):
        self.po_lines = {line.pk: line for line in po_lines}
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        submitted = [form.cleaned_data["line"] for form in self.forms]
        if sorted(submitted) != sorted(self.po_lines):
            raise forms.ValidationError(
                "The lines on this page no longer match the purchase order. Reload the page."
            )

    def totals(self):
        return {form.cleaned_data["line"]: form.cleaned_data["confirmed_total"] for form in self.forms}

    def originals(self):
        return {form.cleaned_data["line"]: form.cleaned_data["original_total"] for form in self.forms}


ConfirmBookedFormSet = forms.formset_factory(
    ConfirmBookedLineForm, formset=_BaseConfirmBookedFormSet, extra=0, max_num=1000,
)


class ConfirmBookedNoteForm(forms.Form):
    # Required only when a total changes; the view and the service check that.
    note = forms.CharField(
        required=False, widget=UnfoldAdminTextareaWidget(attrs={"rows": 2}),
        label="Vendor confirmation note",
        help_text="One note for this save, e.g. who confirmed and how (mail, call, PI number).",
    )


class ReverseGoodsReceiptForm(forms.Form):
    """One quantity field per reversible line plus a required reason."""

    def __init__(self, *args, lines=(), reversible=None, **kwargs):
        super().__init__(*args, **kwargs)
        reversible = reversible or {}
        for line in lines:
            remaining = reversible.get(line.pk, 0)
            if remaining <= 0:
                continue
            self.fields[f"line_{line.pk}"] = forms.IntegerField(
                required=False, min_value=0, max_value=remaining,
                label=(
                    f"{line.variant.sku} [{line.get_stock_type_display().lower()}]: "
                    f"received {line.quantity}, reversible {remaining}"
                ),
                widget=UnfoldAdminTextInputWidget,
            )
        self.fields["reason"] = forms.CharField(
            widget=UnfoldAdminTextareaWidget, label="Reason for reversal"
        )

    def quantities(self):
        return {
            int(name.removeprefix("line_")): value
            for name, value in self.cleaned_data.items()
            if name.startswith("line_") and value
        }


class ResolveDiscrepancyForm(forms.Form):
    resolution = forms.ChoiceField(
        choices=ReceiptDiscrepancy.RESOLUTION_CHOICES, widget=UnfoldAdminSelectWidget
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget, label="Notes")


class ReceiveGoodsForm(forms.Form):
    received_date = forms.DateField(widget=UnfoldAdminDateWidget)
    vendor_document_reference = forms.CharField(
        required=False, max_length=100, widget=UnfoldAdminTextInputWidget,
        label="Delivery challan / invoice number",
    )


class OpeningReceiptForm(forms.Form):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.filter(is_active=True).order_by("name"),
        widget=UnfoldAdminSelectWidget,
    )
    received_date = forms.DateField(widget=UnfoldAdminDateWidget, label="Stock count date")
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].initial = Warehouse.objects.filter(is_default=True).first()
        self.fields["received_date"].initial = timezone.localdate()


class GroupedPaymentAccountIterator(ModelChoiceIterator):
    """Paid Through options as <optgroup>s by kind, in the order the kinds are
    declared on the model, accounts by name inside each group."""

    def __iter__(self):
        if self.field.empty_label is not None:
            yield ("", self.field.empty_label)
        labels = dict(PaymentAccount.KIND_CHOICES)
        rank = {kind: i for i, kind in enumerate(labels)}
        groups = {}
        for account in self.queryset:
            groups.setdefault(account.kind, []).append(account)
        for kind in sorted(groups, key=lambda k: (rank.get(k, len(rank)), k)):
            accounts = sorted(groups[kind], key=lambda a: a.name.lower())
            yield (labels.get(kind, kind), [self.choice(a) for a in accounts])

    def __len__(self):
        return self.queryset.count() + (1 if self.field.empty_label is not None else 0)


class PaymentAccountChoiceField(forms.ModelChoiceField):
    """Grouped, searchable Paid Through select (Select2, Unfold theme)."""

    iterator = GroupedPaymentAccountIterator

    def __init__(self, **kwargs):
        kwargs.setdefault("queryset", PaymentAccount.objects.filter(is_active=True))
        kwargs.setdefault("widget", UnfoldAdminSelect2Widget)
        kwargs.setdefault("empty_label", "Select an account")
        super().__init__(**kwargs)

    def label_from_instance(self, obj):
        return obj.name if obj.is_active else f"{obj.name} (inactive)"


class SupplierPaymentForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), widget=UnfoldAdminDecimalFieldWidget,
    )
    entry_date = forms.DateField(widget=UnfoldAdminDateWidget, label="Date")
    method = forms.ChoiceField(choices=PurchaseOrderPayment.METHOD_CHOICES, widget=UnfoldAdminSelectWidget)
    reference = forms.CharField(
        required=False, max_length=100, widget=UnfoldAdminTextInputWidget,
        help_text="UTR, cheque or card slip number. Optional for cash.",
    )
    notes = forms.CharField(required=False, widget=UnfoldAdminTextareaWidget)
    paid_through = PaymentAccountChoiceField(required=False, label="Paid through")

    def __init__(self, *args, with_paid_through=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["entry_date"].initial = timezone.localdate()
        if not with_paid_through:
            del self.fields["paid_through"]


class PaymentDateInput(forms.DateInput):
    """Native date picker: keyboard entry and a calendar without the admin
    calendar script (which needs jsi18n), and an unambiguous ISO value."""

    input_type = "date"
    template_name = "unfold/widgets/date.html"

    def __init__(self, attrs=None):
        super().__init__(attrs={"class": " ".join(DATETIME_CLASSES), **(attrs or {})}, format="%Y-%m-%d")


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several proof files in one input; the service validates their content."""

    def __init__(self, *args, **kwargs):
        # Unfold's prebuilt CSS has no file: utilities, so the input is
        # transparent over a styled "Upload files" label in the template.
        kwargs.setdefault("widget", MultipleFileInput(attrs={
            "accept": "application/pdf,image/jpeg,image/png,image/webp",
            "class": "absolute cursor-pointer inset-0 opacity-0",
        }))
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data if item]
        return [single(data, initial)] if data else []


class PaymentProofForm(forms.Form):
    attachments = MultipleFileField(
        label="Attachments",
        help_text="PDF, JPG, PNG or WebP. Up to 5 files per payment, 5 MB each. "
                  "If the form shows an error, select the files again.",
    )

    def __init__(self, *args, existing_count=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.existing_count = existing_count

    def clean_attachments(self):
        files = self.cleaned_data["attachments"]
        limit = SupplierPaymentAttachment.MAX_PER_PAYMENT
        if self.existing_count + len(files) > limit:
            raise forms.ValidationError(
                f"A payment can have at most {limit} attachments ({self.existing_count} already attached)."
            )
        return files


class SupplierPaymentEntryForm(SupplierPaymentForm, PaymentProofForm):
    """DEC-011 Record Payment form: Save as Draft or Save as Paid."""

    # ISO only: en-us parsing would read a typed 01/11/2026 as 11 January.
    entry_date = forms.DateField(widget=PaymentDateInput, input_formats=["%Y-%m-%d"], label="Payment date")
    # Display only: disabled fields ignore submitted data; the service numbers on posting.
    payment_number = forms.CharField(
        required=False, disabled=True, label="Payment #",
        widget=UnfoldAdminTextInputWidget(attrs={"placeholder": "Assigned when saved as paid"}),
    )

    field_order = (
        "amount", "method", "entry_date", "payment_number", "paid_through", "reference", "notes", "attachments",
    )

    def __init__(self, *args, current_account=None, posting=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.posting = posting
        self.fields["entry_date"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["method"].label = "Payment mode"
        self.fields["notes"].widget.attrs["rows"] = 3
        # Not a required field (drafts may omit it), so Unfold draws no marker itself.
        self.fields["paid_through"].label = mark_safe('Paid through <span class="text-red-600">*</span>')
        self.fields["paid_through"].help_text = "Required to save as paid. Optional on a draft."
        if current_account is not None:
            # Keep a draft's now-inactive account visible; posting will refuse it.
            self.fields["paid_through"].queryset = PaymentAccount.objects.filter(
                Q(is_active=True) | Q(pk=current_account.pk)
            )
        if not self.fields["paid_through"].queryset.exists():
            self.fields["paid_through"].help_text = (
                "No Paid Through accounts yet. A manager adds them under Purchases > Paid Through Accounts, "
                "named as the ledger account in Zoho Books. Required to save as paid."
            )

    def clean(self):
        """Mirror the service input rules so each message shows under its
        field. The services re-check everything and stay authoritative."""
        cleaned = super().clean()
        entry_date = cleaned.get("entry_date")
        if entry_date and entry_date > timezone.localdate():
            self.add_error("entry_date", "The date cannot be in the future.")
        method = cleaned.get("method")
        reference = (cleaned.get("reference") or "").strip()
        notes = (cleaned.get("notes") or "").strip()
        if method in PurchaseOrderPayment.REFERENCE_REQUIRED_METHODS and not reference:
            self.add_error("reference", "A reference (UTR, cheque or card slip number) is required for this method.")
        elif method == PurchaseOrderPayment.METHOD_OTHER and not reference and len(notes) < 10:
            self.add_error("reference", "For 'Other', enter a reference or notes explaining the payment.")
        if self.posting and "paid_through" in self.fields and "paid_through" not in self.errors:
            account = cleaned.get("paid_through")
            if account is None:
                self.add_error("paid_through", "Paid through is required to post a payment.")
            elif not account.is_active:
                self.add_error("paid_through", f"Paid through account '{account.name}' is inactive.")
        return cleaned


class ReversePaymentEntryForm(forms.Form):
    entry = forms.ModelChoiceField(queryset=PurchaseOrderPayment.objects.none(), widget=UnfoldAdminSelectWidget)
    reason = forms.CharField(widget=UnfoldAdminTextareaWidget, help_text="Why the original entry was wrong.")

    def __init__(self, *args, po, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["entry"].queryset = selectors.reversible_payment_entries(po)
        self.fields["entry"].label_from_instance = lambda e: (
            f"{e.entry_date} {e.get_entry_type_display()} {e.amount} ({e.get_method_display()} {e.reference})"
        )


class SetTaxTreatmentForm(forms.Form):
    treatment = forms.ChoiceField(
        choices=PurchaseOrder.TAX_TREATMENT_CHOICES, widget=UnfoldAdminSelectWidget,
        label="Purchase tax treatment",
    )
