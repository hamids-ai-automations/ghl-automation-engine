"""Warehouse sinks and the type-2 load rule (GHL-025).

The rule that keeps history honest: a contact payload whose tracked attributes
hash to the same value as the current version is **not** a new version. Without
that check, a nightly full sync turns every contact into a new version every
night and the "how often does contact data actually change" question answers
itself with the sync schedule.

A payload older than the version already loaded is ignored rather than appended:
out-of-order delivery must not rewrite the timeline backwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from .schema import contact_key, date_key

__all__ = [
    "ContactRecord",
    "ContactSink",
    "InMemoryWarehouse",
    "LoadResult",
    "LoadStatus",
    "normalise_contact",
    "row_hash",
]

TRACKED_ATTRIBUTES: tuple[str, ...] = (
    "email",
    "phone",
    "first_name",
    "last_name",
    "source",
    "tag_key",
)


class InvalidRecordError(Exception):
    """A source record that cannot become a warehouse row. Reported, never guessed."""


LoadStatus = Literal["new_version", "unchanged", "stale_ignored"]


@dataclass(frozen=True)
class ContactRecord:
    contact_id: str
    location_id: str
    updated_at: str
    created_at: str
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    source: str | None = None
    stage_id: str | None = None
    pipeline_id: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return contact_key(self.location_id, self.contact_id)

    @property
    def tag_key(self) -> str | None:
        return ",".join(sorted(self.tags)) if self.tags else None

    @property
    def created_date_key(self) -> int:
        return date_key(self.created_at)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_tags(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = [str(item) for item in value]
    return tuple(sorted({part.strip().lower() for part in parts if part and part.strip()}))


def normalise_contact(raw: dict[str, Any]) -> ContactRecord:
    """Map a provider payload onto the dimension, or refuse it.

    Missing ``id`` or ``dateUpdated`` is fatal: a contact without an identity
    cannot be keyed, and one without an update timestamp cannot be ordered against
    the watermark. Both are reported as invalid rather than defaulted to
    ``now`` — a defaulted timestamp silently defeats incremental loading.
    """

    contact_id = _clean(raw.get("id") or raw.get("contactId"))
    if not contact_id:
        raise InvalidRecordError("contact payload without an id")

    updated_at = _clean(raw.get("dateUpdated") or raw.get("updatedAt"))
    if not updated_at:
        raise InvalidRecordError(f"contact {contact_id} has no dateUpdated")

    created_at = _clean(raw.get("dateAdded") or raw.get("createdAt") or updated_at)

    return ContactRecord(
        contact_id=contact_id,
        location_id=_clean(raw.get("locationId")) or "",
        updated_at=updated_at,
        created_at=created_at or updated_at,
        email=(_clean(raw.get("email")) or None),
        phone=_clean(raw.get("phone")),
        first_name=_clean(raw.get("firstName")),
        last_name=_clean(raw.get("lastName")),
        source=_clean(raw.get("source")),
        stage_id=_clean(raw.get("stageId")),
        pipeline_id=_clean(raw.get("pipelineId")),
        tags=_clean_tags(raw.get("tags")),
    )


def row_hash(record: ContactRecord) -> str:
    """Hash of the tracked attributes only.

    Volatile provider fields (``dateUpdated`` itself, last-activity counters) are
    excluded on purpose: including them would make every fetch a new version and
    the dimension would record polling frequency instead of change.
    """

    payload = {
        name: getattr(record, name)
        for name in TRACKED_ATTRIBUTES
    }
    payload["tag_key"] = record.tag_key
    payload["stage_id"] = record.stage_id
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class LoadResult:
    contact_key: str
    version: int
    status: LoadStatus
    attributes_changed: int = 0


class ContactSink(Protocol):
    """What the extractor needs from a warehouse. Kept deliberately small."""

    def load_contact(self, record: ContactRecord, *, observed_at: datetime) -> LoadResult: ...


@dataclass
class InMemoryWarehouse:
    """Reference sink: enough of a warehouse to prove the load rules.

    Holds exactly the rows the DDL in :mod:`ghl_warehouse.schema` describes, so a
    test can assert against tables rather than against a mock's call log.
    """

    dim_contact: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    fact_contact_version: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    dim_date: dict[int, dict[str, Any]] = field(default_factory=dict)
    dim_pipeline_stage: dict[str, dict[str, Any]] = field(default_factory=dict)
    loads: int = 0

    # -- helpers ---------------------------------------------------------
    def current_version(self, key: str) -> dict[str, Any] | None:
        versions = [row for (row_key, _), row in self.dim_contact.items() if row_key == key]
        for row in versions:
            if row["is_current"]:
                return row
        return None

    def versions(self, key: str) -> tuple[dict[str, Any], ...]:
        rows = [
            row
            for (row_key, _), row in sorted(
                self.dim_contact.items(), key=lambda item: item[0][1]
            )
            if row_key == key
        ]
        return tuple(rows)

    def contact_count(self) -> int:
        return len({key for key, _ in self.dim_contact})

    def _ensure_date(self, key: int, iso_date: str) -> None:
        if key in self.dim_date:
            return
        self.dim_date[key] = {
            "date_key": key,
            "calendar_date": iso_date,
            "year": key // 10000,
            "month": (key // 100) % 100,
            "day": key % 100,
            "iso_week": 0,
            "weekday": 0,
        }

    # -- load ------------------------------------------------------------
    def load_contact(self, record: ContactRecord, *, observed_at: datetime) -> LoadResult:
        self.loads += 1
        key = record.key
        digest = row_hash(record)
        current = self.current_version(key)

        if current is not None:
            if record.updated_at < current["source_updated_at"]:
                return LoadResult(
                    contact_key=key,
                    version=int(current["version"]),
                    status="stale_ignored",
                )
            if digest == current["row_hash"]:
                return LoadResult(
                    contact_key=key,
                    version=int(current["version"]),
                    status="unchanged",
                )
            current["valid_to"] = record.updated_at
            current["is_current"] = 0
            version = int(current["version"]) + 1
            changed = sum(
                1
                for name in TRACKED_ATTRIBUTES
                if current.get(name) != getattr(record, name)
            )
        else:
            version = 1
            changed = sum(
                1
                for name in TRACKED_ATTRIBUTES
                if getattr(record, name) is not None
            )

        created_key = record.created_date_key
        observed_key = date_key(observed_at.astimezone(timezone.utc).date().isoformat())
        self._ensure_date(created_key, record.created_at[:10])
        self._ensure_date(observed_key, observed_at.astimezone(timezone.utc).date().isoformat())

        if record.pipeline_id and record.stage_id:
            self.dim_pipeline_stage.setdefault(
                f"st_{record.pipeline_id}_{record.stage_id}",
                {
                    "stage_id": record.stage_id,
                    "pipeline_id": record.pipeline_id,
                    "stage_name": None,
                    "position": None,
                },
            )

        self.dim_contact[(key, version)] = {
            "contact_key": key,
            "contact_id": record.contact_id,
            "location_id": record.location_id,
            "version": version,
            "row_hash": digest,
            "email": record.email,
            "phone": record.phone,
            "first_name": record.first_name,
            "last_name": record.last_name,
            "source": record.source,
            "tag_key": record.tag_key,
            "stage_key": None,
            "created_date_key": created_key,
            "source_updated_at": record.updated_at,
            "valid_from": record.updated_at,
            "valid_to": None,
            "is_current": 1,
        }

        if (key, version) not in self.fact_contact_version:
            self.fact_contact_version[(key, version)] = {
                "contact_key": key,
                "version": version,
                "observed_date_key": observed_key,
                "attributes_changed": changed,
                "days_since_created": _days_between(record.created_at, record.updated_at),
                "first_seen_at": observed_at.astimezone(timezone.utc).isoformat(),
            }

        return LoadResult(
            contact_key=key, version=version, status="new_version", attributes_changed=changed
        )


def _days_between(start: str, end: str) -> int:
    from .watermark import parse_timestamp

    delta = parse_timestamp(end) - parse_timestamp(start)
    return max(int(delta.total_seconds() // 86400), 0)
