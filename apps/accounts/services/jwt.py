"""
JWT token helpers.

Thin wrapper around SimpleJWT so the rest of the codebase does not import
from rest_framework_simplejwt directly. If the JWT library changes, only
this module needs updating.
"""

from rest_framework_simplejwt.tokens import RefreshToken


def get_tokens_for_user(user) -> dict:
    """Generate a JWT refresh/access token pair for the given user.

    Args:
        user: A Django User instance.

    Returns:
        Dict with keys "refresh" and "access" containing the JWT strings.
    """
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }
