"""
OTP service layer.

Centralises all OTP lifecycle logic: generation, hashing, verification,
and resending. Views and tests should call only these functions — they
must never interact with OTPVerification directly.
"""

import logging
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.accounts.models import OTPVerification

User = get_user_model()

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

class OTPRateLimitError(Exception):
    """A mobile number has reached its hourly OTP send limit."""

    message = "Too many OTP requests for this number. Please try again later."

    def __init__(self):
        super().__init__(self.message)


def _recent_send_count(user) -> int:
    """SMS sends for this user in the last hour: one per row created in the
    window plus resend_count of rows resent in the window. Resend updates a
    row in place, so rows alone undercount. This never undercounts; it can
    overcount older resends on a row resent inside the window."""
    cutoff = timezone.now() - timedelta(hours=1)
    rows = OTPVerification.objects.filter(user=user)
    initial = rows.filter(created_at__gte=cutoff).count()
    resends = rows.filter(last_resend_at__gte=cutoff).aggregate(
        total=Sum("resend_count")
    )["total"] or 0
    return initial + resends


def _lock_user(user_pk):
    """Serialize OTP sends per mobile. Caller must be inside a transaction."""
    return User.objects.select_for_update().get(pk=user_pk)


def _check_mobile_send_limit(user) -> None:
    """Raise OTPRateLimitError at the per-mobile limit. The user row must
    already be locked so concurrent senders cannot share a stale count."""
    from django.conf import settings  # local import, as in _get_sms_backend

    if _recent_send_count(user) >= settings.OTP_MAX_SENDS_PER_MOBILE_PER_HOUR:
        raise OTPRateLimitError()


@transaction.atomic
def create_otp_record(user, *, is_new_user: bool = False, purpose: str = "login"):
    """Create and send an OTP for the given user.

    Args:
        user: The User instance to generate the OTP for.
        is_new_user: True when the user account was just created.
        purpose: OTP purpose string (currently only "login" is supported).

    Returns:
        Tuple of (OTPVerification instance, sms_sent: bool).

    Raises:
        OTPRateLimitError: The mobile number reached its hourly send limit.
    """
    _lock_user(user.pk)
    _check_mobile_send_limit(user)

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


@transaction.atomic
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
        OTPRateLimitError: The mobile number reached its hourly send limit.
    """
    session = OTPVerification.objects.filter(
        otp_session_token=otp_session_token, is_active=True
    ).values_list("user_id", flat=True).first()
    if session is None:
        raise ValueError("Invalid or expired session. Please request a new OTP.")
    _lock_user(session)  # same lock as create_otp_record, taken first
    record = (
        OTPVerification.objects.select_related("user")
        .select_for_update(of=("self",))
        .get(otp_session_token=otp_session_token, is_active=True)
    )

    if record.is_verified:
        raise ValueError("This OTP has already been verified.")

    if record.resend_count >= OTPVerification.MAX_RESENDS:
        raise ValueError("Maximum resend limit reached. Please request a new OTP.")

    if not record.can_resend:
        seconds_left = record.resend_available_in
        raise ValueError(
            f"Please wait {seconds_left} second(s) before requesting another OTP."
        )

    _check_mobile_send_limit(record.user)

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
