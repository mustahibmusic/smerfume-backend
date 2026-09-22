from django.urls import path

from .views import (
    CancelReturnView,
    CheckoutView,
    CreateReturnView,
    InStoreSaleView,
    OrderDetailView,
    OrderListView,
    ReturnDetailView,
    ReturnListView,
)

urlpatterns = [
    path("", OrderListView.as_view(), name="order-list"),
    path("checkout/", CheckoutView.as_view(), name="checkout"),
    path("in-store/", InStoreSaleView.as_view(), name="in-store-sale"),
    # Literal "returns/..." patterns must precede <str:order_number>/ below —
    # otherwise Django would try to match "returns" itself as an order_number
    # first, since the str path converter matches any single non-slash segment.
    path("returns/", ReturnListView.as_view(), name="return-list"),
    path("returns/<uuid:public_id>/", ReturnDetailView.as_view(), name="return-detail"),
    path("returns/<uuid:public_id>/cancel/", CancelReturnView.as_view(), name="return-cancel"),
    path("<str:order_number>/returns/", CreateReturnView.as_view(), name="order-return-create"),
    path("<str:order_number>/", OrderDetailView.as_view(), name="order-detail"),
]
