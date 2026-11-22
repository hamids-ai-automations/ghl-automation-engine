"""Custom field definitions for a location.

Discovery only. The client never creates or mutates fields: a silent
field creation would turn a mapping mistake into permanently duplicated
data in the customer's CRM.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["CustomFieldsResource"]


class CustomFieldsResource:
    def __init__(self, client: Any) -> None:
        self._client = client

    def list(self, location_id: str | None = None) -> list[dict[str, Any]]:
        location = location_id or getattr(self._client, "location_id", None)
        if not location:
            raise ValueError("location_id is required to list custom fields")
        payload = self._client.get(f"/locations/{location}/customFields")
        if not isinstance(payload, Mapping):
            return []
        fields = payload.get("customFields") or []
        return [dict(item) for item in fields if isinstance(item, Mapping)]
