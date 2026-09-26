from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q, Sum
from django.utils.html import format_html, format_html_join
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminTextareaWidget, UnfoldAdminTextInputWidget

from apps.inventory.models import Supplier

from . import selectors
from . import services as po_services
from .models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine

AUDIT_FIELDS = (
    "created_by", "created_at", "issued_by", "issued_at",
    "cancelled_by", "cancelled_at", "cancel_reason",
)


class PurchaseOrderLineInline(TabularInline):
    """Lines are edited only on the purchase order screen, and only while
    it is a draft. Never registered as a separate admin page."""

    model = PurchaseOrderLine
    extra = 0
    autocomplete_fields = ("variant",)
    fields = (
        "variant", "quantity_ordered", "unit_price", "line_discount_amount",
        "tax_rate", "hsn_code", "gross_line_amount", "taxable_value",
        "effective_unit_cost_ex_tax", "received", "outstanding",
    )
    readonly_fields = (
        "gross_line_amount", "taxable_value", "effective_unit_cost_ex_tax",
        "received", "outstanding",
    )

    def _is_draft(self, obj):
        return obj is None or obj.status == PurchaseOrder.STATUS_DRAFT

    def has_add_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_delete_permission(request, obj)

    @admin.display(description="Gross")
    def gross_line_amount(self, line):
        return line.gross_line_amount if line.pk else "-"

    @admin.display(description="Taxable value")
    def taxable_value(self, line):
        return line.taxable_value if line.pk else "-"

    @admin.display(description="Unit cost (ex tax)")
    def effective_unit_cost_ex_tax(self, line):
        return line.effective_unit_cost_ex_tax if line.pk else "-"

    @admin.display(description="Received")
    def received(self, line):
        return selectors.received_quantity(line) if line.pk else "-"

    @admin.display(description="Outstanding")
    def outstanding(self, line):
        return selectors.outstanding_quantity(line) if line.pk else "-"


class CancelPurchaseOrderForm(forms.Form):
    reason = forms.CharField(widget=UnfoldAdminTextareaWidget, label="Reason for cancelling")


class ReceiveGoodsForm(forms.Form):
    received_date = forms.DateField(widget=UnfoldAdminDateWidget)
    vendor_document_reference = forms.CharField(
        required=False, max_length=100, widget=UnfoldAdminTextInputWidget,
        label="Delivery challan / invoice number",
    )


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(ModelAdmin):
    """Purchase order business screen. Status changes only through the
    Issue / Cancel actions, which call apps.purchases.services."""

    inlines = [PurchaseOrderLineInline]
    list_display = (
        "po_number", "supplier", "warehouse", "order_date", "expected_date",
        "status_label", "line_count", "order_value", "created_by",
    )
    list_filter = ("status", "supplier", "warehouse", "order_date")
    search_fields = (
        "po_number", "supplier__name", "supplier__vendor_code",
        "vendor_reference", "lines__variant__sku",
    )
    list_select_related = ("supplier", "warehouse", "created_by")
    actions_detail = ["issue_po", "receive_goods", "cancel_po"]
    fieldsets = (
        ("Purchase order", {"fields": (
            "number", "status", "supplier", "warehouse", "order_date", "expected_date",
        )}),
        ("Vendor terms", {"fields": ("vendor_reference", "amounts_include_tax")}),
        ("Notes", {"fields": ("notes",)}),
        ("Totals", {"fields": (
            "total_gross_amount", "total_discount_amount",
            "total_discounted_amount", "total_taxable_value",
        )}),
        ("Audit", {"fields": AUDIT_FIELDS, "classes": ("collapse",)}),
    )
    TOTAL_FIELDS = (
        "total_gross_amount", "total_discount_amount",
        "total_discounted_amount", "total_taxable_value",
    )

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .annotate(_line_count=Count("lines"))
            .prefetch_related("lines")
        )

    def has_delete_permission(self, request, obj=None):
        # Purchase orders are cancelled, never deleted.
        return False

    def get_readonly_fields(self, request, obj=None):
        always = ("number", "status", *self.TOTAL_FIELDS, *AUDIT_FIELDS)
        if obj is None:
            return always
        editable = set(obj.editable_fields())
        header = (
            "supplier", "warehouse", "order_date", "expected_date",
            "vendor_reference", "amounts_include_tax", "notes",
        )
        return always + tuple(f for f in header if f not in editable)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # New choices must be active vendors; a PO keeps showing its
        # current vendor even if that vendor was deactivated later.
        if db_field.name == "supplier":
            current = getattr(request, "_po_supplier_id", None)
            kwargs["queryset"] = Supplier.objects.filter(Q(is_active=True) | Q(pk=current))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        request._po_supplier_id = obj.supplier_id if obj else None
        return super().get_form(request, obj, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    # --- list/detail display ---

    @display(description="Status", label={
        "Draft": "info", "Issued": "warning", "Partially received": "warning",
        "Received": "success", "Closed": "success", "Cancelled": "danger",
    })
    def status_label(self, obj):
        return obj.get_status_display()

    @admin.display(description="Lines", ordering="_line_count")
    def line_count(self, obj):
        return obj._line_count

    @admin.display(description="Order value")
    def order_value(self, obj):
        return obj.total_discounted_amount

    @admin.display(description="PO number")
    def number(self, obj):
        return obj.po_number if obj.pk else "Assigned on save"

    @admin.display(description="Gross amount")
    def total_gross_amount(self, obj):
        return obj.total_gross_amount if obj.pk else "-"

    @admin.display(description="Line discounts")
    def total_discount_amount(self, obj):
        return obj.total_discount_amount if obj.pk else "-"

    @admin.display(description="Amount after discounts (as quoted)")
    def total_discounted_amount(self, obj):
        return obj.total_discounted_amount if obj.pk else "-"

    @admin.display(description="Taxable value (ex tax)")
    def total_taxable_value(self, obj):
        if not obj.pk:
            return "-"
        value = obj.total_taxable_value
        return "Tax rate missing on a line" if value is None else value

    # --- lifecycle actions ---

    def _po_in_status(self, object_id, status):
        if object_id is None:
            return True
        return PurchaseOrder.objects.filter(pk=object_id, status__in=status).exists()

    def has_issue_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.issue_purchaseorder") and self._po_in_status(
            object_id, [PurchaseOrder.STATUS_DRAFT]
        )

    def has_cancel_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.cancel_purchaseorder") and self._po_in_status(
            object_id, [PurchaseOrder.STATUS_DRAFT, PurchaseOrder.STATUS_ISSUED]
        )

    def has_receive_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.add_goodsreceipt") and self._po_in_status(
            object_id, list(PurchaseOrder.RECEIVABLE_STATUSES)
        )

    def _change_url(self, po):
        return reverse("admin:purchases_purchaseorder_change", args=[po.pk])

    @action(
        description="Receive goods", url_path="receive", permissions=["receive"],
        icon="inventory",
    )
    def receive_goods(self, request, object_id):
        """GET shows a small form; POST creates an empty draft goods
        receipt. Staff then enter only what physically arrived."""
        po = get_object_or_404(PurchaseOrder, pk=object_id)
        form = ReceiveGoodsForm(
            request.POST or None, initial={"received_date": timezone.localdate()}
        )
        if request.method == "POST" and form.is_valid():
            try:
                receipt = po_services.create_receipt_from_po(
                    po, request.user, **form.cleaned_data
                )
            except po_services.GoodsReceiptError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return redirect(self._change_url(po))
            self.message_user(
                request,
                f"Draft {receipt.grn_number} created. Enter the quantities that physically arrived.",
                level=messages.SUCCESS,
            )
            return redirect(reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk]))
        return self._action_page(
            request, po, f"Receive goods for {po.po_number}", form,
            f"Start a goods receipt for {po.po_number} from {po.supplier}. Nothing is "
            "received until you enter quantities and post the receipt.",
            "Start goods receipt", "bg-primary-600",
        )

    @action(description="Issue", url_path="issue", permissions=["issue"], icon="send")
    def issue_po(self, request, object_id):
        """GET shows a confirmation page; only POST issues (buttons are links)."""
        po = get_object_or_404(PurchaseOrder, pk=object_id)
        if request.method == "POST":
            try:
                po_services.issue_purchase_order(po, request.user)
            except po_services.PurchaseOrderError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(request, f"{po.po_number} issued.", level=messages.SUCCESS)
            return redirect(self._change_url(po))
        return self._action_page(
            request, po, f"Issue {po.po_number}", forms.Form(),
            f"Issue purchase order {po.po_number} to {po.supplier}. After issuing, "
            "prices, quantities and lines can no longer be changed.",
            "Issue purchase order", "bg-primary-600",
        )

    @action(description="Cancel", url_path="cancel", permissions=["cancel"], icon="cancel")
    def cancel_po(self, request, object_id):
        po = get_object_or_404(PurchaseOrder, pk=object_id)
        form = CancelPurchaseOrderForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                po_services.cancel_purchase_order(po, request.user, form.cleaned_data["reason"])
            except po_services.PurchaseOrderError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(request, f"{po.po_number} cancelled.", level=messages.SUCCESS)
            return redirect(self._change_url(po))
        return self._action_page(
            request, po, f"Cancel {po.po_number}", form,
            f"Cancel purchase order {po.po_number} for {po.supplier}. This cannot be "
            "undone. The order stays on record as cancelled.",
            "Cancel purchase order", "bg-red-600",
        )

    def _action_page(self, request, po, title, form, message, button_label, button_class):
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.model._meta,
            "form": form,
            "message": message,
            "button_label": button_label,
            "button_class": button_class,
            "back_url": self._change_url(po),
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)


GRN_AUDIT_FIELDS = (
    "created_by", "created_at", "posted_by", "posted_at",
    "cancelled_by", "cancelled_at", "cancel_reason",
)


class GoodsReceiptLineInline(TabularInline):
    """Received quantities, entered on the goods receipt screen while it is
    a draft. Variant, cost and tax come from the chosen PO line."""

    model = GoodsReceiptLine
    extra = 0
    fields = (
        "po_line", "stock_type", "quantity", "sku", "unit_cost_ex_tax", "tax",
        "ordered", "received_to_date", "outstanding",
    )
    readonly_fields = (
        "sku", "unit_cost_ex_tax", "tax", "ordered", "received_to_date", "outstanding",
    )

    def _is_draft(self, obj):
        return obj is None or obj.is_draft

    def has_add_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._is_draft(obj) and super().has_delete_permission(request, obj)

    def get_extra(self, request, obj=None, **kwargs):
        return len(getattr(request, "_grn_prefill", ()))

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "po_line":
            kwargs["queryset"] = PurchaseOrderLine.objects.filter(
                purchase_order_id=getattr(request, "_grn_po_id", None)
            ).select_related("variant")
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == "po_line":
            field.label_from_instance = lambda line: (
                f"{line.variant.sku} - ordered {line.quantity_ordered} @ {line.unit_price}"
            )
        return field

    @admin.display(description="SKU")
    def sku(self, line):
        return line.variant.sku if line.pk else "-"

    @admin.display(description="Unit cost (ex tax)")
    def unit_cost_ex_tax(self, line):
        return line.unit_cost if line.pk else "-"

    @admin.display(description="Tax %")
    def tax(self, line):
        return line.tax_rate if line.pk else "-"

    @admin.display(description="Ordered")
    def ordered(self, line):
        return line.po_line.quantity_ordered if line.pk and line.po_line_id else "-"

    @admin.display(description="Received to date")
    def received_to_date(self, line):
        return selectors.received_quantity(line.po_line) if line.pk and line.po_line_id else "-"

    @admin.display(description="Outstanding")
    def outstanding(self, line):
        return selectors.outstanding_quantity(line.po_line) if line.pk and line.po_line_id else "-"


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(ModelAdmin):
    """Goods receipt business screen. Receipts start from a purchase order
    ("Receive goods"); status changes only through Post / Cancel, which
    call apps.purchases.services. Posted receipts are read-only."""

    inlines = [GoodsReceiptLineInline]
    list_display = (
        "grn_number", "receipt_type", "supplier", "purchase_order", "warehouse",
        "received_date", "status_label", "line_count", "total_units", "posted_at",
    )
    list_filter = ("status", "receipt_type", "supplier", "warehouse", "received_date")
    search_fields = (
        "grn_number", "purchase_order__po_number", "supplier__name",
        "supplier__vendor_code", "vendor_document_reference", "lines__variant__sku",
    )
    list_select_related = ("supplier", "warehouse", "purchase_order")
    actions_detail = ["post_grn", "cancel_grn"]
    EDITABLE_DRAFT_FIELDS = ("received_date", "vendor_document_reference", "notes")
    fieldsets = (
        ("Goods receipt", {"fields": (
            "number", "receipt_type", "status", "purchase_order", "supplier", "warehouse",
        )}),
        ("Delivery", {"fields": ("received_date", "vendor_document_reference", "notes")}),
        ("Purchase order lines", {"fields": ("po_summary",)}),
        ("Stock", {"fields": ("stock_transaction",)}),
        ("Audit", {"fields": GRN_AUDIT_FIELDS, "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _line_count=Count("lines"), _total_units=Sum("lines__quantity"),
        )

    def has_add_permission(self, request):
        # Receipts always start from a purchase order ("Receive goods").
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        fields = (
            "number", "receipt_type", "status", "purchase_order", "supplier", "warehouse",
            "po_summary", "stock_transaction", *GRN_AUDIT_FIELDS,
        )
        if obj is not None and obj.is_draft:
            return fields
        return fields + self.EDITABLE_DRAFT_FIELDS

    def get_inline_instances(self, request, obj=None):
        # The line inline needs the receipt's PO (to limit PO line choices)
        # and, on a new draft, the outstanding PO lines to offer as rows.
        request._grn_po_id = obj.purchase_order_id if obj else None
        prefill = []
        if request.method == "GET" and obj is not None and obj.is_draft and not obj.lines.exists():
            po_lines = list(PurchaseOrderLine.objects.filter(purchase_order_id=obj.purchase_order_id))
            received = selectors.received_quantities(line.pk for line in po_lines)
            prefill = [
                line.pk for line in po_lines
                if selectors.outstanding_quantity(line, received.get(line.pk, 0)) > 0
            ]
        request._grn_prefill = prefill
        return super().get_inline_instances(request, obj)

    def get_formset_kwargs(self, request, obj, inline, prefix):
        kwargs = super().get_formset_kwargs(request, obj, inline, prefix)
        # One blank row per outstanding PO line. Rows left without a
        # quantity are unchanged and never saved, so nothing is assumed
        # received.
        if request.method == "GET" and isinstance(inline, GoodsReceiptLineInline):
            kwargs["initial"] = [{"po_line": pk} for pk in getattr(request, "_grn_prefill", ())]
        return kwargs

    # --- display ---

    @display(description="Status", label={"Draft": "info", "Posted": "success", "Cancelled": "danger"})
    def status_label(self, obj):
        return obj.get_status_display()

    @admin.display(description="Lines", ordering="_line_count")
    def line_count(self, obj):
        return obj._line_count

    @admin.display(description="Units", ordering="_total_units")
    def total_units(self, obj):
        return obj._total_units or 0

    @admin.display(description="GRN number")
    def number(self, obj):
        return obj.grn_number if obj.pk else "Assigned on save"

    @admin.display(description="Ordered / received / outstanding")
    def po_summary(self, obj):
        if not obj.pk or not obj.purchase_order_id:
            return "-"
        po_lines = list(obj.purchase_order.lines.select_related("variant"))
        received = selectors.received_quantities(line.pk for line in po_lines)
        rows = format_html_join(
            "", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            (
                (
                    line.variant.sku, line.quantity_ordered, received.get(line.pk, 0),
                    selectors.outstanding_quantity(line, received.get(line.pk, 0)),
                )
                for line in po_lines
            ),
        )
        return format_html(
            "<table><tr><th>SKU</th><th>Ordered</th><th>Received</th>"
            "<th>Outstanding</th></tr>{}</table>",
            rows,
        )

    # --- lifecycle actions ---

    def _receipt_in_status(self, object_id, status):
        if object_id is None:
            return True
        return GoodsReceipt.objects.filter(pk=object_id, status=status).exists()

    def has_post_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.post_goodsreceipt") and self._receipt_in_status(
            object_id, GoodsReceipt.STATUS_DRAFT
        )

    def has_cancel_permission(self, request, object_id=None):
        return request.user.has_perm("purchases.cancel_goodsreceipt") and self._receipt_in_status(
            object_id, GoodsReceipt.STATUS_DRAFT
        )

    def _change_url(self, receipt):
        return reverse("admin:purchases_goodsreceipt_change", args=[receipt.pk])

    def _action_page(self, request, receipt, title, form, message, button_label, button_class, rows=()):
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "opts": self.model._meta,
            "form": form,
            "message": message,
            "rows": rows,
            "button_label": button_label,
            "button_class": button_class,
            "back_url": self._change_url(receipt),
        }
        return TemplateResponse(request, "admin/purchases/po_action.html", context)

    @action(description="Post receipt", url_path="post", permissions=["post"], icon="inventory")
    def post_grn(self, request, object_id):
        """GET shows what will be added to stock; only POST posts."""
        receipt = get_object_or_404(GoodsReceipt, pk=object_id)
        if request.method == "POST":
            try:
                po_services.post_goods_receipt(receipt, request.user)
            except po_services.GoodsReceiptAlreadyPosted as exc:
                self.message_user(request, str(exc), level=messages.INFO)
            except po_services.GoodsReceiptError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(
                    request, f"{receipt.grn_number} posted. Stock updated.", level=messages.SUCCESS
                )
            return redirect(self._change_url(receipt))
        rows = [
            f"{line.variant.sku}: +{line.quantity} to {line.get_stock_type_display().lower()} stock"
            for line in receipt.lines.select_related("variant")
        ]
        return self._action_page(
            request, receipt, f"Post {receipt.grn_number}", forms.Form(),
            f"Post goods receipt {receipt.grn_number} into {receipt.warehouse}. Stock increases "
            "immediately and the receipt can no longer be changed.",
            "Post receipt", "bg-primary-600", rows,
        )

    @action(description="Cancel", url_path="cancel", permissions=["cancel"], icon="cancel")
    def cancel_grn(self, request, object_id):
        receipt = get_object_or_404(GoodsReceipt, pk=object_id)
        form = CancelPurchaseOrderForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                po_services.cancel_goods_receipt(receipt, request.user, form.cleaned_data["reason"])
            except po_services.GoodsReceiptError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
            else:
                self.message_user(request, f"{receipt.grn_number} cancelled.", level=messages.SUCCESS)
            return redirect(self._change_url(receipt))
        return self._action_page(
            request, receipt, f"Cancel {receipt.grn_number}", form,
            f"Cancel draft goods receipt {receipt.grn_number}. No stock is affected.",
            "Cancel goods receipt", "bg-red-600",
        )
