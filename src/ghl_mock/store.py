"""In-memory contact store for the mock server.

Mirrors the behaviours that actually break integrations:
* dedupe on email or phone (upsert semantics, not create)
* cursor pagination where the cursor is the last id, not an offset
* a stable ordering so cursors are meaningful
"""

from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class Contact:
    id: str
    locationId: str
    email: str | None = None
    phone: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    tags: list[str] = field(default_factory=list)
    customFields: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "locationId": self.locationId,
            "email": self.email,
            "phone": self.phone,
            "firstName": self.firstName,
            "lastName": self.lastName,
            "tags": list(self.tags),
            "customFields": [dict(item) for item in self.customFields],
        }


@dataclass
class CustomField:
    id: str
    name: str
    fieldKey: str
    dataType: str = "TEXT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "fieldKey": self.fieldKey,
            "dataType": self.dataType,
            "model": "contact",
        }


def default_custom_fields() -> list[CustomField]:
    """The field set the attribution map expects, as a fresh location has it."""

    specs = (
        ("cf_attribution_source", "Attribution Source", "contact.attribution_source", "TEXT"),
        ("cf_attribution_medium", "Attribution Medium", "contact.attribution_medium", "TEXT"),
        ("cf_attribution_campaign", "Attribution Campaign", "contact.attribution_campaign", "TEXT"),
        ("cf_attribution_content", "Attribution Content", "contact.attribution_content", "TEXT"),
        ("cf_attribution_landing_page", "Landing Page", "contact.attribution_landing_page", "TEXT"),
        ("cf_attribution_first_touch_at", "First Touch At", "contact.attribution_first_touch_at", "DATE"),
        ("cf_attribution_last_touch_at", "Last Touch At", "contact.attribution_last_touch_at", "DATE"),
        ("cf_gclid", "GCLID", "contact.gclid", "TEXT"),
        ("cf_fbclid", "FBCLID", "contact.fbclid", "TEXT"),
    )
    return [CustomField(*spec) for spec in specs]


class CustomFieldStore:
    """Location schema. Mutable on purpose: drift must be testable."""

    def __init__(self, fields: Iterable[CustomField] | None = None) -> None:
        self._lock = threading.Lock()
        self._fields: list[CustomField] = list(fields) if fields is not None else default_custom_fields()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.to_dict() for item in self._fields]

    def replace(self, fields: Iterable[dict[str, Any] | CustomField]) -> list[dict[str, Any]]:
        parsed: list[CustomField] = []
        for item in fields:
            if isinstance(item, CustomField):
                parsed.append(item)
            else:
                parsed.append(
                    CustomField(
                        id=str(item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        fieldKey=str(item.get("fieldKey") or item.get("key") or ""),
                        dataType=str(item.get("dataType") or "TEXT"),
                    )
                )
        with self._lock:
            self._fields = parsed
            return [item.to_dict() for item in self._fields]

    def retype(self, field_key: str, data_type: str) -> bool:
        with self._lock:
            for item in self._fields:
                if item.fieldKey == field_key:
                    item.dataType = data_type
                    return True
            return False

    def drop(self, field_key: str) -> bool:
        with self._lock:
            before = len(self._fields)
            self._fields = [item for item in self._fields if item.fieldKey != field_key]
            return len(self._fields) != before


class ContactStore:
    def __init__(self, location_id: str = "loc_test") -> None:
        self.location_id = location_id
        self._contacts: dict[str, Contact] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self.created = 0
        self.updated = 0
        self.custom_field_writes = 0

    # -- writes --------------------------------------------------------------

    def upsert(self, payload: dict[str, Any]) -> tuple[Contact, bool]:
        location = payload.get("locationId") or self.location_id
        email = (payload.get("email") or "").strip().lower() or None
        phone = (payload.get("phone") or "").strip() or None
        with self._lock:
            existing = self._find(email, phone)
            created = existing is None
            if existing is None:
                contact = Contact(id=str(uuid.uuid4()), locationId=location)
                self._contacts[contact.id] = contact
                self._order.append(contact.id)
                self.created += 1
            else:
                contact = existing
                self.updated += 1
            if email:
                contact.email = email
            if phone:
                contact.phone = phone
            for attr in ("firstName", "lastName"):
                if payload.get(attr):
                    setattr(contact, attr, payload[attr])
            if isinstance(payload.get("tags"), list):
                merged = list(dict.fromkeys([*contact.tags, *payload["tags"]]))
                contact.tags = merged
            if isinstance(payload.get("customFields"), list):
                self._merge_custom_fields(contact, payload["customFields"])
            return contact, created

    def _merge_custom_fields(self, contact: Contact, incoming: list[Any]) -> None:
        """Replace by field id, the way GHL upsert behaves."""

        merged: dict[str, dict[str, Any]] = {
            str(item.get("id")): dict(item)
            for item in contact.customFields
            if isinstance(item, dict) and item.get("id")
        }
        changed = 0
        for item in incoming:
            if not isinstance(item, dict):
                continue
            field_id = item.get("id")
            if not field_id:
                continue
            entry = {"id": str(field_id), "value": item.get("value")}
            if (merged.get(str(field_id)) or {}).get("value") != entry["value"]:
                changed += 1
            merged[str(field_id)] = entry
        if changed:
            contact.customFields = list(merged.values())
            self.custom_field_writes += changed

    def _find(self, email: str | None, phone: str | None) -> Contact | None:
        for cid in self._order:
            contact = self._contacts[cid]
            if email and contact.email == email:
                return contact
            if phone and contact.phone == phone:
                return contact
        return None

    # -- reads ---------------------------------------------------------------

    def get(self, contact_id: str) -> Contact | None:
        return self._contacts.get(contact_id)

    def search(
        self,
        *,
        location_id: str | None = None,
        query: str | None = None,
        page_limit: int = 100,
        start_after_id: str | None = None,
    ) -> dict[str, Any]:
        page_limit = max(1, min(int(page_limit or 100), 100))
        with self._lock:
            ids = list(self._order)
            contacts = [self._contacts[i] for i in ids]
        if location_id:
            contacts = [c for c in contacts if c.locationId == location_id]
        if query:
            needle = query.strip().lower()
            contacts = [
                c
                for c in contacts
                if needle
                in " ".join(
                    filter(
                        None,
                        [c.email, c.phone, c.firstName, c.lastName],
                    )
                ).lower()
            ]
        if start_after_id:
            seen = {c.id for c in contacts}
            if start_after_id in seen:
                index = next(i for i, c in enumerate(contacts) if c.id == start_after_id)
                contacts = contacts[index + 1 :]

        page = contacts[:page_limit]
        has_more = len(contacts) > page_limit
        meta: dict[str, Any] = {"total": len(page)}
        if has_more and page:
            meta["startAfterId"] = page[-1].id
            meta["nextCursor"] = page[-1].id
        return {"contacts": [c.to_dict() for c in page], "meta": meta}

    def all_contacts(self) -> Iterable[Contact]:
        return [self._contacts[i] for i in self._order]

    def reset(self) -> None:
        with self._lock:
            self._contacts.clear()
            self._order.clear()
            self.created = 0
            self.updated = 0
            self.custom_field_writes = 0
