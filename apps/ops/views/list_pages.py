"""Row actions, quick view and registered bulk actions for every live
Smerfume Panel list (see apps.ops.list_actions).

Each list subclasses its existing view and says, per row, which actions
this user may run now. The decisions reuse the domain rules
(apps.purchases.rules, the shared Sales helpers, model permissions and the
statuses the services enforce); every POST lands on an existing view or a
BulkActionView that calls a domain service. Immutable records (posted
payments, stock movements, booked-quantity history) get Quick view and
their domain actions only, never Edit or Delete.
"""

from urllib.parse import quote

from django.urls import reverse

from apps.ops.list_actions import (
    ActionListMixin,
    BulkAction,
    BulkActionView,
    RowAction,
    delete_action,
    quick_view,
    row_quick_view,
)
from apps.ops.views import catalog, customers, inventory, purchases, refunds, returns
from apps.ops.views.orders import OrderListView
from apps.ops.views.returns import RETURN_ACTIONS, order_can_create_return
from apps.orders import payments as order_payments
from apps.orders import services as order_services
from apps.orders.models import Order, Refund, Return
from apps.purchases import rules
from apps.purchases import services as po_services
from apps.purchases.models import GoodsReceipt, PurchaseOrderPayment

# ── Sales ────────────────────────────────────────────────────────────────────


class OrderListPage(ActionListMixin, OrderListView):
    ops_key_field = "order_number"

    def prepare_rows(self, objects, user):
        self._payable = set()
        if user.has_perm("orders.add_orderpayment"):
            self._payable = set(order_payments.orders_open_for_manual_payment(
                Order.objects.filter(pk__in=[o.pk for o in objects])
            ).values_list("pk", flat=True))

    def row_actions(self, order, user):
        actions = [quick_view(reverse("ops:order_detail", args=[order.order_number]))]
        if order.pk in self._payable:
            actions.append(RowAction("Record payment", reverse("ops:order_record_payment", args=[order.order_number])))
        if order_can_create_return(order, user):
            actions.append(RowAction("Create return", reverse("ops:order_create_return", args=[order.order_number])))
        return actions


def _return_bulk(key, done):
    service, error_cls, permission, statuses, label, _success, confirm = RETURN_ACTIONS[key]
    return BulkAction(
        key=key, label=label, done=done, errors=(error_cls,),
        confirm=confirm or f"{label} the selected returns?",
        allowed=lambda user: user.has_perm(permission),
        eligible=lambda obj, user: obj.status in statuses,
        run=lambda obj, user: service(obj),
    )


class ReturnListPage(ActionListMixin, returns.ReturnListView):
    bulk_url_name = "ops:return_bulk"
    bulk_actions = (
        _return_bulk("approve", "Approved"),
        _return_bulk("in_transit", "Marked in transit"),
    )

    def row_actions(self, obj, user):
        actions = [quick_view(reverse("ops:return_detail", args=[obj.public_id]))]
        for key, (_s, _e, permission, statuses, label, _m, confirm) in RETURN_ACTIONS.items():
            if obj.status in statuses and user.has_perm(permission):
                actions.append(RowAction(
                    label, reverse("ops:return_action", args=[obj.public_id, key]), method="post",
                    confirm=confirm or f"{label}?", danger=key in ("reject", "cancel"),
                ))
        return actions


class ReturnBulkView(BulkActionView):
    permission_required = ("orders.view_return",)
    list_view = ReturnListPage
    list_view_url_name = "ops:return_list"

    def queryset(self):
        return Return.objects.all()


def _ready_for_processing(refund, user):
    return refund.refund_status == Refund.STATUS_PENDING and bool(refund.refund_method)


class RefundListPage(ActionListMixin, refunds.RefundListView):
    bulk_url_name = "ops:refund_bulk"
    bulk_actions = (
        BulkAction(
            key="processing", label="Mark processing", done="Marked processing",
            confirm="Mark the selected refunds as processing? Their refund method can no longer be changed.",
            errors=(order_services.RefundError,),
            allowed=lambda user: user.has_perm("orders.process_refund"),
            eligible=_ready_for_processing,
            run=lambda obj, user: order_services.mark_refund_processing(obj),
        ),
    )

    def row_actions(self, obj, user):
        url = reverse("ops:refund_detail", args=[obj.public_id])
        actions = [quick_view(url)]
        if user.has_perm("orders.process_refund") and _ready_for_processing(obj, user):
            actions.append(RowAction(
                "Mark processing", reverse("ops:refund_processing", args=[obj.public_id]), method="post",
                confirm="Mark this refund as processing? The refund method can no longer be changed.",
            ))
        if obj.refund_status == Refund.STATUS_PROCESSING and user.has_perm("orders.complete_refund"):
            actions.append(RowAction("Complete or fail", url))
        return actions


class RefundBulkView(BulkActionView):
    permission_required = ("orders.view_refund",)
    list_view = RefundListPage
    list_view_url_name = "ops:refund_list"

    def queryset(self):
        return Refund.objects.all()


# ── Purchases ────────────────────────────────────────────────────────────────


class VendorListPage(ActionListMixin, purchases.VendorListView):
    ops_key_field = "vendor_code"

    def row_actions(self, obj, user):
        actions = [quick_view(reverse("ops:vendor_detail", args=[obj.vendor_code]))]
        if rules.can_change_vendor(user):
            actions.append(RowAction("Edit", reverse("ops:vendor_edit", args=[obj.vendor_code]), kind="edit"))
        return actions


class PurchaseOrderListPage(ActionListMixin, purchases.PurchaseOrderListView):
    ops_key_field = "po_number"

    def row_actions(self, po, user):
        actions = [quick_view(reverse("ops:po_detail", args=[po.po_number]))]
        if rules.can_edit_po(user, po):
            actions.append(RowAction("Edit", reverse("ops:po_edit", args=[po.po_number]), kind="edit"))
        if rules.can_issue(user, po):
            actions.append(RowAction("Issue", reverse("ops:po_issue", args=[po.po_number])))
        if rules.can_receive(user, po):
            actions.append(RowAction("Receive goods", reverse("ops:po_receive", args=[po.po_number])))
        if rules.can_cancel(user, po):
            actions.append(RowAction("Cancel", reverse("ops:po_cancel", args=[po.po_number])))
        return actions


def _is_deletable_draft(entry, user):
    return entry.status == PurchaseOrderPayment.STATUS_DRAFT and user.has_perm("purchases.record_po_payment")


class PaymentLedgerPage(ActionListMixin, purchases.PaymentLedgerView):
    bulk_url_name = "ops:payment_bulk"
    bulk_actions = (
        BulkAction(
            key="delete_drafts", label="Delete drafts", done="Deleted drafts", danger=True,
            confirm="Delete the selected draft payments? Posted payments are never deleted.",
            errors=(po_services.SupplierPaymentError,),
            allowed=lambda user: user.has_perm("purchases.record_po_payment"),
            eligible=_is_deletable_draft,
            run=lambda obj, user: po_services.delete_payment_draft(obj, user),
        ),
    )

    def base_queryset(self):
        return super().base_queryset().select_related("reversal")

    def row_actions(self, entry, user):
        url = reverse("ops:payment_entry", args=[entry.public_id])
        actions = [quick_view(url)]
        if entry.status == PurchaseOrderPayment.STATUS_DRAFT:
            if user.has_perm("purchases.record_po_payment") or user.has_perm("purchases.post_po_payment"):
                actions.append(RowAction("Edit draft", url, kind="edit"))
            if _is_deletable_draft(entry, user):
                actions.append(delete_action(
                    "Delete draft", url, "Delete this draft payment? This cannot be undone.", (("_delete", "1"),),
                ))
        elif (
            entry.entry_type in (PurchaseOrderPayment.TYPE_PAYMENT, PurchaseOrderPayment.TYPE_VENDOR_REFUND)
            and user.has_perm("purchases.reverse_po_payment")
            and not hasattr(entry, "reversal")
        ):
            actions.append(RowAction("Reverse", reverse("ops:po_reverse_entry", args=[entry.purchase_order.po_number])))
        return actions


class PaymentBulkView(BulkActionView):
    permission_required = ("purchases.view_purchaseorderpayment",)
    list_view = PaymentLedgerPage
    list_view_url_name = "ops:payment_list"

    def queryset(self):
        return PurchaseOrderPayment.objects.select_related("purchase_order")


class PaymentAccountListPage(ActionListMixin, purchases.PaymentAccountListView):
    def row_actions(self, obj, user):
        actions = [row_quick_view()]
        if rules.can_change_account(user):
            actions.append(RowAction("Edit", reverse("ops:account_edit", args=[obj.public_id]), kind="edit"))
        return actions


def _post_receipt(receipt, user):
    post = (
        po_services.post_opening_receipt if receipt.receipt_type == GoodsReceipt.TYPE_OPENING
        else po_services.post_goods_receipt
    )
    return post(receipt, user)


class GoodsReceiptListPage(ActionListMixin, purchases.GoodsReceiptListView):
    ops_key_field = "grn_number"
    bulk_url_name = "ops:grn_bulk"
    bulk_actions = (
        BulkAction(
            key="post", label="Post receipts", done="Posted",
            confirm="Post the selected draft receipts? Stock increases now and they can no longer be changed.",
            errors=(po_services.GoodsReceiptError,),
            allowed=lambda user: user.has_perm("purchases.post_goodsreceipt"),
            eligible=lambda obj, user: rules.can_post_receipt(user, obj),
            run=_post_receipt,
        ),
    )

    def row_actions(self, receipt, user):
        actions = [quick_view(reverse("ops:grn_detail", args=[receipt.grn_number]))]
        if rules.can_edit_receipt(user, receipt):
            actions.append(RowAction("Edit", reverse("ops:grn_edit", args=[receipt.grn_number]), kind="edit"))
        if rules.can_post_receipt(user, receipt):
            actions.append(RowAction(
                "Post", reverse("ops:grn_post", args=[receipt.grn_number]), method="post",
                confirm=f"Post {receipt.grn_number}? Stock increases now and the receipt can no longer be changed.",
            ))
        return actions


class GoodsReceiptBulkView(BulkActionView):
    permission_required = ("purchases.view_goodsreceipt",)
    list_view = GoodsReceiptListPage
    list_view_url_name = "ops:grn_list"

    def queryset(self):
        return GoodsReceipt.objects.all()


class DiscrepancyListPage(ActionListMixin, purchases.DiscrepancyListView):
    def row_actions(self, obj, user):
        url = reverse("ops:discrepancy_detail", args=[obj.public_id])
        actions = [quick_view(url)]
        if rules.can_resolve_discrepancy(user, obj):
            actions.append(RowAction("Resolve", url))
        return actions


class BookedChangeListPage(ActionListMixin, purchases.BookedChangeListView):
    def row_actions(self, obj, user):
        return [row_quick_view()]


# ── Inventory ────────────────────────────────────────────────────────────────


class StockListPage(ActionListMixin, inventory.StockListView):
    def row_actions(self, stock, user):
        url = reverse("ops:stock_detail", args=[stock.public_id])
        actions = [quick_view(url)]
        if user.has_perm("inventory.change_inventorystock") or user.has_perm("inventory.transfer_stock_type"):
            actions.append(RowAction("Adjust", url))
        return actions


class PartialLotListPage(ActionListMixin, inventory.PartialLotListView):
    def row_actions(self, lot, user):
        url = reverse("ops:partial_lot_detail", args=[lot.public_id])
        actions = [quick_view(url)]
        if not lot.is_depleted and user.has_perm("inventory.change_partialbottlelot"):
            actions.append(RowAction("Adjust", url))
        return actions


class MovementListPage(ActionListMixin, inventory.MovementListView):
    def row_actions(self, movement, user):
        return [row_quick_view()]


class ReservationListPage(ActionListMixin, inventory.ReservationListView):
    def row_actions(self, reservation, user):
        return [quick_view(reverse("ops:reservation_detail", args=[reservation.public_id]))]


class WarehouseListPage(ActionListMixin, inventory.WarehouseListView):
    def row_actions(self, warehouse, user):
        return [row_quick_view()]



# ── Catalog (read-only; gallery changes are made on the variant page) ────────


class BrandListPage(ActionListMixin, catalog.BrandListView):
    ops_key_field = "slug"

    def row_actions(self, brand, user):
        return [quick_view(reverse("ops:brand_detail", args=[brand.slug]))]


class PerfumeNoteListPage(ActionListMixin, catalog.PerfumeNoteListView):
    def row_actions(self, note, user):
        return [quick_view(reverse("ops:note_detail", args=[note.public_id]))]


class ProductListPage(ActionListMixin, catalog.ProductListView):
    ops_key_field = "slug"

    def row_actions(self, product, user):
        return [quick_view(reverse("ops:product_detail", args=[product.slug]))]


class EditionListPage(ActionListMixin, catalog.EditionListView):
    def row_actions(self, edition, user):
        return [quick_view(reverse("ops:edition_detail", args=[edition.public_id]))]


class VariantListPage(ActionListMixin, catalog.VariantListView):
    ops_key_field = "sku"

    def row_actions(self, variant, user):
        url = reverse("ops:variant_detail", args=[variant.sku])
        actions = [quick_view(url)]
        if user.has_perm(catalog.VIEW_IMAGE):
            actions.append(RowAction("Manage images", url + "#gallery", kind="view"))
        return actions


# ── Customers (read-only) ────────────────────────────────────────────────────


class CustomerListPage(ActionListMixin, customers.CustomerListView):
    def row_actions(self, customer, user):
        actions = [quick_view(reverse("ops:customer_detail", args=[customer.public_id]))]
        search = customer.mobile_number or customer.email
        if search and user.has_perm("orders.view_order"):
            actions.append(RowAction("Orders", f"{reverse('ops:order_list')}?q={quote(search)}", kind="view"))
        return actions


class UserListPage(ActionListMixin, customers.UserListView):
    def row_actions(self, account, user):
        actions = [quick_view(reverse("ops:user_detail", args=[account.public_id]))]
        if customers.is_customer(account) and user.has_perm(customers.VIEW_CUSTOMER):
            actions.append(RowAction("Customer", reverse("ops:customer_detail", args=[account.public_id]), kind="view"))
        return actions
