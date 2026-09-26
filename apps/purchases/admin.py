from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminTextareaWidget

from apps.inventory.models import Supplier

from . import services as po_services
from .models import PurchaseOrder, PurchaseOrderLine

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
        "effective_unit_cost_ex_tax",
    )
    readonly_fields = ("gross_line_amount", "taxable_value", "effective_unit_cost_ex_tax")

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


class CancelPurchaseOrderForm(forms.Form):
    reason = forms.CharField(widget=UnfoldAdminTextareaWidget, label="Reason for cancelling")


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
    actions_detail = ["issue_po", "cancel_po"]
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

    def _change_url(self, po):
        return reverse("admin:purchases_purchaseorder_change", args=[po.pk])

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
