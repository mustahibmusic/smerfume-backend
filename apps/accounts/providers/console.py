"""
Console SMS backend — development / test use only.

This backend is automatically selected when SEND_REAL_OTP is not True in settings,
regardless of the SMS_BACKEND setting value. This prevents accidental SMS charges
in dev and staging environments.

To activate the real SMS backend, set SEND_REAL_OTP=True in your .env (production only).
"""

import logging

from .base import BaseSMSBackend

logger = logging.getLogger(__name__)


class ConsoleSMSBackend(BaseSMSBackend):
    """Logs the OTP to the console instead of sending a real SMS.

    Used automatically when settings.SEND_REAL_OTP is False (default).
    Safe for local development, CI, and staging environments.
    """

    def send_otp(self, mobile_number: str, otp: str, otp_session_token: str = "") -> bool:
        """Print the OTP and session token directly to stdout so they are always visible in the console."""
        print(
            f"\n{'='*50}\n"
            f"  [DEV] OTP for {mobile_number}: {otp}\n"
            f"  Session Token : {otp_session_token}\n"
            f"  (Set SEND_REAL_OTP=True in .env to send real SMS)\n"
            f"{'='*50}\n"
        )
        return True
