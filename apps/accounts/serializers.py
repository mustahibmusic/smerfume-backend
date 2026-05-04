import re

from rest_framework import serializers


class RequestOTPSerializer(serializers.Serializer):
    mobile_number = serializers.CharField(max_length=15)

    def validate_mobile_number(self, value):
        cleaned = re.sub(r"\s+", "", value)
        if not re.fullmatch(r"\+?[0-9]{10,15}", cleaned):
            raise serializers.ValidationError("Enter a valid mobile number.")
        return cleaned


class VerifyOTPSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254)
    otp = serializers.CharField(min_length=6, max_length=6)

    def validate_otp(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("OTP must be numeric.")
        return value
