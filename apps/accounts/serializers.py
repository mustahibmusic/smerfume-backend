"""
Serializers for the accounts app.

Validation helpers:
    _normalize_indian_mobile — converts various Indian mobile formats to E.164.
"""

import re

from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


# ── Mobile number normalisation ──────────────────────────────────────────────

def _normalize_indian_mobile(value: str) -> str:
    """Normalise an Indian mobile number to E.164 format (+91XXXXXXXXXX).

    Accepted input formats:
        - 10-digit bare number:      9876543210
        - 12-digit with country code: 919876543210
        - E.164 with plus:            +919876543210
        - Whitespace is stripped before validation.

    The first digit of the 10-digit part must be 6–9 (Indian mobile range).

    Returns:
        E.164 string, e.g. "+919876543210".

    Raises:
        serializers.ValidationError: If the number does not match any accepted format.
    """
    cleaned = re.sub(r"\s+", "", value)

    # Strip leading '+' for uniform processing
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    # Strip leading country code 91 if present (leaving 10 digits)
    if cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]

    if not re.fullmatch(r"[0-9]{10}", cleaned):
        raise serializers.ValidationError(
            "Enter a valid 10-digit Indian mobile number."
        )

    if cleaned[0] not in "6789":
        raise serializers.ValidationError(
            "Mobile number must start with 6, 7, 8, or 9."
        )

    return f"+91{cleaned}"


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
