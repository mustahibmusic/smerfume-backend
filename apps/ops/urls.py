from django.urls import path, re_path

from apps.ops.views.auth import OpsLoginView, OpsLogoutView
from apps.ops.views.home import OpsHomeView, OpsNotFoundView
from apps.ops.views.orders import OrderDetailView, OrderListView

app_name = "ops"

urlpatterns = [
    path("", OpsHomeView.as_view(), name="home"),
    path("login/", OpsLoginView.as_view(), name="login"),
    path("logout/", OpsLogoutView.as_view(), name="logout"),
    path("orders/", OrderListView.as_view(), name="order_list"),
    path("orders/<str:order_number>/", OrderDetailView.as_view(), name="order_detail"),
    # Must stay last: branded 404 for any other /ops/ path.
    re_path(r"^.*$", OpsNotFoundView.as_view(), name="not_found"),
]
