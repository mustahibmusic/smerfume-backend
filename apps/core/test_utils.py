"""Shared test helpers."""

from django.db import connection
from django.test.utils import CaptureQueriesContext


class AdminQueryCountMixin:
    """Assert an admin changelist runs a fixed number of queries no matter
    how many rows it shows (no per-row N+1)."""

    def _changelist_query_count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def assertChangelistQueriesFlat(self, url, make_row):
        """make_row(i) creates one row. Compare 2 rows with 6 rows."""
        for i in range(2):
            make_row(i)
        with_two = self._changelist_query_count(url)
        for i in range(2, 6):
            make_row(i)
        with_six = self._changelist_query_count(url)
        self.assertEqual(
            with_two, with_six,
            f"{url}: {with_two} queries for 2 rows but {with_six} for 6 rows",
        )
