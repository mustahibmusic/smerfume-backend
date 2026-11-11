from django.urls import path, re_path

from apps.ops.views import catalog, customers, inventory, list_pages, payments, purchase_drafts, purchases, refunds, returns, sales
from apps.ops.views.auth import OpsLoginView, OpsLogoutView
from apps.ops.views.home import OpsHomeView, OpsNotFoundView
from apps.ops.views.orders import OrderDetailView

app_name = "ops"

urlpatterns = [
    path("", OpsHomeView.as_view(), name="home"),
    path("login/", OpsLoginView.as_view(), name="login"),
    path("logout/", OpsLogoutView.as_view(), name="logout"),
    # Catalog
    path("catalog/products/", list_pages.ProductListPage.as_view(), name="product_list"),
    path("catalog/products/<slug:slug>/", catalog.ProductDetailView.as_view(), name="product_detail"),
    path("catalog/editions/", list_pages.EditionListPage.as_view(), name="edition_list"),
    path("catalog/editions/<uuid:public_id>/", catalog.EditionDetailView.as_view(), name="edition_detail"),
    path("catalog/variants/", list_pages.VariantListPage.as_view(), name="variant_list"),
    path(
        "catalog/variants/<str:sku>/images/upload/", catalog.GalleryUploadView.as_view(),
        name="variant_image_upload",
    ),
    path(
        "catalog/variants/<str:sku>/images/<uuid:image_id>/<slug:action>/",
        catalog.GalleryImageActionView.as_view(), name="variant_image_action",
    ),
    path("catalog/variants/<str:sku>/", catalog.VariantDetailView.as_view(), name="variant_detail"),
    path("catalog/brands/", list_pages.BrandListPage.as_view(), name="brand_list"),
    path("catalog/brands/<slug:slug>/", catalog.BrandDetailView.as_view(), name="brand_detail"),
    path("catalog/notes/", list_pages.PerfumeNoteListPage.as_view(), name="note_list"),
    path("catalog/notes/<uuid:public_id>/", catalog.PerfumeNoteDetailView.as_view(), name="note_detail"),
    # Customers
    path("customers/", list_pages.CustomerListPage.as_view(), name="customer_list"),
    path("customers/<uuid:public_id>/", customers.CustomerDetailView.as_view(), name="customer_detail"),
    path("users/", list_pages.UserListPage.as_view(), name="user_list"),
    path("users/<uuid:public_id>/", customers.UserDetailView.as_view(), name="user_detail"),
    # Sales
    path("orders/", list_pages.OrderListPage.as_view(), name="order_list"),
    path("orders/<str:order_number>/", OrderDetailView.as_view(), name="order_detail"),
    path(
        "orders/<str:order_number>/return/", returns.OrderCreateReturnView.as_view(),
        name="order_create_return",
    ),
    path(
        "orders/<str:order_number>/record-payment/", payments.OrderRecordPaymentView.as_view(),
        name="order_record_payment",
    ),
    path("sales/new/", sales.InStoreSaleView.as_view(), name="in_store_sale"),
    path("sales/items/", sales.InStoreItemSearchView.as_view(), name="in_store_items"),
    path("returns/", list_pages.ReturnListPage.as_view(), name="return_list"),
    path("returns/bulk/", list_pages.ReturnBulkView.as_view(), name="return_bulk"),
    path("returns/<uuid:public_id>/", returns.ReturnDetailView.as_view(), name="return_detail"),
    path(
        "returns/<uuid:public_id>/received-quantities/", returns.ReturnReceivedQuantitiesView.as_view(),
        name="return_received_quantities",
    ),
    path(
        "returns/<uuid:public_id>/items/<uuid:item_public_id>/inspect/",
        returns.ReturnItemInspectView.as_view(), name="return_item_inspect",
    ),
    path(
        "returns/<uuid:public_id>/<slug:action>/", returns.ReturnActionView.as_view(),
        name="return_action",
    ),
    path("refunds/", list_pages.RefundListPage.as_view(), name="refund_list"),
    path("refunds/bulk/", list_pages.RefundBulkView.as_view(), name="refund_bulk"),
    path("refunds/<uuid:public_id>/", refunds.RefundDetailView.as_view(), name="refund_detail"),
    path("refunds/<uuid:public_id>/method/", refunds.RefundMethodView.as_view(), name="refund_method"),
    path(
        "refunds/<uuid:public_id>/processing/", refunds.RefundProcessingView.as_view(),
        name="refund_processing",
    ),
    path("refunds/<uuid:public_id>/complete/", refunds.RefundCompleteView.as_view(), name="refund_complete"),
    path("refunds/<uuid:public_id>/fail/", refunds.RefundFailView.as_view(), name="refund_fail"),
    path(
        "refunds/<uuid:public_id>/adjustments/", refunds.RefundAdjustmentView.as_view(),
        name="refund_adjustment",
    ),
    # Inventory
    path("inventory/stock/", list_pages.StockListPage.as_view(), name="stock_list"),
    path("inventory/stock/<uuid:public_id>/", inventory.StockDetailView.as_view(), name="stock_detail"),
    path("inventory/stock/<uuid:public_id>/adjust/", inventory.StockAdjustView.as_view(), name="stock_adjust"),
    path(
        "inventory/stock/<uuid:public_id>/reorder-level/", inventory.StockReorderLevelView.as_view(),
        name="stock_reorder_level",
    ),
    path(
        "inventory/stock/<uuid:public_id>/transfer/", inventory.StockTransferView.as_view(),
        name="stock_transfer",
    ),
    path("inventory/partial-bottles/", list_pages.PartialLotListPage.as_view(), name="partial_lot_list"),
    path(
        "inventory/partial-bottles/<uuid:public_id>/", inventory.PartialLotDetailView.as_view(),
        name="partial_lot_detail",
    ),
    path(
        "inventory/partial-bottles/<uuid:public_id>/adjust/", inventory.PartialLotAdjustView.as_view(),
        name="partial_lot_adjust",
    ),
    path("inventory/movements/", list_pages.MovementListPage.as_view(), name="movement_list"),
    path("inventory/reservations/", list_pages.ReservationListPage.as_view(), name="reservation_list"),
    path(
        "inventory/reservations/<uuid:public_id>/", inventory.ReservationDetailView.as_view(),
        name="reservation_detail",
    ),
    path(
        "inventory/allocations/<uuid:public_id>/reallocate/", inventory.AllocationReallocateView.as_view(),
        name="allocation_reallocate",
    ),
    path("inventory/warehouses/", list_pages.WarehouseListPage.as_view(), name="warehouse_list"),
    # Purchases
    path("purchases/vendors/", list_pages.VendorListPage.as_view(), name="vendor_list"),
    path("purchases/vendors/new/", purchase_drafts.VendorFormView.as_view(), name="vendor_new"),
    path(
        "purchases/vendors/<str:vendor_code>/edit/", purchase_drafts.VendorFormView.as_view(),
        name="vendor_edit",
    ),
    path("purchases/vendors/<str:vendor_code>/", purchases.VendorDetailView.as_view(), name="vendor_detail"),
    path("purchases/orders/", list_pages.PurchaseOrderListPage.as_view(), name="po_list"),
    path("purchases/orders/new/", purchase_drafts.PurchaseOrderFormView.as_view(), name="po_new"),
    path("purchases/items/", purchase_drafts.PurchaseItemSearchView.as_view(), name="purchase_items"),
    path(
        "purchases/orders/<str:po_number>/edit/", purchase_drafts.PurchaseOrderFormView.as_view(),
        name="po_edit",
    ),
    path(
        "purchases/orders/<str:po_number>/receive/", purchase_drafts.ReceiveGoodsView.as_view(),
        name="po_receive",
    ),
    path("purchases/orders/<str:po_number>/", purchases.PurchaseOrderDetailView.as_view(), name="po_detail"),
    path("purchases/orders/<str:po_number>/issue/", purchases.PurchaseOrderIssueView.as_view(), name="po_issue"),
    path("purchases/orders/<str:po_number>/close/", purchases.PurchaseOrderCloseView.as_view(), name="po_close"),
    path("purchases/orders/<str:po_number>/cancel/", purchases.PurchaseOrderCancelView.as_view(), name="po_cancel"),
    path(
        "purchases/orders/<str:po_number>/tax-treatment/", purchases.PurchaseOrderTaxTreatmentView.as_view(),
        name="po_tax_treatment",
    ),
    path(
        "purchases/orders/<str:po_number>/confirm-booked/", purchases.PurchaseOrderConfirmBookedView.as_view(),
        name="po_confirm_booked",
    ),
    path(
        "purchases/orders/<str:po_number>/payments/new/", purchases.PaymentEntryView.as_view(),
        name="po_payment_new",
    ),
    path(
        "purchases/orders/<str:po_number>/vendor-refund/", purchases.PurchaseOrderVendorRefundView.as_view(),
        name="po_vendor_refund",
    ),
    path(
        "purchases/orders/<str:po_number>/reverse-entry/", purchases.PurchaseOrderReverseEntryView.as_view(),
        name="po_reverse_entry",
    ),
    path("purchases/payments/", list_pages.PaymentLedgerPage.as_view(), name="payment_list"),
    path("purchases/payments/bulk/", list_pages.PaymentBulkView.as_view(), name="payment_bulk"),
    path("purchases/payments/<uuid:public_id>/", purchases.PaymentEntryView.as_view(), name="payment_entry"),
    path(
        "purchases/attachments/<uuid:public_id>/", purchases.PaymentAttachmentDownloadView.as_view(),
        name="payment_attachment",
    ),
    path("purchases/paid-through/", list_pages.PaymentAccountListPage.as_view(), name="account_list"),
    path("purchases/paid-through/new/", purchase_drafts.PaymentAccountFormView.as_view(), name="account_new"),
    path(
        "purchases/paid-through/<uuid:public_id>/edit/", purchase_drafts.PaymentAccountFormView.as_view(),
        name="account_edit",
    ),
    path("purchases/receipts/", list_pages.GoodsReceiptListPage.as_view(), name="grn_list"),
    path("purchases/receipts/bulk/", list_pages.GoodsReceiptBulkView.as_view(), name="grn_bulk"),
    path("purchases/opening/new/", purchase_drafts.OpeningReceiptStartView.as_view(), name="opening_new"),
    path(
        "purchases/receipts/<str:grn_number>/edit/", purchase_drafts.GoodsReceiptEditView.as_view(),
        name="grn_edit",
    ),
    path("purchases/receipts/<str:grn_number>/", purchases.GoodsReceiptDetailView.as_view(), name="grn_detail"),
    path("purchases/receipts/<str:grn_number>/post/", purchases.GoodsReceiptPostView.as_view(), name="grn_post"),
    path(
        "purchases/receipts/<str:grn_number>/cancel/", purchases.GoodsReceiptCancelView.as_view(),
        name="grn_cancel",
    ),
    path(
        "purchases/receipts/<str:grn_number>/reverse/", purchases.GoodsReceiptReverseView.as_view(),
        name="grn_reverse",
    ),
    path("purchases/discrepancies/", list_pages.DiscrepancyListPage.as_view(), name="discrepancy_list"),
    path(
        "purchases/discrepancies/<uuid:public_id>/", purchases.DiscrepancyDetailView.as_view(),
        name="discrepancy_detail",
    ),
    path(
        "purchases/discrepancies/<uuid:public_id>/resolve/", purchases.DiscrepancyResolveView.as_view(),
        name="discrepancy_resolve",
    ),
    path("purchases/booked-history/", list_pages.BookedChangeListPage.as_view(), name="booked_list"),
    # Must stay last: branded 404 for any other /ops/ path.
    re_path(r"^.*$", OpsNotFoundView.as_view(), name="not_found"),
]
