"""
OTP service layer.

Centralises all OTP lifecycle logic: generation, hashing, verification,
and resending. Views and tests should call only these functions — they
must never interact with OTPVerification directly.
"""

import logging
import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.accounts.models import OTPVerification

logger = logging.getLogger(__name__)


# ── Backend resolution ──────────────────────────────────────────────────────

def _get_sms_backend():
    """Resolve and return an SMS backend instance.

    Two-setting pattern:
        SEND_REAL_OTP (bool, default False):
            Master cost-control gate. When False, ConsoleSMSBackend is
            returned regardless of SMS_BACKEND — prevents accidental charges
            in dev, CI, and staging environments.

        SMS_BACKEND (str):
            Dotted class path of the real SMS provider to use when
            SEND_REAL_OTP is True. Change this env var to switch providers
            without any code changes.
    """
    from django.conf import settings  # local import avoids circular issues at module load

    if not getattr(settings, "SEND_REAL_OTP", False):
        # Always use the console backend unless explicitly enabled
        from apps.accounts.providers.console import ConsoleSMSBackend
        return ConsoleSMSBackend()

    backend_class = import_string(settings.SMS_BACKEND)
    return backend_class()


# ── OTP helpers ─────────────────────────────────────────────────────────────

def _generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP string."""
    return str(secrets.randbelow(900000) + 100000)


# ── Public service functions ─────────────────────────────────────────────────

def create_otp_record(user, *, is_new_user: bool = False, purpose: str = "login"):
    """Create and send an OTP for the given user.

    Args:
        user: The User instance to generate the OTP for.
        is_new_user: True when the user account was just created.
        purpose: OTP purpose string (currently only "login" is supported).

    Returns:
        Tuple of (OTPVerification instance, sms_sent: bool).
    """
    otp_code = _generate_otp()
    otp_hash = make_password(otp_code)

    record = OTPVerification.objects.create(
        user=user,
        otp_hash=otp_hash,
        purpose=purpose,
        is_new_user=is_new_user,
    )

    backend = _get_sms_backend()
    sms_sent = backend.send_otp(user.mobile_number, otp_code, str(record.otp_session_token))

    if not sms_sent:
        logger.warning("SMS dispatch failed for user %s (OTP record %s)", user.pk, record.pk)

    return record, sms_sent


def verify_otp_record(otp_session_token: str, otp_submitted: str) -> "OTPVerification":
    """Verify a submitted OTP against the stored hash.

    Args:
        otp_session_token: UUID token returned at OTP request time.
        otp_submitted: 6-digit string entered by the user.

    Returns:
        The verified OTPVerification instance (is_verified is set to True).

    Raises:
        ValueError: With a user-safe message on any failure condition.
    """
    try:
        record = OTPVerification.objects.select_related("user").get(
            otp_session_token=otp_session_token,
            is_active=True,
        )
    except OTPVerification.DoesNotExist:
        raise ValueError("Invalid or expired session. Please request a new OTP.")

    if record.is_verified:
        raise ValueError("This OTP has already been used. Please request a new one.")

    if record.is_expired:
        raise ValueError("OTP has expired. Please request a new one.")

    if record.is_locked:
        raise ValueError(
            "Too many incorrect attempts. Please request a new OTP."
        )

    if not check_password(otp_submitted, record.otp_hash):
        # Increment attempt counter before saving
        record.attempt_count += 1
        record.save(update_fields=["attempt_count", "updated_at"])
        attempts_left = OTPVerification.MAX_ATTEMPTS - record.attempt_count
        if attempts_left <= 0:
            raise ValueError("Too many incorrect attempts. Please request a new OTP.")
        raise ValueError(f"Incorrect OTP. {attempts_left} attempt(s) remaining.")

    record.is_verified = True
    record.save(update_fields=["is_verified", "updated_at"])
    return record


def resend_otp(otp_session_token: str):
    """Generate and send a new OTP on the same session.

    Validates resend limits and cooldown before proceeding. The existing
    OTPVerification record is updated in-place (same session token) so
    the client does not need to track a new token.

    Args:
        otp_session_token: UUID token from the original OTP request.

    Returns:
        Tuple of (OTPVerification instance, sms_sent: bool).

    Raises:
        ValueError: With a user-safe message on limit/cooldown violations.
    """
    try:
        record = OTPVerification.objects.select_related("user").get(
            otp_session_token=otp_session_token,
            is_active=True,
        )
    except OTPVerification.DoesNotExist:
        raise ValueError("Invalid or expired session. Please request a new OTP.")

    if record.is_verified:
        raise ValueError("This OTP has already been verified.")

    if record.resend_count >= OTPVerification.MAX_RESENDS:
        raise ValueError("Maximum resend limit reached. Please request a new OTP.")

    if not record.can_resend:
        seconds_left = record.resend_available_in
        raise ValueError(
            f"Please wait {seconds_left} second(s) before requesting another OTP."
        )

    # Generate fresh OTP and update the existing record
    otp_code = _generate_otp()
    now = timezone.now()

    record.otp_hash = make_password(otp_code)
    record.attempt_count = 0
    record.is_verified = False
    record.expires_at = now + timedelta(minutes=OTPVerification.OTP_EXPIRY_MINUTES)
    record.resend_count += 1
    record.last_resend_at = now
    record.save(update_fields=[
        "otp_hash", "attempt_count", "is_verified",
        "expires_at", "resend_count", "last_resend_at", "updated_at",
    ])

    backend = _get_sms_backend()
    sms_sent = backend.send_otp(record.user.mobile_number, otp_code, str(record.otp_session_token))

    if not sms_sent:
        logger.warning(
            "SMS resend failed for user %s (OTP record %s)",
            record.user.pk,
            record.pk,
        )

    return record, sms_sent
