from django.urls import path, include

urlpatterns = [
    path("auth/", include("apps.accounts.urls")),
    # path("catalog/", include("apps.catalog.urls")),
    # path("inventory/", include("apps.inventory.urls")),
    # path("cart/", include("apps.cart.urls")),
    # path("orders/", include("apps.orders.urls")),
    # path("payments/", include("apps.payments.urls")),
    # path("shipping/", include("apps.shipping.urls")),
]
