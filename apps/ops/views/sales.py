"""New In-store Sale for the Smerfume Panel.

The same workflow as Django Admin's "New in-store sale": shared form rules
(apps.orders.forms) and the one write path,
apps.orders.services.create_in_store_order(). Nothing is recorded here.
"""

import uuid

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from apps.catalog.models import ProductVariant
from apps.inventory.models import Warehouse
from apps.inventory.services.reservation import InsufficientStockError
from apps.ops.forms import OpsInStoreSaleForm, OpsInStoreSaleLineFormSet
from apps.ops.mixins import OpsAccessMixin
from apps.orders import services as order_services
from apps.orders.forms import ITEM_SEARCH_LIMIT, counter_item_rows, search_counter_items

ADD_ORDER = "orders.add_order"


class InStoreSaleView(OpsAccessMixin, TemplateView):
    """GET shows an empty sale; POST records it through
    create_in_store_order(). Any failure re-renders the form and records
    nothing. The sale records no payment (DEC-013)."""

    permission_required = (ADD_ORDER,)
    nav_active = "in_store_sale"
    template_name = "ops/sales/new.html"

    def get(self, request, *args, **kwargs):
        form = OpsInStoreSaleForm(initial={"submission_key": uuid.uuid4()})
        formset = OpsInStoreSaleLineFormSet(prefix="lines")
        return self.render_to_response(self.get_context_data(form=form, formset=formset))

    def post(self, request, *args, **kwargs):
        form = OpsInStoreSaleForm(request.POST)
        formset = OpsInStoreSaleLineFormSet(request.POST, prefix="lines")
        if form.is_valid() and formset.is_valid() and formset.check_stock(form.cleaned_data["warehouse"]):
            data = form.cleaned_data
            try:
                order = order_services.create_in_store_order(
                    customer_mobile=data["customer_mobile"],
                    customer_name=data["customer_name"],
                    lines=formset.lines(),
                    discount_amount=data["discount_amount"] or 0,
                    warehouse=data["warehouse"],
                    notes=data["notes"],
                    staff_user=request.user,
                    submission_key=data["submission_key"],
                )
            except order_services.DuplicateInStoreSale as exc:
                messages.info(request, str(exc))
                return redirect(reverse("ops:order_detail", args=[exc.order.order_number]))
            except (order_services.InStoreSaleError, InsufficientStockError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    f"In-store sale {order.order_number} recorded (total ₹{order.total}). "
                    "No payment is recorded yet.",
                )
                return redirect(reverse("ops:order_detail", args=[order.order_number]))
        return self.render_to_response(self.get_context_data(form=form, formset=formset))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        formset = context["formset"]
        skus = {(f["sku"].value() or "").strip() for f in formset.forms} - {""}
        labels = {
            v.sku: str(v)
            for v in ProductVariant.objects.filter(sku__in=skus).select_related("edition__product__brand")
        }
        for line in formset.forms:
            # Display only: the chosen item's name under its SKU box.
            line.item_label = labels.get((line["sku"].value() or "").strip(), "")
        return context


class InStoreItemSearchView(OpsAccessMixin, View):
    """GET /ops/sales/items/?warehouse=<public_id>&q=... (read-only JSON).

    Up to 20 items, exact barcode/SKU first; ``?skus=A,B`` refreshes stock
    for chosen lines. Rows carry SKU, name, barcode, physical stock and
    price; never database ids. Same helpers as the admin picker."""

    permission_required = (ADD_ORDER,)
    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        warehouse = Warehouse.objects.filter(public_id=_uuid_or_none(request.GET.get("warehouse"))).first()
        if warehouse is None:
            return JsonResponse({"error": "Choose a valid warehouse."}, status=400)
        if "skus" in request.GET:
            skus = [s.strip() for s in request.GET["skus"].split(",") if s.strip()][:50]
            variants = ProductVariant.objects.filter(sku__in=skus).select_related("edition__product__brand")
            return JsonResponse({"results": counter_item_rows(variants, warehouse), "more": False})
        query = request.GET.get("q", "")[:100]
        variants = search_counter_items(query)
        return JsonResponse({
            "results": counter_item_rows(variants[:ITEM_SEARCH_LIMIT], warehouse, query),
            "more": len(variants) > ITEM_SEARCH_LIMIT,
        })


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
