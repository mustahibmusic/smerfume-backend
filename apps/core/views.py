import logging

from django.db import connection
from django.http import HttpResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def readiness(request):
    """Readiness probe: SELECT 1 on the primary database. 200 "ok" or 503
    "unavailable" (the cause is logged, never returned). Redis is not
    checked: throttling keeps working without it."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Readiness check failed: database unavailable.")
        response = HttpResponse(b"unavailable", status=503, content_type="text/plain")
    else:
        response = HttpResponse(b"ok", content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response
