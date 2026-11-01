"""Liveness probe for container hosts.

/healthz/ answers before any other middleware, so a provider's internal
probe (an IP as the Host header, plain HTTP) is not refused by
ALLOWED_HOSTS or redirected by SECURE_SSL_REDIRECT. It only says the
process is serving: no database, no cache, no version or environment
details. Readiness (database) is /readyz/, which runs through the normal
request path. Nothing but this exact path and GET/HEAD skips the checks.
"""

from django.http import HttpResponse

LIVENESS_PATH = "/healthz/"


class LivenessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == LIVENESS_PATH and request.method in ("GET", "HEAD"):
            response = HttpResponse(b"ok", content_type="text/plain")
            response["Cache-Control"] = "no-store"
            return response
        return self.get_response(request)
