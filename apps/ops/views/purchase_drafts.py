"""Smerfume Panel pages that create and edit purchase drafts and purchase
master data: purchase orders, goods receipt lines, opening stock, vendors
and Paid Through accounts.

Every save calls one service (apps.purchases.services or
apps.inventory.services.suppliers); nothing here writes a model. Access
follows apps.purchases.rules on both GET and POST.
"""

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.catalog.models import ProductVariant
from apps.inventory.models import Supplier
from apps.inventory.services import suppliers as supplier_services
from apps.ops.mixins import OpsAccessMixin, render_forbidden
from apps.ops.purchase_forms import (
    OpeningLineFormSet,
    OpeningStartForm,
    PaymentAccountForm,
    PurchaseOrderHeaderForm,
    PurchaseOrderLineFormSet,
    ReceiptDiscrepancyFormSet,
    ReceiptHeaderForm,
    ReceiptQuantitiesForm,
    ReceiveGoodsStartForm,
    VendorForm,
    discrepancy_initial,
    opening_line_initial,
    po_header_initial,
    po_line_initial,
    receipt_quantities_initial,
)
from apps.purchases import rules
from apps.purchases import services as po_services
from apps.purchases.models import GoodsReceipt, PaymentAccount, PurchaseOrder
from apps.purchases.selectors import outstanding_quantity, received_quantities

VIEW_PO = "purchases.view_purchaseorder"
VIEW_GRN = "purchases.view_goodsreceipt"


class PurchaseItemSearchView(OpsAccessMixin, View):
    """GET /ops/purchases/items/?q= : purchasable items (active, not
    decants) for SKU boxes on purchase forms. SKU and name only."""

    permission_required = (VIEW_PO,)
    http_method_names = ["get"]

    def get(self, request):
        terms = request.GET.get("q", "").split()[:5]
        variants = ProductVariant.objects.filter(is_active=True, is_decant=False).select_related(
            "edition__product__brand",
        )
        if not terms:
            return JsonResponse({"results": []})
        for term in terms:
            variants = variants.filter(
                Q(sku__icontains=term) | Q(barcode__iexact=term) | Q(edition__name__icontains=term)
                | Q(edition__product__name__icontains=term) | Q(edition__product__brand__name__icontains=term)
            )
        return JsonResponse({"results": [{"sku": v.sku, "text": str(v)} for v in variants.order_by("sku")[:20]]})


# ── Purchase orders ──────────────────────────────────────────────────────────


class PurchaseOrderFormView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/orders/new/ and
    /ops/purchases/orders/<po_number>/edit/ (drafts only)."""

    permission_required = (VIEW_PO,)
    nav_active = "purchase_orders"
    template_name = "ops/purchases/po_form.html"

    def _po(self, kwargs):
        if "po_number" not in kwargs:
            return None, (None if rules.can_create_po(self.request.user) else render_forbidden(self.request))
        po = get_object_or_404(PurchaseOrder.objects.select_related("supplier", "warehouse"),
                               po_number=kwargs["po_number"])
        return po, (None if rules.can_edit_po(self.request.user, po) else render_forbidden(self.request))

    def get(self, request, **kwargs):
        po, refused = self._po(kwargs)
        if refused:
            return refused
        header = PurchaseOrderHeaderForm(po=po, initial=po_header_initial(po) if po else None)
        lines = PurchaseOrderLineFormSet(prefix="lines", initial=po_line_initial(po) if po else None)
        return self.render_to_response(self.get_context_data(po=po, header=header, lines=lines))

    def post(self, request, **kwargs):
        po, refused = self._po(kwargs)
        if refused:
            return refused
        header = PurchaseOrderHeaderForm(request.POST, po=po)
        lines = PurchaseOrderLineFormSet(request.POST, prefix="lines")
        if header.is_valid() and lines.is_valid():
            try:
                if po is None:
                    po = po_services.create_purchase_order(
                        user=request.user, lines=lines.line_data(), **header.cleaned_data,
                    )
                    messages.success(request, f"Draft {po.po_number} created.")
                else:
                    po_services.update_draft_purchase_order(
                        po, user=request.user, lines=lines.line_data(), **header.cleaned_data,
                    )
                    messages.success(request, f"{po.po_number} saved.")
            except po_services.PurchaseOrderError as exc:
                header.add_error(None, str(exc))
            else:
                return redirect(reverse("ops:po_detail", args=[po.po_number]))
        return self.render_to_response(self.get_context_data(po=po, header=header, lines=lines))


# ── Goods receipts ───────────────────────────────────────────────────────────


class ReceiveGoodsView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/orders/<po_number>/receive/: start a draft
    receipt (create_receipt_from_po), then enter quantities on it."""

    permission_required = (VIEW_PO, VIEW_GRN)
    nav_active = "goods_receipts"
    template_name = "ops/purchases/receive_start.html"

    def _po(self, po_number):
        po = get_object_or_404(PurchaseOrder.objects.select_related("supplier"), po_number=po_number)
        return po, (None if rules.can_receive(self.request.user, po) else render_forbidden(self.request))

    def get(self, request, po_number):
        po, refused = self._po(po_number)
        return refused or self.render_to_response(self.get_context_data(po=po, form=ReceiveGoodsStartForm()))

    def post(self, request, po_number):
        po, refused = self._po(po_number)
        if refused:
            return refused
        form = ReceiveGoodsStartForm(request.POST)
        if form.is_valid():
            try:
                receipt = po_services.create_receipt_from_po(po, request.user, **form.cleaned_data)
            except po_services.GoodsReceiptError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Draft {receipt.grn_number} created. Enter what physically arrived.")
                return redirect(reverse("ops:grn_edit", args=[receipt.grn_number]))
        return self.render_to_response(self.get_context_data(po=po, form=form))


class OpeningReceiptStartView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/opening/new/: start a draft opening-stock
    receipt (create_opening_receipt). No vendor, no purchase order."""

    permission_required = (VIEW_GRN,)
    nav_active = "goods_receipts"
    template_name = "ops/purchases/opening_start.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not rules.can_create_opening(request.user):
            return render_forbidden(request)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return self.render_to_response(self.get_context_data(form=OpeningStartForm()))

    def post(self, request):
        form = OpeningStartForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            receipt = po_services.create_opening_receipt(
                data["warehouse"], request.user, received_date=data["received_date"], notes=data["notes"],
            )
            messages.success(request, f"Draft opening receipt {receipt.grn_number} created. Add the stock lines.")
            return redirect(reverse("ops:grn_edit", args=[receipt.grn_number]))
        return self.render_to_response(self.get_context_data(form=form))


class GoodsReceiptEditView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/receipts/<grn_number>/edit/ (drafts only).

    PO receipt: quantities per PO line (retail / damaged) and short /
    wrong item / excess discrepancies, through update_goods_receipt_lines.
    Opening receipt: stock lines through update_opening_receipt_lines.
    Stock changes only when the receipt is posted."""

    permission_required = (VIEW_GRN,)
    nav_active = "goods_receipts"

    def get_template_names(self):
        if self.receipt.receipt_type == GoodsReceipt.TYPE_OPENING:
            return ["ops/purchases/opening_edit.html"]
        return ["ops/purchases/grn_edit.html"]

    def _load(self, grn_number):
        self.receipt = get_object_or_404(
            GoodsReceipt.objects.select_related("purchase_order", "supplier", "warehouse"), grn_number=grn_number,
        )
        if not rules.can_edit_receipt(self.request.user, self.receipt):
            return render_forbidden(self.request)
        return None

    def _po_lines(self):
        return list(self.receipt.purchase_order.lines.select_related("variant").order_by("pk"))

    def _header_initial(self):
        r = self.receipt
        return {"received_date": r.received_date, "vendor_document_reference": r.vendor_document_reference,
                "notes": r.notes}

    def get(self, request, grn_number):
        refused = self._load(grn_number)
        if refused:
            return refused
        header = ReceiptHeaderForm(initial=self._header_initial())
        if self.receipt.receipt_type == GoodsReceipt.TYPE_OPENING:
            lines = OpeningLineFormSet(prefix="lines", initial=opening_line_initial(self.receipt))
            return self.render_to_response(self.get_context_data(header=header, lines=lines))
        po_lines = self._po_lines()
        quantities = ReceiptQuantitiesForm(po_lines=po_lines, initial=receipt_quantities_initial(self.receipt))
        discrepancies = ReceiptDiscrepancyFormSet(
            prefix="disc", po_lines=po_lines, initial=discrepancy_initial(self.receipt),
        )
        return self.render_to_response(self.get_context_data(
            header=header, quantities=quantities, discrepancies=discrepancies,
        ))

    def post(self, request, grn_number):
        refused = self._load(grn_number)
        if refused:
            return refused
        header = ReceiptHeaderForm(request.POST)
        receipt = self.receipt
        if receipt.receipt_type == GoodsReceipt.TYPE_OPENING:
            lines = OpeningLineFormSet(request.POST, prefix="lines")
            if header.is_valid() and lines.is_valid():
                header_data = {k: v for k, v in header.cleaned_data.items() if k != "vendor_document_reference"}
                try:
                    po_services.update_opening_receipt_lines(
                        receipt, user=request.user, lines=lines.line_data(), **header_data,
                    )
                except po_services.GoodsReceiptError as exc:
                    header.add_error(None, str(exc))
                else:
                    messages.success(request, f"{receipt.grn_number} saved. Post it to add the stock.")
                    return redirect(reverse("ops:grn_detail", args=[receipt.grn_number]))
            return self.render_to_response(self.get_context_data(header=header, lines=lines))

        po_lines = self._po_lines()
        quantities = ReceiptQuantitiesForm(request.POST, po_lines=po_lines)
        discrepancies = ReceiptDiscrepancyFormSet(request.POST, prefix="disc", po_lines=po_lines)
        if header.is_valid() and quantities.is_valid() and discrepancies.is_valid():
            try:
                po_services.update_goods_receipt_lines(
                    receipt, user=request.user, lines=quantities.line_data(),
                    discrepancies=discrepancies.discrepancy_data(), **header.cleaned_data,
                )
            except po_services.GoodsReceiptError as exc:
                header.add_error(None, str(exc))
            else:
                messages.success(request, f"{receipt.grn_number} saved. Post it to add the stock.")
                return redirect(reverse("ops:grn_detail", args=[receipt.grn_number]))
        return self.render_to_response(self.get_context_data(
            header=header, quantities=quantities, discrepancies=discrepancies,
        ))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["receipt"] = self.receipt
        quantities = kwargs.get("quantities")
        if quantities is not None:
            # Posted receipts so far and what is still outstanding, per PO
            # line (display; the service re-checks over-receipt on save).
            received = received_quantities([line.pk for line in quantities.po_lines])
            context["receive_rows"] = [
                (line, retail, damaged, received.get(line.pk, 0),
                 outstanding_quantity(line, received.get(line.pk, 0)))
                for line, retail, damaged in quantities.rows()
            ]
        return context


# ── Vendors and Paid Through accounts ────────────────────────────────────────


class VendorFormView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/vendors/new/ and /vendors/<vendor_code>/edit/
    through create_supplier / update_supplier."""

    permission_required = ("inventory.view_supplier",)
    nav_active = "vendors"
    template_name = "ops/purchases/master_form.html"

    def _vendor(self, kwargs):
        if "vendor_code" not in kwargs:
            return None, (None if rules.can_add_vendor(self.request.user) else render_forbidden(self.request))
        vendor = get_object_or_404(Supplier, vendor_code=kwargs["vendor_code"])
        return vendor, (None if rules.can_change_vendor(self.request.user) else render_forbidden(self.request))

    def get(self, request, **kwargs):
        vendor, refused = self._vendor(kwargs)
        return refused or self.render_to_response(self.get_context_data(
            vendor=vendor, form=VendorForm(instance=vendor),
        ))

    def post(self, request, **kwargs):
        vendor, refused = self._vendor(kwargs)
        if refused:
            return refused
        form = VendorForm(request.POST, instance=Supplier.objects.get(pk=vendor.pk) if vendor else None)
        if form.is_valid():
            try:
                if vendor is None:
                    vendor = supplier_services.create_supplier(user=request.user, **form.cleaned_data)
                else:
                    vendor = supplier_services.update_supplier(vendor, user=request.user, **form.cleaned_data)
            except supplier_services.SupplierError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Vendor {vendor.name} saved.")
                return redirect(reverse("ops:vendor_detail", args=[vendor.vendor_code]))
        return self.render_to_response(self.get_context_data(vendor=vendor, form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vendor = kwargs.get("vendor")
        context.update({
            "title": f"Edit {vendor.name}" if vendor else "New vendor",
            "back_url": reverse("ops:vendor_detail", args=[vendor.vendor_code]) if vendor else reverse("ops:vendor_list"),
        })
        return context


class PaymentAccountFormView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/paid-through/new/ and
    /paid-through/<public_id>/edit/ through create/update_payment_account."""

    permission_required = ("purchases.view_paymentaccount",)
    nav_active = "paid_through"
    template_name = "ops/purchases/master_form.html"

    def _account(self, kwargs):
        if "public_id" not in kwargs:
            return None, (None if rules.can_add_account(self.request.user) else render_forbidden(self.request))
        account = get_object_or_404(PaymentAccount, public_id=kwargs["public_id"])
        return account, (None if rules.can_change_account(self.request.user) else render_forbidden(self.request))

    def get(self, request, **kwargs):
        account, refused = self._account(kwargs)
        return refused or self.render_to_response(self.get_context_data(
            account=account, form=PaymentAccountForm(instance=account),
        ))

    def post(self, request, **kwargs):
        account, refused = self._account(kwargs)
        if refused:
            return refused
        form = PaymentAccountForm(
            request.POST, instance=PaymentAccount.objects.get(pk=account.pk) if account else None,
        )
        if form.is_valid():
            data = form.cleaned_data
            try:
                if account is None:
                    po_services.create_payment_account(user=request.user, **data)
                else:
                    po_services.update_payment_account(account, user=request.user, **data)
            except po_services.PaymentAccountError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Paid Through account {data['name']} saved.")
                return redirect(reverse("ops:account_list"))
        return self.render_to_response(self.get_context_data(account=account, form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account = kwargs.get("account")
        context.update({
            "title": f"Edit {account.name}" if account else "New Paid Through account",
            "back_url": reverse("ops:account_list"),
            "help": "Use the ledger account name from Zoho Books. No balances or bank details are stored.",
        })
        return context
