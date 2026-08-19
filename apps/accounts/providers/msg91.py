"""
MSG91 SMS backend — production provider.

API reference: https://docs.msg91.com/reference/send-otp

Environment variables required (set in .env for production):
    MSG91_AUTH_KEY    — Authentication key from the MSG91 dashboard
    MSG91_TEMPLATE_ID — OTP template ID configured in MSG91
    MSG91_SENDER_ID   — 6-char sender ID approved by MSG91 (default: SMRFME)

To switch to a different SMS provider:
    1. Create a new backend class in this package extending BaseSMSBackend
    2. Implement send_otp()
    3. Set SMS_BACKEND=<your.module.ClassName> in .env
    4. No code changes needed anywhere else — the service layer resolves the backend
       dynamically via import_string.
"""

import logging

import requests
from django.conf import settings

from .base import BaseSMSBackend

logger = logging.getLogger(__name__)

# MSG91 OTP endpoint — v5 API
MSG91_OTP_URL = "https://control.msg91.com/api/v5/otp"

# Hard timeout so a slow vendor never hangs the request cycle
_REQUEST_TIMEOUT_SECONDS = 5


class MSG91SMSBackend(BaseSMSBackend):
    """Sends OTPs via MSG91's v5 OTP API.

    This class is only instantiated when settings.SEND_REAL_OTP is True.
    In all other environments ConsoleSMSBackend is used instead (see services/otp.py).
    """

    def send_otp(self, mobile_number: str, otp: str, otp_session_token: str = "") -> bool:
        """Dispatch OTP through MSG91.

        Args:
            mobile_number: E.164-formatted number (e.g. "+919876543210").
                           MSG91 expects the number without the leading '+'.
            otp: 6-digit numeric string.

        Returns:
            True on HTTP 200 with MSG91 type=="success", False otherwise.
        """
        # MSG91 expects the mobile number without the leading '+'
        mobile_clean = mobile_number.lstrip("+")

        headers = {
            "authkey": settings.MSG91_AUTH_KEY,
            "Content-Type": "application/json",
        }

        payload = {
            "template_id": settings.MSG91_TEMPLATE_ID,
            "mobile": mobile_clean,
            "authkey": settings.MSG91_AUTH_KEY,
            # MSG91 replaces ##OTP## in the template with this value
            "otp": otp,
            # Sender ID must match the approved sender on the MSG91 dashboard
            "sender": settings.MSG91_SENDER_ID,
        }

        try:
            response = requests.post(
                MSG91_OTP_URL,
                json=payload,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()

            # MSG91 returns {"type": "success", ...} on success
            if data.get("type") == "success":
                logger.info("MSG91 OTP sent to %s", mobile_number)
                return True

            # Vendor returned HTTP 200 but indicated a logical failure
            logger.warning(
                "MSG91 returned non-success response for %s: %s",
                mobile_number,
                data,
            )
            return False

        except requests.exceptions.Timeout:
            logger.error("MSG91 request timed out for %s", mobile_number)
            return False
        except requests.exceptions.RequestException as exc:
            logger.error("MSG91 request failed for %s: %s", mobile_number, exc)
            return False
