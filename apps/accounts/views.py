import logging
import secrets

from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTPVerification
from .serializers import RequestOTPSerializer, VerifyOTPSerializer

logger = logging.getLogger(__name__)
User = get_user_model()


class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp_request"

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

        otp_code = f"{secrets.randbelow(900000) + 100000}"
        OTPVerification.objects.create(user=user, code=otp_code)

        # TODO: Integrate SMS/WhatsApp gateway (e.g., Twilio, MSG91) here
        logger.debug("OTP for %s: %s", mobile_number, otp_code)

        return Response({"message": "OTP sent successfully", "is_new_user": created})


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        otp_submitted = serializer.validated_data["otp"]

        user = User.objects.filter(
            Q(email=identifier) | Q(mobile_number=identifier)
        ).first()

        if not user:
            return Response({"error": "User not found."}, status=404)

        otp_record = OTPVerification.objects.filter(
            user=user,
            code=otp_submitted,
            is_verified=False,
        ).last()

        if not otp_record or otp_record.is_expired:
            return Response({"error": "Invalid or expired OTP."}, status=400)

        otp_record.is_verified = True
        otp_record.save(update_fields=["is_verified"])

        refresh = RefreshToken.for_user(user)

        return Response({
            "message": "Authentication successful.",
            "tokens": {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            },
            "user": {
                "public_id": str(user.public_id) if hasattr(user, "public_id") else None,
                "email": user.email,
                "mobile": user.mobile_number,
                "role": user.role,
            },
        })
