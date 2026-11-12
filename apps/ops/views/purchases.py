"""Purchases pages for the Smerfume Panel.

Same data, permissions and action rules as the Django Admin purchase pages:
action availability comes from apps.purchases.rules (shared with admin),
forms from apps.purchases.forms, and every write is an
apps.purchases.services call. Supplier payments keep DEC-010/011: drafts
never count, Save as Paid needs post_po_payment, posted entries are only
reversed, proof files are private downloads.

Addressed by business identifiers: vendor code, PO number, GRN number;
payments, attachments and discrepancies by public id.
"""

from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.inventory.models import Supplier
from apps.ops.forms import (
    BookedChangeFilterForm,
    DiscrepancyFilterForm,
    GoodsReceiptFilterForm,
    PaymentLedgerFilterForm,
    PurchaseOrderFilterForm,
    VendorFilterForm,
)
from apps.ops.mixins import FilteredListMixin, OpsAccessMixin, render_forbidden, render_not_found
from apps.purchases import rules, selectors
from apps.purchases import services as po_services
from apps.purchases.forms import (
    CancelPurchaseOrderForm,
    ClosePurchaseOrderForm,
    ConfirmBookedFormSet,
    ConfirmBookedNoteForm,
    PaymentProofForm,
    ResolveDiscrepancyForm,
    ReverseGoodsReceiptForm,
    ReversePaymentEntryForm,
    SetTaxTreatmentForm,
    SupplierPaymentEntryForm,
    SupplierPaymentForm,
)
from apps.purchases.models import (
    PAYMENT_STATUS_CHOICES,
    BookedQuantityChange,
    GoodsReceipt,
    PaymentAccount,
    PurchaseOrder,
    PurchaseOrderPayment,
    ReceiptDiscrepancy,
    SupplierPaymentAttachment,
    payment_status_for,
)

VIEW_PO = "purchases.view_purchaseorder"
VIEW_PAYMENTS = "purchases.view_purchaseorderpayment"
VIEW_GRN = "purchases.view_goodsreceipt"
VARIANT_RELATED = "variant__edition__product__brand"
PAYMENT_STATUS_LABELS = dict(PAYMENT_STATUS_CHOICES)


def _po_or_404(po_number):
    return get_object_or_404(PurchaseOrder.objects.select_related("supplier", "warehouse"), po_number=po_number)


def _po_url(po):
    return reverse("ops:po_detail", args=[po.po_number])


def _grn_url(receipt):
    return reverse("ops:grn_detail", args=[receipt.grn_number])


def _messages_from(request, form):
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for error in errors:
            messages.error(request, f"{label}: {error}" if label else error)


def _run(request, call, error_classes, success):
    """Call one service; report success or its refusal as a message."""
    try:
        result = call()
    except error_classes as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, success)
    return result


# ── Vendors (read-only) ──────────────────────────────────────────────────────


class VendorListView(OpsAccessMixin, FilteredListMixin, ListView):
    permission_required = ("inventory.view_supplier",)
    nav_active = "vendors"
    template_name = "ops/purchases/vendor_list.html"
    filter_form_class = VendorFilterForm

    def base_queryset(self):
        return Supplier.objects.annotate(po_count=Count("purchase_orders")).order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_add_vendor"] = rules.can_add_vendor(self.request.user)
        return context

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(name__icontains=q) | Q(vendor_code__icontains=q) | Q(legal_name__icontains=q)
                | Q(gstin__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
            )
        if data.get("state") == "active":
            queryset = queryset.filter(is_active=True)
        elif data.get("state") == "inactive":
            queryset = queryset.filter(is_active=False)
        return queryset


class VendorDetailView(OpsAccessMixin, TemplateView):
    permission_required = ("inventory.view_supplier",)
    nav_active = "vendors"
    template_name = "ops/purchases/vendor_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        vendor = get_object_or_404(Supplier, vendor_code=kwargs["vendor_code"])
        can_view_pos = self.request.user.has_perm(VIEW_PO)
        context.update({
            "can_change_vendor": rules.can_change_vendor(self.request.user),
            "vendor": vendor,
            "can_view_pos": can_view_pos,
            "orders": vendor.purchase_orders.select_related("warehouse").order_by("-order_date", "-id")[:20]
            if can_view_pos else [],
        })
        return context


# ── Purchase orders ──────────────────────────────────────────────────────────


class PurchaseOrderListView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/purchases/orders/?q=&status=&payment_status=&vendor=&overdue=on"""

    permission_required = (VIEW_PO,)
    nav_active = "purchase_orders"
    template_name = "ops/purchases/po_list.html"
    filter_form_class = PurchaseOrderFilterForm

    def base_queryset(self):
        return selectors.annotate_payment_summary(
            PurchaseOrder.objects.select_related("supplier", "warehouse").annotate(line_count=Count("lines"))
        ).order_by("-order_date", "-id")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(po_number__icontains=q) | Q(supplier__name__icontains=q)
                | Q(supplier__vendor_code__icontains=q) | Q(vendor_reference__icontains=q)
                | Q(lines__variant__sku__iexact=q)
            ).distinct()
        if data.get("status"):
            queryset = queryset.filter(status=data["status"])
        if data.get("vendor"):
            queryset = queryset.filter(supplier=data["vendor"])
        if data.get("payment_status") and self.request.user.has_perm(VIEW_PAYMENTS):
            queryset = queryset.filter(payment_status_db=data["payment_status"])
        if data.get("overdue"):
            queryset = selectors.overdue_purchase_orders(queryset)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_view_payments"] = self.request.user.has_perm(VIEW_PAYMENTS)
        context["payment_status_labels"] = PAYMENT_STATUS_LABELS
        context["can_create_po"] = rules.can_create_po(self.request.user)
        return context


class PurchaseOrderDetailView(OpsAccessMixin, TemplateView):
    """GET /ops/purchases/orders/<po_number>/: lines with received and
    booking figures, receipts, supplier payments (DEC-010, only with the
    payment view permission) and the actions this user may run now."""

    permission_required = (VIEW_PO,)
    nav_active = "purchase_orders"
    template_name = "ops/purchases/po_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = _po_or_404(kwargs["po_number"])
        user = self.request.user
        lines = list(po.lines.select_related(VARIANT_RELATED).order_by("pk"))
        figures = selectors.booking_figures(po, lines)
        received = selectors.received_quantities([line.pk for line in lines])
        for line in lines:
            line.figures = figures.get(line.pk)
            line.received_total = received.get(line.pk, 0)
            line.outstanding = selectors.outstanding_quantity(line, line.received_total)
        can_view_payments = user.has_perm(VIEW_PAYMENTS)
        payments = None
        if can_view_payments:
            summary = po.payment_summary()
            ordered = po.ordered_gross_total
            payments = {
                "summary": summary,
                "ordered": ordered,
                "remaining": None if ordered is None else ordered - summary["net_paid"],
                "status": PAYMENT_STATUS_LABELS[payment_status_for(summary["net_paid"], ordered)],
                "entries": po.payment_entries.select_related("paid_through", "reverses").order_by(
                    "entry_date", "pk",
                ),
            }
        context.update({
            "po": po,
            "lines": lines,
            "receipts": po.receipts.order_by("-received_date", "-id") if user.has_perm(VIEW_GRN) else None,
            "open_discrepancies": selectors.open_discrepancy_count(po),
            "actions": rules.po_actions(user, po),
            "payments": payments,
        })
        return context


class _PurchaseOrderActionView(OpsAccessMixin, TemplateView):
    """GET shows a confirmation form; POST runs one service. ``rule`` from
    apps.purchases.rules gates both, as in admin."""

    template_name = "ops/purchases/po_action.html"
    nav_active = "purchase_orders"
    permission_required = (VIEW_PO,)
    rule = None
    form_class = None
    title = ""
    message = ""
    button = ""
    danger = False

    def _guard(self, po_number):
        po = _po_or_404(po_number)
        if not self.rule(self.request.user, po):
            return po, render_forbidden(self.request)
        return po, None

    def get_form(self, po, data=None):
        return self.form_class(data) if self.form_class else None

    def get(self, request, po_number):
        po, refused = self._guard(po_number)
        if refused:
            return refused
        return self.render_to_response(self.get_context_data(po=po, form=self.get_form(po)))

    def post(self, request, po_number):
        po, refused = self._guard(po_number)
        if refused:
            return refused
        form = self.get_form(po, request.POST)
        if form is not None and not form.is_valid():
            return self.render_to_response(self.get_context_data(po=po, form=form))
        self.perform(request, po, form.cleaned_data if form is not None else {})
        return redirect(_po_url(po))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = kwargs["po"]
        context.update({
            "title": self.title.format(po=po),
            "message": self.message.format(po=po),
            "button": self.button,
            "danger": self.danger,
            "back_url": _po_url(po),
        })
        return context


class PurchaseOrderIssueView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_issue)
    title = "Issue {po.po_number}"
    message = ("Issue purchase order {po.po_number} to {po.supplier}. After issuing, prices, quantities "
               "and lines can no longer be changed.")
    button = "Issue purchase order"

    def perform(self, request, po, data):
        _run(request, lambda: po_services.issue_purchase_order(po, request.user),
             po_services.PurchaseOrderError, f"{po.po_number} issued.")


class PurchaseOrderCloseView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_close)
    form_class = ClosePurchaseOrderForm
    title = "Close {po.po_number}"
    message = ("Close purchase order {po.po_number}. Smerfume stops waiting for the quantity not yet "
               "received. No more goods can be received and it cannot be cancelled. Received stock is "
               "not affected.")
    button = "Close purchase order"
    danger = True

    def perform(self, request, po, data):
        _run(request, lambda: po_services.close_purchase_order(po, request.user, data["reason"]),
             po_services.PurchaseOrderError, f"{po.po_number} closed.")


class PurchaseOrderCancelView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_cancel)
    form_class = CancelPurchaseOrderForm
    title = "Cancel {po.po_number}"
    message = ("Cancel purchase order {po.po_number} for {po.supplier}. This cannot be undone. The order "
               "stays on record as cancelled.")
    button = "Cancel purchase order"
    danger = True

    def perform(self, request, po, data):
        _run(request, lambda: po_services.cancel_purchase_order(po, request.user, data["reason"]),
             po_services.PurchaseOrderError, f"{po.po_number} cancelled.")


class PurchaseOrderTaxTreatmentView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_set_tax_treatment)
    form_class = SetTaxTreatmentForm
    title = "Set tax treatment for {po.po_number}"
    message = ("This PO was created before purchase tax treatment existed. Choose it once; it is checked "
               "against the PO lines and cannot be changed afterwards.")
    button = "Set tax treatment"

    def perform(self, request, po, data):
        _run(request, lambda: po_services.set_po_tax_treatment(po, data["treatment"], request.user),
             po_services.PurchaseOrderError, f"{po.po_number} classified.")


class PurchaseOrderVendorRefundView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_record_vendor_refund)
    title = "Record vendor refund on {po.po_number}"
    message = ("Record money the supplier actually returned. It reduces net paid and cannot exceed it. "
               "To correct a wrongly entered payment, use Reverse entry instead.")
    button = "Record vendor refund"

    def get_form(self, po, data=None):
        return SupplierPaymentForm(data, with_paid_through=False)

    def perform(self, request, po, data):
        _run(request, lambda: po_services.record_vendor_refund(
            po, amount=data["amount"], entry_date=data["entry_date"], method=data["method"],
            reference=data["reference"], notes=data["notes"], user=request.user,
        ), po_services.SupplierPaymentError, f"Recorded vendor refund on {po.po_number}.")


class PurchaseOrderReverseEntryView(_PurchaseOrderActionView):
    rule = staticmethod(rules.can_reverse_payment_entry)
    title = "Reverse an entry on {po.po_number}"
    message = ("Use this only when a payment or vendor refund was recorded incorrectly. The full amount "
               "is reversed and the original entry stays on record.")
    button = "Reverse entry"
    danger = True

    def get_form(self, po, data=None):
        return ReversePaymentEntryForm(data, po=po)

    def perform(self, request, po, data):
        _run(request, lambda: po_services.reverse_po_payment_entry(data["entry"], data["reason"], request.user),
             po_services.SupplierPaymentError, f"Entry reversed on {po.po_number}.")


class PurchaseOrderConfirmBookedView(OpsAccessMixin, TemplateView):
    """GET/POST /ops/purchases/orders/<po_number>/confirm-booked/: every line
    in one table, saved through set_confirmed_booked_quantities() (all or
    nothing, one shared note), as in admin."""

    permission_required = (VIEW_PO,)
    nav_active = "purchase_orders"
    template_name = "ops/purchases/confirm_booked.html"

    def _load(self, po_number):
        po = _po_or_404(po_number)
        if not rules.can_confirm_booked(self.request.user, po):
            return po, None, render_forbidden(self.request)
        return po, list(po.lines.select_related(VARIANT_RELATED).order_by("pk")), None

    def get(self, request, po_number):
        po, lines, refused = self._load(po_number)
        if refused:
            return refused
        formset = ConfirmBookedFormSet(prefix="lines", po_lines=lines, initial=[
            {"line": line.pk, "original_total": line.confirmed_booked_quantity,
             "confirmed_total": line.confirmed_booked_quantity}
            for line in lines
        ])
        return self._render(po, lines, formset, ConfirmBookedNoteForm())

    def post(self, request, po_number):
        po, lines, refused = self._load(po_number)
        if refused:
            return refused
        formset = ConfirmBookedFormSet(request.POST, prefix="lines", po_lines=lines)
        note_form = ConfirmBookedNoteForm(request.POST)
        if formset.is_valid() and note_form.is_valid():
            totals, originals = formset.totals(), formset.originals()
            changed = [line_id for line_id, total in totals.items() if total != originals[line_id]]
            note = note_form.cleaned_data["note"].strip()
            if not changed:
                messages.info(request, "No confirmed totals changed.")
                return redirect(_po_url(po))
            if not note:
                note_form.add_error("note", "A note about the vendor confirmation is required.")
            else:
                try:
                    changes = po_services.set_confirmed_booked_quantities(
                        po, totals, request.user, note, expected=originals,
                    )
                except po_services.BookedQuantityBatchError as exc:
                    for form in formset.forms:
                        message = exc.errors.get(form.cleaned_data["line"])
                        if message:
                            form.add_error("confirmed_total", message)
                except po_services.BookedQuantityError as exc:
                    note_form.add_error(None, str(exc))
                else:
                    messages.success(request, f"Vendor-confirmed totals saved for {len(changes)} line(s).")
                    return redirect(_po_url(po))
        return self._render(po, lines, formset, note_form)

    def _render(self, po, lines, formset, note_form):
        figures = selectors.booking_figures(po, lines)
        by_id = {line.pk: line for line in lines}
        rows = []
        for form in formset.forms:
            try:
                line = by_id.get(int(form["line"].value()))
            except (TypeError, ValueError):
                line = None
            figure = figures.get(line.pk) if line else None
            minimum = figure["received"] + figure["allocated"] if figure else None
            rows.append({"form": form, "line": line, "figures": figure, "minimum": minimum})
        return self.render_to_response(self.get_context_data(
            po=po, formset=formset, note_form=note_form, rows=rows,
        ))


# ── Supplier payments (DEC-010 / DEC-011) ────────────────────────────────────


class PaymentEntryView(OpsAccessMixin, TemplateView):
    """New payment: /ops/purchases/orders/<po_number>/payments/new/.
    Existing entry: /ops/purchases/payments/<public_id>/.

    Drafts are created, edited, posted (Save as Paid) or deleted; posted
    entries are read-only and only take extra proof. Drafting needs
    record_po_payment, posting needs post_po_payment, as in admin."""

    permission_required = (VIEW_PO, VIEW_PAYMENTS)
    nav_active = "supplier_payments"
    template_name = "ops/purchases/payment_entry.html"

    def _load(self, kwargs):
        if "public_id" in kwargs:
            entry = get_object_or_404(
                PurchaseOrderPayment.objects.select_related("purchase_order__supplier", "paid_through"),
                public_id=kwargs["public_id"],
            )
            return entry.purchase_order, entry, None
        po = _po_or_404(kwargs["po_number"])
        if not rules.can_record_payment(self.request.user, po):
            return po, None, render_forbidden(self.request)
        return po, None, None

    def _state(self, entry):
        user = self.request.user
        can_draft = user.has_perm("purchases.record_po_payment")
        can_post = user.has_perm("purchases.post_po_payment")
        editable = entry is None or entry.is_draft
        is_payment = entry is None or entry.entry_type == PurchaseOrderPayment.TYPE_PAYMENT
        return {
            "can_draft": can_draft,
            "can_post": can_post,
            "editable": editable,
            "is_draft": entry is not None and entry.is_draft,
            "can_add_proof": is_payment and (can_draft if editable else (can_draft or can_post)),
        }

    def _form(self, po, entry, state, existing, data=None, files=None):
        if state["editable"]:
            if entry is not None:
                initial = {
                    "amount": entry.amount, "entry_date": entry.entry_date, "method": entry.method,
                    "reference": entry.reference, "notes": entry.notes, "paid_through": entry.paid_through,
                }
            else:
                remaining = po.remaining_against_po
                initial = {"amount": remaining} if remaining is not None and remaining > 0 else None
            return SupplierPaymentEntryForm(
                data, files, initial=initial, existing_count=len(existing),
                current_account=entry.paid_through if entry else None,
                posting=bool(data) and "_post" in data,
            )
        return PaymentProofForm(data, files, existing_count=len(existing))

    def get(self, request, **kwargs):
        po, entry, refused = self._load(kwargs)
        if refused:
            return refused
        state = self._state(entry)
        existing = list(entry.attachments.all()) if entry else []
        return self._render(po, entry, state, existing, self._form(po, entry, state, existing))

    def post(self, request, **kwargs):
        po, entry, refused = self._load(kwargs)
        if refused:
            return refused
        state = self._state(entry)
        if not (state["can_draft"] or state["can_post"]):
            return render_forbidden(request)
        existing = list(entry.attachments.all()) if entry else []
        here = request.get_full_path()
        if "_delete" in request.POST or "_remove_attachment" in request.POST:
            if not (state["is_draft"] and state["can_draft"]):
                return render_forbidden(request)
            return self._housekeeping(request, po, entry, existing, here)
        if state["editable"]:
            posting = "_post" in request.POST
            if not (state["can_post"] if posting else state["can_draft"]):
                return render_forbidden(request)
        elif not state["can_add_proof"]:
            return render_forbidden(request)
        form = self._form(po, entry, state, existing, request.POST, request.FILES)
        if form.is_valid():
            saved = self._save(request, po, entry, form, state["editable"], "_post" in request.POST)
            if saved is not None:
                return saved
        return self._render(po, entry, state, existing, form, files_lost=bool(request.FILES))

    def _housekeeping(self, request, po, entry, existing, here):
        try:
            if "_delete" in request.POST:
                po_services.delete_payment_draft(entry, request.user)
            else:
                wanted = request.POST["_remove_attachment"]
                attachment = next((a for a in existing if str(a.public_id) == wanted), None)
                if attachment is None:
                    raise po_services.SupplierPaymentError("This attachment no longer exists.")
                po_services.remove_payment_attachment(attachment, request.user)
        except po_services.SupplierPaymentError as exc:
            messages.error(request, str(exc))
            return redirect(here)
        if "_delete" in request.POST:
            messages.success(request, f"Draft payment deleted from {po.po_number}.")
            return redirect(_po_url(po))
        messages.success(request, "Attachment removed.")
        return redirect(here)

    def _save(self, request, po, entry, form, editable, posting):
        data = dict(form.cleaned_data)
        files = data.pop("attachments", [])
        try:
            with transaction.atomic():
                if not editable:
                    po_services.add_payment_attachments(entry, files, request.user)
                    saved, verb = entry, "Added proof to"
                else:
                    fields = {k: data[k] for k in ("amount", "entry_date", "method", "reference", "notes",
                                                   "paid_through")}
                    if posting:
                        saved = po_services.post_payment(po, user=request.user, draft=entry, **fields)
                        verb = "Recorded payment"
                    elif entry is not None:
                        saved = po_services.update_payment_draft(entry, user=request.user, **fields)
                        verb = "Updated draft payment"
                    else:
                        saved = po_services.create_payment_draft(po, user=request.user, **fields)
                        verb = "Saved draft payment"
                    if files:
                        po_services.add_payment_attachments(saved, files, request.user)
        except po_services.SupplierPaymentError as exc:
            form.add_error(None, str(exc))
            return None
        label = saved.payment_number or "draft"
        messages.success(request, f"{verb} {label} (₹{saved.amount}) on {po.po_number}.")
        return redirect(_po_url(po))

    def _render(self, po, entry, state, existing, form, files_lost=False):
        net_paid = po.payment_summary()["net_paid"]
        ordered = po.ordered_gross_total
        drafts = po.payment_entries.drafts().aggregate(count=Count("pk"), total=Sum("amount"))
        return self.render_to_response(self.get_context_data(
            po=po, entry=entry, form=form, files_lost=files_lost, attachments=existing,
            summary={
                "status": PAYMENT_STATUS_LABELS[payment_status_for(net_paid, ordered)],
                "ordered": ordered,
                "net_paid": net_paid,
                "remaining": None if ordered is None else ordered - net_paid,
                "draft_count": drafts["count"],
                "draft_total": drafts["total"] or Decimal("0"),
            },
            **state,
        ))


class PaymentAttachmentDownloadView(OpsAccessMixin, View):
    """GET /ops/purchases/attachments/<public_id>/: private proof file, as
    a download only, to users who may view supplier payments (DEC-011)."""

    permission_required = (VIEW_PAYMENTS,)
    http_method_names = ["get"]

    def get(self, request, public_id):
        attachment = get_object_or_404(SupplierPaymentAttachment, public_id=public_id)
        try:
            handle = attachment.file.open("rb")
        except FileNotFoundError:
            return render_not_found(request)
        response = FileResponse(
            handle, as_attachment=True, filename=attachment.original_name, content_type=attachment.content_type,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class PaymentLedgerView(OpsAccessMixin, FilteredListMixin, ListView):
    """GET /ops/purchases/payments/: every supplier payment entry (drafts
    included, marked) with filters. Read-only; entries open their page."""

    permission_required = (VIEW_PAYMENTS,)
    nav_active = "supplier_payments"
    template_name = "ops/purchases/payment_list.html"
    filter_form_class = PaymentLedgerFilterForm

    def base_queryset(self):
        return PurchaseOrderPayment.objects.select_related(
            "purchase_order__supplier", "paid_through",
        ).order_by("-entry_date", "-pk")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(payment_number__icontains=q) | Q(purchase_order__po_number__icontains=q)
                | Q(reference__icontains=q) | Q(purchase_order__supplier__name__icontains=q)
            )
        for field in ("status", "entry_type", "method", "paid_through"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset


class PaymentAccountListView(OpsAccessMixin, ListView):
    """GET /ops/purchases/paid-through/ (create/edit through the account services)."""

    permission_required = ("purchases.view_paymentaccount",)
    nav_active = "paid_through"
    template_name = "ops/purchases/account_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_add_account"] = rules.can_add_account(self.request.user)
        context["can_change_account"] = rules.can_change_account(self.request.user)
        return context

    def get_queryset(self):
        return PaymentAccount.objects.annotate(
            posted_count=Count("payments", filter=Q(payments__status=PurchaseOrderPayment.STATUS_POSTED)),
        ).order_by("-is_active", "kind", "name")


# ── Goods receipts ───────────────────────────────────────────────────────────


class GoodsReceiptListView(OpsAccessMixin, FilteredListMixin, ListView):
    permission_required = (VIEW_GRN,)
    nav_active = "goods_receipts"
    template_name = "ops/purchases/grn_list.html"
    filter_form_class = GoodsReceiptFilterForm

    def base_queryset(self):
        return GoodsReceipt.objects.select_related("supplier", "warehouse", "purchase_order").annotate(
            line_count=Count("lines"), total_units=Sum("lines__quantity"),
        ).order_by("-received_date", "-id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_create_opening"] = rules.can_create_opening(self.request.user)
        return context

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(grn_number__icontains=q) | Q(purchase_order__po_number__icontains=q)
                | Q(supplier__name__icontains=q) | Q(vendor_document_reference__icontains=q)
            )
        for field in ("status", "receipt_type"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset


def _grn_or_404(grn_number):
    return get_object_or_404(
        GoodsReceipt.objects.select_related("supplier", "warehouse", "purchase_order", "reverses"),
        grn_number=grn_number,
    )


class GoodsReceiptDetailView(OpsAccessMixin, TemplateView):
    permission_required = (VIEW_GRN,)
    nav_active = "goods_receipts"
    template_name = "ops/purchases/grn_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        receipt = _grn_or_404(kwargs["grn_number"])
        user = self.request.user
        context.update({
            "receipt": receipt,
            "lines": receipt.lines.select_related(VARIANT_RELATED).order_by("pk"),
            "discrepancies": receipt.discrepancies.select_related("variant", "po_line__variant").order_by("pk"),
            "reversals": receipt.reversals.order_by("id"),
            "can_post": rules.can_post_receipt(user, receipt),
            "can_cancel": rules.can_cancel_receipt(user, receipt),
            "can_reverse": rules.can_reverse_receipt(user, receipt),
            "can_edit": rules.can_edit_receipt(user, receipt),
            "cancel_form": CancelPurchaseOrderForm() if rules.can_cancel_receipt(user, receipt) else None,
        })
        return context


class GoodsReceiptPostView(OpsAccessMixin, View):
    """POST: post a draft receipt (stock increases) through
    post_goods_receipt / post_opening_receipt."""

    permission_required = (VIEW_GRN,)
    http_method_names = ["post"]

    def post(self, request, grn_number):
        receipt = _grn_or_404(grn_number)
        if not rules.can_post_receipt(request.user, receipt):
            return render_forbidden(request)
        is_opening = receipt.receipt_type == GoodsReceipt.TYPE_OPENING
        post = po_services.post_opening_receipt if is_opening else po_services.post_goods_receipt
        try:
            post(receipt, request.user)
        except po_services.GoodsReceiptAlreadyPosted as exc:
            messages.info(request, str(exc))
        except po_services.GoodsReceiptError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{receipt.grn_number} posted. Stock updated.")
        return redirect(_grn_url(receipt))


class GoodsReceiptCancelView(OpsAccessMixin, View):
    permission_required = (VIEW_GRN,)
    http_method_names = ["post"]

    def post(self, request, grn_number):
        receipt = _grn_or_404(grn_number)
        if not rules.can_cancel_receipt(request.user, receipt):
            return render_forbidden(request)
        form = CancelPurchaseOrderForm(request.POST)
        if form.is_valid():
            _run(request, lambda: po_services.cancel_goods_receipt(receipt, request.user, form.cleaned_data["reason"]),
                 po_services.GoodsReceiptError, f"{receipt.grn_number} cancelled.")
        else:
            _messages_from(request, form)
        return redirect(_grn_url(receipt))


class GoodsReceiptReverseView(OpsAccessMixin, TemplateView):
    """GET shows reversible lines; POST posts a reversal receipt through
    reverse_goods_receipt(). The original receipt is never changed."""

    permission_required = (VIEW_GRN,)
    nav_active = "goods_receipts"
    template_name = "ops/purchases/grn_reverse.html"

    def _load(self, grn_number, data=None):
        receipt = _grn_or_404(grn_number)
        if not rules.can_reverse_receipt(self.request.user, receipt):
            return receipt, None, render_forbidden(self.request)
        lines = list(receipt.lines.select_related("variant").order_by("pk"))
        form = ReverseGoodsReceiptForm(data, lines=lines, reversible=selectors.reversible_quantities(receipt))
        return receipt, form, None

    def get(self, request, grn_number):
        receipt, form, refused = self._load(grn_number)
        return refused or self.render_to_response(self.get_context_data(receipt=receipt, form=form))

    def post(self, request, grn_number):
        receipt, form, refused = self._load(grn_number, request.POST)
        if refused:
            return refused
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(receipt=receipt, form=form))
        reversal = _run(request, lambda: po_services.reverse_goods_receipt(
            receipt, request.user, form.cleaned_data["reason"], form.quantities(),
        ), po_services.GoodsReceiptError, f"Reversal posted for {receipt.grn_number}. Stock reduced.")
        return redirect(_grn_url(reversal or receipt))


# ── Receipt discrepancies ────────────────────────────────────────────────────


class DiscrepancyListView(OpsAccessMixin, FilteredListMixin, ListView):
    """Posted receipts only (drafts are edited on their receipt), as in admin."""

    permission_required = ("purchases.view_receiptdiscrepancy",)
    nav_active = "receipt_discrepancies"
    template_name = "ops/purchases/discrepancy_list.html"
    filter_form_class = DiscrepancyFilterForm

    def base_queryset(self):
        return ReceiptDiscrepancy.objects.filter(receipt__status=GoodsReceipt.STATUS_POSTED).select_related(
            "receipt__purchase_order", "receipt__supplier", "variant", "po_line__variant",
        ).order_by("status", "-created_at", "-id")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(receipt__grn_number__icontains=q) | Q(receipt__purchase_order__po_number__icontains=q)
                | Q(variant__sku__icontains=q) | Q(po_line__variant__sku__icontains=q)
                | Q(observed_item_description__icontains=q)
            )
        for field in ("status", "discrepancy_type"):
            if data.get(field):
                queryset = queryset.filter(**{field: data[field]})
        return queryset


class DiscrepancyDetailView(OpsAccessMixin, TemplateView):
    permission_required = ("purchases.view_receiptdiscrepancy",)
    nav_active = "receipt_discrepancies"
    template_name = "ops/purchases/discrepancy_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        discrepancy = get_object_or_404(
            ReceiptDiscrepancy.objects.filter(receipt__status=GoodsReceipt.STATUS_POSTED).select_related(
                "receipt__purchase_order", "receipt__supplier", "variant", "po_line__variant",
                "created_by", "resolved_by",
            ),
            public_id=kwargs["public_id"],
        )
        context.update({
            "discrepancy": discrepancy,
            "resolve_form": ResolveDiscrepancyForm()
            if rules.can_resolve_discrepancy(self.request.user, discrepancy) else None,
        })
        return context


class DiscrepancyResolveView(OpsAccessMixin, View):
    """POST: record the outcome through resolve_discrepancy() (no stock or
    accounting effect)."""

    permission_required = ("purchases.view_receiptdiscrepancy",)
    http_method_names = ["post"]

    def post(self, request, public_id):
        discrepancy = get_object_or_404(
            ReceiptDiscrepancy.objects.filter(receipt__status=GoodsReceipt.STATUS_POSTED).select_related("receipt"),
            public_id=public_id,
        )
        if not rules.can_resolve_discrepancy(request.user, discrepancy):
            return render_forbidden(request)
        form = ResolveDiscrepancyForm(request.POST)
        if form.is_valid():
            _run(request, lambda: po_services.resolve_discrepancy(
                discrepancy, request.user, form.cleaned_data["resolution"], form.cleaned_data["notes"],
            ), po_services.ReceiptDiscrepancyError, "Discrepancy resolved.")
        else:
            _messages_from(request, form)
        return redirect(reverse("ops:discrepancy_detail", args=[discrepancy.public_id]))


# ── Booked quantity history (read-only) ──────────────────────────────────────


class BookedChangeListView(OpsAccessMixin, FilteredListMixin, ListView):
    permission_required = ("purchases.view_bookedquantitychange",)
    nav_active = "booked_quantity_history"
    template_name = "ops/purchases/booked_list.html"
    filter_form_class = BookedChangeFilterForm

    def base_queryset(self):
        return BookedQuantityChange.objects.select_related(
            "purchase_order_line__purchase_order", "purchase_order_line__" + VARIANT_RELATED, "performed_by",
        ).order_by("-created_at", "-id")

    def filter_queryset(self, queryset, data):
        if data.get("q"):
            q = data["q"]
            queryset = queryset.filter(
                Q(purchase_order_line__purchase_order__po_number__icontains=q)
                | Q(purchase_order_line__variant__sku__icontains=q) | Q(note__icontains=q)
            )
        return queryset
