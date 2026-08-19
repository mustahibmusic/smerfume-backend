"""
SMS Provider abstraction layer.

This module defines the abstract base class that all SMS backends must implement.
The provider pattern allows switching SMS vendors without touching business logic:
    1. Create a new class in this package that extends BaseSMSBackend
    2. Implement send_otp()
    3. Set SMS_BACKEND in settings to the new class path

Current providers:
    - ConsoleSMSBackend (default in dev/staging, logs to console)
    - MSG91SMSBackend   (production, set SEND_REAL_OTP=True)
"""

from abc import ABC, abstractmethod


class BaseSMSBackend(ABC):
    """Abstract base class for SMS provider backends.

    All concrete backends must implement send_otp(). The method should
    return True on success and False on any failure so callers can decide
    whether to surface an error to the user.
    """

    @abstractmethod
    def send_otp(self, mobile_number: str, otp: str, otp_session_token: str = "") -> bool:
        """Send a one-time password via SMS.

        Args:
            mobile_number: E.164-formatted phone number (e.g. "+919876543210").
            otp: 6-digit numeric string.
            otp_session_token: UUID session token (used by dev backends for console output).

        Returns:
            True if the SMS was dispatched successfully, False otherwise.
        """
        raise NotImplementedError
