"""DRF throttles with an explicit policy for when the cache (Redis) fails.

OTP endpoints fail closed: a missing rate limit would weaken OTP abuse
protection. General anon/user throttles fail open so the storefront stays
up. Only RedisError is handled; the client never sees Redis details.
"""
import logging

from redis.exceptions import RedisError
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.throttling import (
    AnonRateThrottle,
    ScopedRateThrottle,
    UserRateThrottle,
)

logger = logging.getLogger(__name__)


class ThrottleUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Login is temporarily unavailable. Please try again shortly."
    default_code = "throttle_unavailable"


def _log_cache_failure(scope):
    logger.error("Throttle cache unavailable (scope=%s).", scope, exc_info=True)


class FailClosedScopedRateThrottle(ScopedRateThrottle):
    """Scoped throttle for OTP endpoints: refuses the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(getattr(view, "throttle_scope", None))
            raise ThrottleUnavailable()


class FailOpenAnonRateThrottle(AnonRateThrottle):
    """Global anonymous burst limit: allows the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(self.scope)
            return True


class FailOpenUserRateThrottle(UserRateThrottle):
    """Global authenticated burst limit: allows the request if Redis fails."""

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except RedisError:
            _log_cache_failure(self.scope)
            return True
