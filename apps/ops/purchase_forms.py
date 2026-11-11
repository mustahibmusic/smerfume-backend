"""Smerfume Panel forms for purchase drafts and purchase master data.

Presentation and input parsing only (plain widgets; rows chosen by vendor
code, SKU or public id, never database id). The services in
apps.purchases.services and apps.inventory.services.suppliers validate
again with the rows locked and are the only write path.
"""

from django import forms
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import ProductVariant
from apps.inventory.models import Supplier, Warehouse
from apps.inventory.services.suppliers import EDITABLE_FIELDS as VENDOR_FIELDS
from apps.purchases.models import (
    GoodsReceiptLine,
    PaymentAccount,
    PurchaseOrder,
    ReceiptDiscrepancy,
)


def _date_input():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SkuField(forms.CharField):
    """A product variant typed or scanned as SKU (or exact barcode)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 64)
        kwargs.setdefault("label", "Item (SKU)")
        kwargs.setdefault("widget", forms.TextInput(attrs={
            "autocomplete": "off", "data-sku-search": "", "placeholder": "SKU, barcode or name",
        }))
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        variant = (
            ProductVariant.objects.filter(sku=value).first()
            or ProductVariant.objects.filter(barcode=value).first()
        )
        if variant is None:
            raise forms.ValidationError(f"Unknown SKU '{value}'.")
        return variant


# ── Purchase orders ──────────────────────────────────────────────────────────


class PurchaseOrderHeaderForm(forms.Form):
    supplier = forms.ModelChoiceField(
        queryset=Supplier.objects.none(), to_field_name="vendor_code", label="Vendor",
    )
    warehouse = forms.ModelChoiceField(queryset=Warehouse.objects.none(), to_field_name="public_id")
    order_date = forms.DateField(widget=_date_input(), input_formats=["%Y-%m-%d"])
    expected_date = forms.DateField(required=False, widget=_date_input(), input_formats=["%Y-%m-%d"])
    vendor_reference = forms.CharField(required=False, max_length=100, label="Vendor reference")
    purchase_tax_treatment = forms.ChoiceField(
        choices=[("", "Choose tax treatment")] + list(PurchaseOrder.TAX_TREATMENT_CHOICES),
        label="Purchase tax treatment",
        help_text="GST invoice: every line needs a tax rate. No GST invoice: prices exclude tax, no tax rates.",
    )
    amounts_include_tax = forms.BooleanField(required=False, label="Prices include GST")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, po=None, **kwargs):
        super().__init__(*args, **kwargs)
        # New choices must be active; a draft keeps its current vendor.
        current_supplier = po.supplier_id if po else None
        current_warehouse = po.warehouse_id if po else None
        self.fields["supplier"].queryset = Supplier.objects.filter(
            Q(is_active=True) | Q(pk=current_supplier)
        ).order_by("name")
        self.fields["warehouse"].queryset = Warehouse.objects.filter(
            Q(is_active=True) | Q(pk=current_warehouse)
        ).order_by("name")
        if po is None:
            self.fields["order_date"].initial = timezone.localdate()
            self.fields["warehouse"].initial = Warehouse.objects.filter(is_default=True).first()


class PurchaseOrderLineForm(forms.Form):
    sku = SkuField()
    quantity_ordered = forms.IntegerField(min_value=1, label="Quantity")
    unit_price = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, label="Unit price")
    line_discount_amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, label="Line discount",
    )
    tax_rate = forms.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False, label="Tax %",
    )
    hsn_code = forms.CharField(max_length=8, required=False, label="HSN")

    def line_data(self):
        data = self.cleaned_data
        return {
            "variant": data["sku"], "quantity_ordered": data["quantity_ordered"],
            "unit_price": data["unit_price"], "line_discount_amount": data.get("line_discount_amount") or 0,
            "tax_rate": data.get("tax_rate"), "hsn_code": data.get("hsn_code") or "",
        }


class _LineFormSet(forms.BaseFormSet):
    def live_forms(self):
        return [
            form for form in self.forms
            if not self._should_delete_form(form) and form.has_changed() and form.cleaned_data.get("sku")
        ]

    def line_data(self):
        return [form.line_data() for form in self.live_forms()]


PurchaseOrderLineFormSet = forms.formset_factory(
    PurchaseOrderLineForm, formset=_LineFormSet, extra=1, max_num=200, can_delete=True,
)


def po_line_initial(po):
    return [
        {
            "sku": line.variant.sku, "quantity_ordered": line.quantity_ordered, "unit_price": line.unit_price,
            "line_discount_amount": line.line_discount_amount, "tax_rate": line.tax_rate,
            "hsn_code": line.hsn_code,
        }
        for line in po.lines.select_related("variant").order_by("pk")
    ]


def po_header_initial(po):
    return {
        "supplier": po.supplier, "warehouse": po.warehouse, "order_date": po.order_date,
        "expected_date": po.expected_date, "vendor_reference": po.vendor_reference,
        "purchase_tax_treatment": po.purchase_tax_treatment, "amounts_include_tax": po.amounts_include_tax,
        "notes": po.notes,
    }


# ── Goods receipts ───────────────────────────────────────────────────────────


class ReceiptHeaderForm(forms.Form):
    received_date = forms.DateField(widget=_date_input(), input_formats=["%Y-%m-%d"])
    vendor_document_reference = forms.CharField(
        required=False, max_length=100, label="Delivery challan / invoice number",
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_received_date(self):
        value = self.cleaned_data["received_date"]
        if value > timezone.localdate():
            raise forms.ValidationError("The received date cannot be in the future.")
        return value


class ReceiveGoodsStartForm(forms.Form):
    received_date = forms.DateField(widget=_date_input(), input_formats=["%Y-%m-%d"])
    vendor_document_reference = forms.CharField(
        required=False, max_length=100, label="Delivery challan / invoice number",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["received_date"].initial = timezone.localdate()


class ReceiptQuantitiesForm(forms.Form):
    """One retail and one damaged quantity per PO line (by its public id)."""

    def __init__(self, *args, po_lines=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.po_lines = list(po_lines)
        for line in self.po_lines:
            self.fields[f"retail_{line.public_id}"] = forms.IntegerField(
                required=False, min_value=0, label=f"{line.variant.sku} retail",
            )
            self.fields[f"damaged_{line.public_id}"] = forms.IntegerField(
                required=False, min_value=0, label=f"{line.variant.sku} damaged",
            )

    def rows(self):
        return [(line, self[f"retail_{line.public_id}"], self[f"damaged_{line.public_id}"]) for line in self.po_lines]

    def line_data(self):
        data = []
        for line in self.po_lines:
            for stock_type in ("retail", "damaged"):
                quantity = self.cleaned_data.get(f"{stock_type}_{line.public_id}")
                if quantity:
                    data.append({"po_line": line, "stock_type": stock_type, "quantity": quantity})
        return data


class ReceiptDiscrepancyForm(forms.Form):
    discrepancy_type = forms.ChoiceField(
        choices=[("", "Choose")] + [
            c for c in ReceiptDiscrepancy.TYPE_CHOICES if c[0] in ReceiptDiscrepancy.STAFF_TYPES
        ],
        label="Type",
    )
    po_line = forms.ChoiceField(required=False, label="PO line")
    quantity = forms.IntegerField(min_value=1)
    sku = SkuField(required=False, label="Item that arrived (wrong item)")
    observed_item_description = forms.CharField(required=False, max_length=255, label="Or describe it")
    notes = forms.CharField(required=False, max_length=500)

    def __init__(self, *args, po_lines=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.po_lines = {str(line.public_id): line for line in po_lines}
        self.fields["po_line"].choices = [("", "—")] + [
            (key, f"{line.variant.sku} (ordered {line.quantity_ordered})") for key, line in self.po_lines.items()
        ]

    def discrepancy_data(self):
        data = self.cleaned_data
        return {
            "discrepancy_type": data["discrepancy_type"], "po_line": self.po_lines.get(data.get("po_line") or ""),
            "quantity": data["quantity"], "variant": data.get("sku"),
            "observed_item_description": data.get("observed_item_description", ""), "notes": data.get("notes", ""),
        }


class _DiscrepancyFormSet(forms.BaseFormSet):
    def __init__(self, *args, po_lines=(), **kwargs):
        self.po_lines = po_lines
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        return {**super().get_form_kwargs(index), "po_lines": self.po_lines}

    @property
    def empty_form(self):
        form = self.form(auto_id=self.auto_id, prefix=self.add_prefix("__prefix__"), empty_permitted=True,
                         use_required_attribute=False, po_lines=self.po_lines, renderer=self.renderer)
        self.add_fields(form, None)
        return form

    def discrepancy_data(self):
        return [
            form.discrepancy_data() for form in self.forms
            if not self._should_delete_form(form) and form.has_changed() and form.cleaned_data.get("discrepancy_type")
        ]


ReceiptDiscrepancyFormSet = forms.formset_factory(
    ReceiptDiscrepancyForm, formset=_DiscrepancyFormSet, extra=1, max_num=100, can_delete=True,
)


def receipt_quantities_initial(receipt):
    initial = {}
    for line in receipt.lines.select_related("po_line"):
        if line.po_line_id:
            key = f"{line.stock_type}_{line.po_line.public_id}"
            initial[key] = initial.get(key, 0) + line.quantity
    return initial


def discrepancy_initial(receipt):
    return [
        {
            "discrepancy_type": d.discrepancy_type, "po_line": str(d.po_line.public_id) if d.po_line_id else "",
            "quantity": d.quantity, "sku": d.variant.sku if d.variant_id else "",
            "observed_item_description": d.observed_item_description, "notes": d.notes,
        }
        for d in receipt.discrepancies.select_related("po_line", "variant").filter(
            discrepancy_type__in=ReceiptDiscrepancy.STAFF_TYPES,
        ).order_by("pk")
    ]


# ── Opening stock ────────────────────────────────────────────────────────────


class OpeningStartForm(forms.Form):
    warehouse = forms.ModelChoiceField(
        queryset=Warehouse.objects.filter(is_active=True).order_by("name"), to_field_name="public_id",
    )
    received_date = forms.DateField(widget=_date_input(), input_formats=["%Y-%m-%d"], label="Stock count date")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].initial = Warehouse.objects.filter(is_default=True).first()
        self.fields["received_date"].initial = timezone.localdate()


class OpeningLineForm(forms.Form):
    sku = SkuField()
    stock_type = forms.ChoiceField(choices=GoodsReceiptLine.STOCK_TYPE_CHOICES, label="Stock type")
    quantity = forms.IntegerField(min_value=1)
    unit_cost = forms.DecimalField(max_digits=12, decimal_places=4, min_value=0, label="Unit cost (ex tax)")
    tax_rate = forms.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False,
                                  label="Tax %")

    def line_data(self):
        data = self.cleaned_data
        return {
            "variant": data["sku"], "stock_type": data["stock_type"], "quantity": data["quantity"],
            "unit_cost": data["unit_cost"], "tax_rate": data.get("tax_rate"),
        }


OpeningLineFormSet = forms.formset_factory(
    OpeningLineForm, formset=_LineFormSet, extra=1, max_num=500, can_delete=True,
)


def opening_line_initial(receipt):
    return [
        {"sku": line.variant.sku, "stock_type": line.stock_type, "quantity": line.quantity,
         "unit_cost": line.unit_cost, "tax_rate": line.tax_rate}
        for line in receipt.lines.select_related("variant").order_by("pk")
    ]


# ── Master data ──────────────────────────────────────────────────────────────


class VendorForm(forms.ModelForm):
    """Field list and validation from the model; saved only through
    apps.inventory.services.suppliers."""

    class Meta:
        model = Supplier
        fields = VENDOR_FIELDS
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class PaymentAccountForm(forms.ModelForm):
    """Name and kind only (no balances or account details). Saved only
    through create/update_payment_account()."""

    class Meta:
        model = PaymentAccount
        fields = ("name", "kind", "is_active")

