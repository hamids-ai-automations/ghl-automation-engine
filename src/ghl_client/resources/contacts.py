"""Contacts resource: paginated search and idempotent upsert.

Upsert idempotency matters more than it looks. HighLevel deduplicates a contact on
email or phone, so a naive "create" on a retry produces duplicates that then split
workflow history across two ids. The ledger here makes a replayed upsert return the
original response instead of hitting the API again.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..client import GhlClient
from ..pagination import Collection, Page, extract_cursor, paginate


@dataclass
class IdempotencyLedger:
    """In-process replay cache. Swap for Redis/Postgres in real deployments."""

    _records: dict[str, dict[str, Any]] = field(default_factory=dict)
    hits: int = 0

    def key_for(self, payload: Mapping[str, Any], explicit: str | None = None) -> str:
        if explicit:
            return f"k:{explicit}"
        identity = {
            "email": (payload.get("email") or "").strip().lower(),
            "phone": (payload.get("phone") or "").strip(),
            "locationId": payload.get("locationId"),
        }
        if not identity["email"] and not identity["phone"]:
            return ""
        blob = json.dumps(identity, sort_keys=True).encode("utf-8")
        return "f:" + hashlib.sha256(blob).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        record = self._records.get(key)
        if record is not None:
            self.hits += 1
            return dict(record)
        return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        if key:
            self._records[key] = dict(value)


class ContactsResource:
    def __init__(
        self,
        client: GhlClient,
        *,
        ledger: IdempotencyLedger | None = None,
        page_size: int = 100,
    ) -> None:
        self._client = client
        self.ledger = ledger or IdempotencyLedger()
        self.page_size = page_size

    def get(self, contact_id: str) -> dict[str, Any]:
        return self._client.get(f"/contacts/{contact_id}")

    def upsert(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body = dict(payload)
        if self._client.location_id and "locationId" not in body:
            body["locationId"] = self._client.location_id
        key = self.ledger.key_for(body, idempotency_key)
        cached = self.ledger.get(key)
        if cached is not None:
            return cached
        result = self._client.post("/contacts/upsert", json_body=body)
        self.ledger.put(key, result)
        return result

    def search(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
        strict: bool = True,
        max_pages: int = 200,
    ) -> Collection[dict[str, Any]]:
        """Search contacts, walking every cursor.

        Returns a Collection. Check ``.complete`` before acting on the result set —
        an incomplete collection means the automation must not proceed.
        """

        def fetch(cursor: str | None) -> Page[dict[str, Any]]:
            payload = self._client.post(
                "/contacts/search",
                json_body={
                    "locationId": self._client.location_id,
                    "query": query,
                    "pageLimit": self.page_size,
                    "startAfterId": cursor,
                },
            )
            items = (payload or {}).get("contacts") or []
            return Page(items=items, next_cursor=extract_cursor(payload or {}))

        return paginate(
            fetch, max_pages=max_pages, max_items=limit, strict=strict
        )
