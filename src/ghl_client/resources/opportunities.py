"""Opportunities resource: paged search (GHL-026).

Only the read path is implemented. Creating or moving an opportunity is a write
with money attached, and this package does not get to guess at a payload shape
for one until the warehouse can reconcile it.
"""

from __future__ import annotations

from typing import Any

from ..client import GhlClient
from ..pagination import Collection, Page, extract_cursor, paginate

__all__ = ["OpportunitiesResource"]


class OpportunitiesResource:
    def __init__(self, client: GhlClient, *, page_size: int = 100) -> None:
        self._client = client
        self.page_size = page_size

    def get(self, opportunity_id: str) -> dict[str, Any]:
        return self._client.get(f"/opportunities/{opportunity_id}")

    def search(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
        strict: bool = True,
        max_pages: int = 200,
    ) -> Collection[dict[str, Any]]:
        """Walk every cursor and return an honest ``Collection``.

        The same contract as contacts: ``complete`` is either true or the call
        raised, because a downstream pipeline sum computed over page 1 of 5 is
        worse than no sum at all.
        """

        def fetch(cursor: str | None) -> Page[dict[str, Any]]:
            payload = self._client.post(
                "/opportunities/search",
                json_body={
                    "locationId": self._client.location_id,
                    "query": query,
                    "pageLimit": self.page_size,
                    "startAfterId": cursor,
                },
            )
            items = (payload or {}).get("opportunities") or []
            return Page(items=items, next_cursor=extract_cursor(payload or {}))

        return paginate(fetch, max_pages=max_pages, max_items=limit, strict=strict)
