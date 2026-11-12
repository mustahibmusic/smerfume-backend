"""
Serializers for the accounts app.

Validation helpers:
    _normalize_indian_mobile — OTP wrapper around apps.accounts.mobile.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .mobile import InvalidMobileNumber, normalize_local_mobile

User = get_user_model()


# ── Mobile number normalisation ──────────────────────────────────────────────

def _normalize_indian_mobile(value: str) -> str:
    """OTP login takes local numbers only (SMS). Thin wrapper around the
    shared rule in apps.accounts.mobile: any 10-digit local number, or its
    0 / 91 / +91 forms, becomes ``+91XXXXXXXXXX``.

    Raises:
        serializers.ValidationError: If the value is not a local number.
    """
    try:
        return normalize_local_mobile(value)
    except InvalidMobileNumber as exc:
        raise serializers.ValidationError(str(exc)) from exc


# ── Request serializers ──────────────────────────────────────────────────────

class RequestOTPSerializer(serializers.Serializer):
    """Validate and normalise the mobile number for an OTP request."""

    mobile_number = serializers.CharField(max_length=20)

    def validate_mobile_number(self, value: str) -> str:
        return _normalize_indian_mobile(value)


class VerifyOTPSerializer(serializers.Serializer):
    """Validate OTP submission payload."""

    otp_session_token = serializers.UUIDField()
    otp = serializers.CharField(min_length=6, max_length=6)

    def validate_otp(self, value: str) -> str:
        if not value.isdigit():
            raise serializers.ValidationError("OTP must be numeric.")
        return value


class ResendOTPSerializer(serializers.Serializer):
    """Validate resend OTP request payload."""

    otp_session_token = serializers.UUIDField()


# ── User serializers ──────────────────────────────────────────────────────────

class UserMeSerializer(serializers.ModelSerializer):
    """Read-only serializer exposing safe public user fields.

    Returns public_id (UUID) rather than the integer primary key so the
    client never has an enumerable identifier.
    """

    name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("public_id", "mobile_number", "email", "role", "name")
        read_only_fields = fields

    def get_name(self, obj) -> str:
        """Return the user's full name via the model property."""
        return obj.full_name


class UpdateMeSerializer(serializers.ModelSerializer):
    """Allow users to update their own profile fields.

    Validates email uniqueness while excluding the current user's own record
    from the uniqueness check.
    """

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email")

    def validate_email(self, value: str) -> str:
        """Ensure the new email is not already taken by another user."""
        qs = User.objects.filter(email=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                "A user with that email already exists."
            )
        return value
