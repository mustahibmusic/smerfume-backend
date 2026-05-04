from rest_framework.views import APIView
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from .models import OTPVerification
import random
from rest_framework_simplejwt.tokens import RefreshToken
from django.db.models import Q


User = get_user_model()

class RequestOTPView(APIView):
    def post(self, request):
        identifier = request.data.get("mobile_number") # Could be mobile later
        
        # 1. Get or Create User (Silent Registration)
        user, created = User.objects.get_or_create(
            mobile_number=identifier,
            defaults={
                'username': identifier.split('@')[0] + str(random.randint(1000, 9999)),
                'role': 'customer',
                'is_staff': False
            }
        )
        
        if created:
            user.set_unusable_password()
            user.save()

        # 2. Generate 6-digit OTP
        otp_code = f"{random.randint(100000, 999999)}"
        
        # 3. Store OTP
        OTPVerification.objects.create(user=user, code=otp_code)

        # 4. TODO: Integrate Email/SMS Service here
        print(f"DEBUG: Sending OTP {otp_code} to {identifier}")
        
        return Response({"message": "OTP sent successfully", "is_new_user": created})
    

from django.contrib.auth import login

class VerifyOTPView(APIView):
    def post(self, request):
        identifier = request.data.get("identifier") # email or mobile_number
        otp_submitted = request.data.get("otp")

        # 1. Find user by email or mobile
        user = User.objects.filter(
            Q(email=identifier) | Q(mobile_number=identifier)
        ).first()

        if not user:
            return Response({"error": "User not found"}, status=404)

        # 2. Check for the latest unverified OTP
        otp_record = OTPVerification.objects.filter(
            user=user, 
            code=otp_submitted, 
            is_verified=False
        ).last()

        if otp_record and not otp_record.is_expired:
            # Mark as used
            otp_record.is_verified = True
            otp_record.save()

            # 3. Generate JWT Tokens
            refresh = RefreshToken.for_user(user)
            
            return Response({
                "message": "Authentication successful",
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
                "user": {
                    "email": user.email,
                    "mobile": user.mobile_number,
                    "role": user.role
                }
            })

        return Response({"error": "Invalid or expired OTP"}, status=400)