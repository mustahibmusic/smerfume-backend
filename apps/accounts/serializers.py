from rest_framework import serializers

class RequestOTPSerializer(serializers.Serializer):
    mobile_number = serializers.CharField(max_length=15)


class VerifyOTPSerializer(serializers.Serializer):
    mobile_number = serializers.CharField(max_length=15)
    otp = serializers.CharField(max_length=6)
