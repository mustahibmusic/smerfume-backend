"""
Authentication views for the accounts app.

All OTP endpoints return HTTP 200 on success and 400 on client errors.
The RequestOTPView always returns 200 even for unknown mobile numbers —
this prevents account enumeration (an attacker cannot tell whether a
number is registered by observing the response status).
"""

import logging
import secrets

from django.contrib.auth import get_user_model
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    RequestOTPSerializer,
    ResendOTPSerializer,
    UpdateMeSerializer,
    UserMeSerializer,
    VerifyOTPSerializer,
)
from .services.otp import create_otp_record, resend_otp, verify_otp_record

logger = logging.getLogger(__name__)
User = get_user_model()


class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"

    @extend_schema(
        tags=["Auth"],
        summary="Request OTP",
        description=(
            "Send a one-time password to the provided Indian mobile number. "
            "Creates a new user account if the number is not yet registered.\n\n"
            "Always returns HTTP 200 regardless of whether the number exists — "
            "this prevents account enumeration attacks."
        ),
        request=RequestOTPSerializer,
        responses={
            200: OpenApiResponse(
                description="OTP dispatched. Use `otp_session_token` in the verify call.",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "otp_session_token": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "expires_in": 300,
                            "resend_available_in": 30,
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="Invalid mobile number format."),
            429: OpenApiResponse(description="Rate limit exceeded (5 requests/hour)."),
        },
        auth=[],  # public endpoint — no JWT required
    )
    
    def post(self, request):
        serializer = RequestOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mobile_number = serializer.validated_data["mobile_number"]

        user, created = User.objects.get_or_create(
            mobile_number=mobile_number,
            defaults={
                "username": f"user_{mobile_number[-4:]}_{secrets.token_hex(3)}",
                "role": "customer",
                "is_staff": False,
            },
        )

        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])

        record, sms_sent = create_otp_record(user, is_new_user=created, purpose="login")

        response_data = {
            "otp_session_token": str(record.otp_session_token),
            "expires_in": record.OTP_EXPIRY_MINUTES * 60,
            "resend_available_in": record.resend_available_in,
        }

        # Only surface sms_sent when it failed — avoids noise in the happy path
        if not sms_sent:
            response_data["sms_sent"] = False

        return Response(response_data, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_verify"

    @extend_schema(
        tags=["Auth"],
        summary="Verify OTP",
        description=(
            "Submit the OTP received via SMS along with the `otp_session_token` "
            "from the request step. On success, returns a JWT access token and "
            "refresh token.\n\n"
            "- Failed attempts increment a counter; the session locks after **3 failures**.\n"
            "- The `is_new_user` flag lets the client redirect to profile completion."
        ),
        request=VerifyOTPSerializer,
        responses={
            200: OpenApiResponse(
                description="OTP verified. JWT tokens issued.",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={
                            "access": "<jwt_access_token>",
                            "refresh": "<jwt_refresh_token>",
                            "is_new_user": True,
                            "user": {
                                "public_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                                "mobile_number": "+919876543210",
                                "email": None,
                                "role": "customer",
                                "name": "",
                            },
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="Incorrect OTP, expired session, or session locked.",
            ),
            429: OpenApiResponse(description="Rate limit exceeded (10 requests/minute)."),
        },
        auth=[],  # public endpoint
    )
    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp_session_token = str(serializer.validated_data["otp_session_token"])
        otp_submitted = serializer.validated_data["otp"]

        try:
            record = verify_otp_record(otp_session_token, otp_submitted)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        user = record.user
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "is_new_user": record.is_new_user,
                "user": UserMeSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class ResendOTPView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth"],
        summary="Resend OTP",
        description=(
            "Generate a new OTP and resend it on the same session. "
            "The `otp_session_token` does not change.\n\n"
            "**Limits:** max 3 resends per session, 30-second cooldown between resends."
        ),
        request=ResendOTPSerializer,
        responses={
            200: OpenApiResponse(
                description="New OTP sent.",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"detail": "OTP resent successfully.", "resend_available_in": 30},
                    )
                ],
            ),
            400: OpenApiResponse(
                description="Cooldown active, resend limit reached, or invalid session token.",
            ),
        },
        auth=[],
    )
    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        otp_session_token = str(serializer.validated_data["otp_session_token"])

        try:
            record, _sms_sent = resend_otp(otp_session_token)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "detail": "OTP resent successfully.",
                "resend_available_in": record.resend_available_in,
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth"],
        summary="Logout",
        description=(
            "Blacklist the provided refresh token. After logout the refresh token "
            "is permanently invalidated. The access token expires naturally (60 min).\n\n"
            "Requires a valid `Authorization: Bearer <access_token>` header."
        ),
        request={
            "application/json": {
                "type": "object",
                "properties": {"refresh": {"type": "string", "description": "JWT refresh token"}},
                "required": ["refresh"],
            }
        },
        responses={
            200: OpenApiResponse(
                description="Logged out.",
                examples=[OpenApiExample("Success", value={"detail": "Logged out successfully."})],
            ),
            400: OpenApiResponse(description="Missing or already blacklisted refresh token."),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response(
                {"error": "refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"detail": "Logged out successfully."}, status=status.HTTP_200_OK)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Auth"],
        summary="Get my profile",
        description=(
            "Return the authenticated customer's public profile. "
            "Always returns `public_id` — the integer primary key is never exposed."
        ),
        responses={
            200: UserMeSerializer,
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def get(self, request):
        return Response(UserMeSerializer(request.user).data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Auth"],
        summary="Update my profile",
        description=(
            "Partially update the authenticated customer's profile. "
            "Intended for profile completion after first login (`is_new_user=true`).\n\n"
            "Mobile number and role cannot be changed via this endpoint."
        ),
        request=UpdateMeSerializer,
        responses={
            200: UserMeSerializer,
            400: OpenApiResponse(description="Validation error (e.g. email already taken)."),
            401: OpenApiResponse(description="Access token missing or expired."),
        },
    )
    def patch(self, request):
        serializer = UpdateMeSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserMeSerializer(request.user).data, status=status.HTTP_200_OK)
