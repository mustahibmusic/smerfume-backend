"""Contact-number rules shared by OTP login, checkout, in-store sales and
customer lookup. This is the only place a mobile number is normalised.

Canonical storage for a local (Indian) number is ``+91`` followed by its
10 digits. Any 10-digit local number is accepted; the first digit is not
checked. Explicit international numbers (``+`` and a country code other
than 91) are kept as entered, because checkout has always accepted them.
"""

import re

INDIA_PREFIX = "+91"
LOCAL_DIGITS = 10
LOOKUP_MIN_DIGITS = 6

_FORMATTING = re.compile(r"[\s\-.()]")
_DIGITS = re.compile(r"[0-9]+")


class InvalidMobileNumber(ValueError):
    """The value is not a contact number Smerfume accepts."""


def _strip_formatting(value):
    return _FORMATTING.sub("", value or "")


def _local_digits(cleaned):
    """The 10 local digits when ``cleaned`` (no formatting) is a recognised
    local or +91 form, else None."""
    if cleaned.startswith("+"):
        rest = cleaned[1:]
        if rest.startswith("91") and rest.isdigit():
            if len(rest) == LOCAL_DIGITS + 2:
                return rest[2:]
            if len(rest) == LOCAL_DIGITS + 3 and rest[2] == "0":
                return rest[3:]  # +91 0XXXXXXXXXX: the trunk zero is formatting
        return None
    if not cleaned.isdigit():
        return None
    if len(cleaned) == LOCAL_DIGITS:
        return cleaned  # any first digit, including 0
    if len(cleaned) == LOCAL_DIGITS + 1 and cleaned.startswith("0"):
        return cleaned[1:]  # only the extra trunk zero is formatting
    if len(cleaned) == LOCAL_DIGITS + 2 and cleaned.startswith("91"):
        return cleaned[2:]
    return None


def canonical_indian_mobile(value):
    """``+91XXXXXXXXXX`` for a recognised local or +91 number, else None.
    Never raises."""
    digits = _local_digits(_strip_formatting(value))
    return f"{INDIA_PREFIX}{digits}" if digits else None


def normalize_indian_mobile(value):
    """Canonical contact number for storage and lookup.

    - 10-digit local number (any first digit): ``+91`` + the 10 digits
    - ``0`` + 10 digits, ``91`` + 10 digits, ``+91`` + 10 digits: the same
    - ``+`` with another country code, 10 to 15 digits in total: unchanged
    - another bare 11 to 15 digit value (accepted by checkout before this
      rule existed): unchanged, never guessed into ``+91``

    Raises InvalidMobileNumber for anything else.
    """
    cleaned = _strip_formatting(value)
    canonical = canonical_indian_mobile(cleaned)
    if canonical:
        return canonical
    if cleaned.startswith(INDIA_PREFIX):
        raise InvalidMobileNumber("Enter a valid 10-digit mobile number after +91.")
    if re.fullmatch(r"\+[0-9]{10,15}", cleaned) or re.fullmatch(r"[0-9]{11,15}", cleaned):
        return cleaned
    raise InvalidMobileNumber("Enter a valid mobile number.")


def normalize_local_mobile(value):
    """Like normalize_indian_mobile() but for flows that only work with
    local numbers (OTP by SMS): anything that is not a recognised local or
    +91 number is refused."""
    canonical = canonical_indian_mobile(value)
    if canonical is None:
        raise InvalidMobileNumber("Enter a valid 10-digit mobile number.")
    return canonical


def mobile_lookup_prefix(value):
    """``+91`` + 6 to 10 local digits for the in-store customer lookup, or
    None (too short, not a local number, or another country code).

    Partial input is taken as local digits exactly as typed, so a leading
    zero stays part of the number. The trunk (``0`` + 10) and ``91`` + 10
    forms are recognised only when complete.
    """
    cleaned = _strip_formatting(value)
    full = canonical_indian_mobile(cleaned)
    if full:
        return full
    if cleaned.startswith(INDIA_PREFIX):
        cleaned = cleaned[len(INDIA_PREFIX):]
    if cleaned.startswith("+") or not _DIGITS.fullmatch(cleaned or "x"):
        return None
    if LOOKUP_MIN_DIGITS <= len(cleaned) <= LOCAL_DIGITS:
        return f"{INDIA_PREFIX}{cleaned}"
    return None


def mask_mobile(value):
    """For logs and reports: only the last four digits."""
    value = value or ""
    return f"******{value[-4:]}" if len(value) >= 4 else "******"


def mobile_search_prefix(dial_code, national):
    """Prefix for a customer search scoped to one country: the dial code
    plus the typed national digits (at least 6), or None.

    For +91 the local rules apply (mobile_lookup_prefix): a leading zero is
    part of a partial number, and the complete 0 + 10 / 91 + 10 forms are
    recognised. For other codes the digits are used as typed, up to 15
    digits in total.
    """
    cleaned = _strip_formatting(national)
    if not cleaned.isdigit():
        return None
    if dial_code == INDIA_PREFIX:
        return mobile_lookup_prefix(cleaned)
    if len(cleaned) < LOOKUP_MIN_DIGITS or len(dial_code) - 1 + len(cleaned) > 15:
        return None
    return f"{dial_code}{cleaned}"


def combine_mobile(dial_code, national):
    """The canonical stored number for a country dial code and a national
    number typed separately (the in-store sale form). Raises
    InvalidMobileNumber."""
    cleaned = _strip_formatting(national)
    if not cleaned.isdigit():
        raise InvalidMobileNumber("Enter the mobile number as digits, without the country code.")
    if dial_code == INDIA_PREFIX:
        return normalize_local_mobile(cleaned)
    return normalize_indian_mobile(f"{dial_code}{cleaned}")
