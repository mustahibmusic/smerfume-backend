from django.urls import path
from drf_spectacular.utils import extend_schema
from rest_framework_simplejwt.views import TokenRefreshView

from .views import LogoutView, MeView, RequestOTPView, ResendOTPView, VerifyOTPView


# Subclass to attach the Auth tag so token/refresh appears grouped correctly
# in Swagger instead of an untagged section.
class _TaggedTokenRefreshView(TokenRefreshView):
    @extend_schema(
        tags=["Auth"],
        summary="Refresh access token",
        description=(
            "Exchange a valid refresh token for a new access token.\n\n"
            "With `ROTATE_REFRESH_TOKENS=True` (enabled) a new refresh token is also "
            "issued and the old one is blacklisted immediately."
        ),
        auth=[],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


urlpatterns = [
    path("otp/request/", RequestOTPView.as_view(), name="otp_request"),
    path("otp/verify/", VerifyOTPView.as_view(), name="otp_verify"),
    path("otp/resend/", ResendOTPView.as_view(), name="otp_resend"),
    path("token/refresh/", _TaggedTokenRefreshView.as_view(), name="token_refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
]
