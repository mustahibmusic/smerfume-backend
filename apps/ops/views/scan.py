"""Barcode / SKU scan lookup for the Smerfume Panel (read-only JSON).

GET /ops/lookup/items/?context=sale&warehouse=<public_id>&code=<scan>
GET /ops/lookup/items/?context=purchase&code=<scan>
GET /ops/lookup/items/?context=receipt&grn=<GRN number>&code=<scan>

One path for every screen: catalog.selectors.resolve_scan() identifies
the variant (exact barcode, then exact SKU as typed, then upper-case SKU);
the context then says whether that variant fits this transaction. The page
only adds or changes a line after a "found" answer, and every submit is
validated again by the workflow's own form and service, so this endpoint
never makes a transaction valid by itself. Nothing is written.

200 {"status": "found" | "not_found" | "ineligible", "message",
     "item": {"sku", "name", "barcode", ...context fields} | null}

- sale: active variants. Extra: "stock" (physical units the counter can
  sell now in that warehouse; booked incoming never counts) and "price".
  Permission: orders.add_order.
- purchase: active, non-decant variants; no stock condition. Permission:
  purchases.add_purchaseorder.
- receipt: the variant must be on the draft receipt's PO with units still
  outstanding. Extra: "line" (PO line public id), "ordered", "received"
  (posted so far), "outstanding". Permission: editing that receipt.

400 for an unknown context or missing parameter; 403 without permission;
404 for an unknown warehouse or receipt. No database ids are returned.
"""

import uuid

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.catalog.selectors import SCAN_CODE_MAX_LENGTH, resolve_scan
from apps.inventory.models import Warehouse
from apps.inventory.selectors import in_store_available
from apps.ops.mixins import OpsAccessMixin
from apps.ops.purchase_forms import purchasable_problem
from apps.purchases import rules
from apps.purchases.models import GoodsReceipt, PurchaseOrder
from apps.purchases.selectors import outstanding_quantity, received_quantities

CONTEXTS = ("sale", "purchase", "receipt")


def _answer(status, message="", item=None):
    return JsonResponse({"status": status, "message": message, "item": item})


def _item(variant, **extra):
    return {"sku": variant.sku, "name": str(variant), "barcode": variant.barcode or "", **extra}


class ItemScanView(OpsAccessMixin, View):
    http_method_names = ["get"]

    def get(self, request):
        context = request.GET.get("context", "")
        if context not in CONTEXTS:
            return JsonResponse({"error": "Unknown scan context."}, status=400)
        code = request.GET.get("code", "").strip()[:SCAN_CODE_MAX_LENGTH]
        handler = getattr(self, f"_{context}")
        return handler(request, code)

    # ── Contexts ──────────────────────────────────────────────────────────

    def _sale(self, request, code):
        if not request.user.has_perm("orders.add_order"):
            return JsonResponse({"error": "Not allowed."}, status=403)
        warehouse = get_object_or_404(Warehouse, public_id=_uuid(request.GET.get("warehouse")))
        variant = resolve_scan(code)
        if variant is None:
            return _answer("not_found", f"No item with barcode or SKU “{code}”.")
        if not variant.is_active:
            return _answer("ineligible", f"{variant} is inactive and cannot be sold.")
        stock = in_store_available([variant.pk], warehouse)[variant.pk]
        return _answer("found", item=_item(variant, stock=stock, price=str(variant.selling_price)))

    def _purchase(self, request, code):
        if not rules.can_create_po(request.user):
            return JsonResponse({"error": "Not allowed."}, status=403)
        variant = resolve_scan(code)
        if variant is None:
            return _answer("not_found", f"No item with barcode or SKU “{code}”.")
        problem = purchasable_problem(variant)  # same rule as the PO line form
        if problem:
            return _answer("ineligible", problem)
        return _answer("found", item=_item(variant))

    def _receipt(self, request, code):
        receipt = get_object_or_404(
            GoodsReceipt.objects.select_related("purchase_order"),
            grn_number=request.GET.get("grn", ""), receipt_type=GoodsReceipt.TYPE_STANDARD,
        )
        if not rules.can_edit_receipt(request.user, receipt):
            return JsonResponse({"error": "Not allowed."}, status=403)
        po = receipt.purchase_order
        if po.status not in PurchaseOrder.RECEIVABLE_STATUSES:
            return _answer("ineligible", f"{po.po_number} is {po.get_status_display().lower()}.")
        variant = resolve_scan(code)
        if variant is None:
            return _answer("not_found", f"No item with barcode or SKU “{code}”.")
        lines = list(po.lines.filter(variant=variant).order_by("pk"))
        if not lines:
            return _answer("ineligible", f"Not on {po.po_number}.")
        received = received_quantities([line.pk for line in lines])
        for line in lines:
            done = received.get(line.pk, 0)
            outstanding = outstanding_quantity(line, done)
            if outstanding > 0:
                return _answer("found", item=_item(
                    variant, line=str(line.public_id), ordered=line.quantity_ordered,
                    received=done, outstanding=outstanding,
                ))
        return _answer("ineligible", f"{variant} is fully received on {po.po_number}.")


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
