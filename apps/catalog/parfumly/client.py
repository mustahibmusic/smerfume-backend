"""
HTTP client for the public Parfumly API (https://api.parfumly.in).

Only this module performs network I/O. It returns raw decoded JSON and knows
nothing about Smerfume models; normalization lives in normalize.py.

Contract (verified against live responses — the published /openapi.json
returned 404 at implementation time):

    GET /products/search?brand=<brand-slug>&page=<n>&pageSize=<<=48>
        -> {"items": [...], "total": int, "page": int, "pageSize": int, ...}
    GET /products/{slug}
        -> full product: name, slug, gender, year, notes{top,heart,base},
           brand, variants[{concentration, sizeMl, form, offers, ...}], ...

No authentication is required.
"""

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

# The API rejects pageSize > 48 with HTTP 400.
MAX_PAGE_SIZE = 48
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ParfumlyError(Exception):
    """A Parfumly request failed permanently (after retries)."""


class ParfumlyClient:
    def __init__(
        self,
        base_url=None,
        delay_seconds=None,
        timeout_seconds=None,
        max_attempts=3,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
    ):
        self.base_url = (base_url or settings.PARFUMLY_API_BASE_URL).rstrip("/")
        self.delay_seconds = (
            settings.PARFUMLY_REQUEST_DELAY_SECONDS if delay_seconds is None else delay_seconds
        )
        self.timeout_seconds = (
            settings.PARFUMLY_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        self.max_attempts = max_attempts
        self._opener = opener
        self._sleep = sleep
        self._has_requested = False

    def search_brand(self, brand_slug):
        """Return every search item for a brand, following pagination."""
        items = []
        page = 1
        while True:
            data = self._get(
                "/products/search",
                {"brand": brand_slug, "page": page, "pageSize": MAX_PAGE_SIZE},
            )
            page_items = data.get("items") or []
            items.extend(page_items)
            total = data.get("total")
            if not page_items or (isinstance(total, int) and len(items) >= total):
                return items
            page += 1

    def get_product(self, slug):
        """Return the full product document for a Parfumly product slug."""
        return self._get(f"/products/{urllib.parse.quote(slug, safe='')}")

    def _get(self, path, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "smerfume-catalog-import/1.0"},
        )

        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                retryable = exc.code in RETRYABLE_STATUS_CODES
                reason = f"HTTP {exc.code}"
            except (OSError, http.client.HTTPException) as exc:
                # URLError, timeouts, connection resets and truncated responses
                # are transient network failures.
                retryable = True
                reason = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                # Malformed JSON — retrying will not help.
                raise ParfumlyError(f"Invalid JSON from {url}") from exc

            if not retryable or attempt == self.max_attempts:
                logger.error("Parfumly request failed: %s (%s, attempt %d)", url, reason, attempt)
                raise ParfumlyError(f"{reason} for {url}")
            logger.warning("Parfumly request retry: %s (%s, attempt %d)", url, reason, attempt)
            self._sleep(max(self.delay_seconds, 1) * 2 ** attempt)

        raise ParfumlyError(f"Request not attempted: {url}")  # pragma: no cover

    def _throttle(self):
        if self._has_requested and self.delay_seconds:
            self._sleep(self.delay_seconds)
        self._has_requested = True
