from django.urls import path
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
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
        examples=[
            OpenApiExample(
                "Refresh request",
                request_only=True,
                value={"refresh": "<refresh_token>"},
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=TokenRefreshSerializer,
                description="New access token, plus a rotated refresh token.",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"access": "<new_access_token>", "refresh": "<new_refresh_token>"},
                    )
                ],
            ),
            401: OpenApiResponse(
                description="Refresh token is invalid, expired, or already blacklisted.",
                examples=[
                    OpenApiExample(
                        "Expired token",
                        value={"detail": "Token is expired", "code": "token_not_valid"},
                    ),
                    OpenApiExample(
                        "Blacklisted token",
                        value={"detail": "Token is blacklisted", "code": "token_not_valid"},
                    ),
                ],
            ),
        },
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
