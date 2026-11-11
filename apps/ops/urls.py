from django.urls import path, re_path

from apps.ops.views import inventory, payments, refunds, returns, sales
from apps.ops.views.auth import OpsLoginView, OpsLogoutView
from apps.ops.views.home import OpsHomeView, OpsNotFoundView
from apps.ops.views.orders import OrderDetailView, OrderListView

app_name = "ops"

urlpatterns = [
    path("", OpsHomeView.as_view(), name="home"),
    path("login/", OpsLoginView.as_view(), name="login"),
    path("logout/", OpsLogoutView.as_view(), name="logout"),
    # Sales
    path("orders/", OrderListView.as_view(), name="order_list"),
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
    path("returns/", returns.ReturnListView.as_view(), name="return_list"),
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
    path("refunds/", refunds.RefundListView.as_view(), name="refund_list"),
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
    path("inventory/stock/", inventory.StockListView.as_view(), name="stock_list"),
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
    path("inventory/partial-bottles/", inventory.PartialLotListView.as_view(), name="partial_lot_list"),
    path(
        "inventory/partial-bottles/<uuid:public_id>/", inventory.PartialLotDetailView.as_view(),
        name="partial_lot_detail",
    ),
    path(
        "inventory/partial-bottles/<uuid:public_id>/adjust/", inventory.PartialLotAdjustView.as_view(),
        name="partial_lot_adjust",
    ),
    path("inventory/movements/", inventory.MovementListView.as_view(), name="movement_list"),
    path("inventory/reservations/", inventory.ReservationListView.as_view(), name="reservation_list"),
    path(
        "inventory/reservations/<uuid:public_id>/", inventory.ReservationDetailView.as_view(),
        name="reservation_detail",
    ),
    path(
        "inventory/allocations/<uuid:public_id>/reallocate/", inventory.AllocationReallocateView.as_view(),
        name="allocation_reallocate",
    ),
    path("inventory/warehouses/", inventory.WarehouseListView.as_view(), name="warehouse_list"),
    # Must stay last: branded 404 for any other /ops/ path.
    re_path(r"^.*$", OpsNotFoundView.as_view(), name="not_found"),
]
