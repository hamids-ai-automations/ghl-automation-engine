"""Opportunity grain for the reporting warehouse (GHL-026).

Contacts answer "who is in the list". Opportunities answer "what is the list
worth", and that second question is where reporting warehouses usually start
lying: value is read from *current* records, so pipeline history is whatever the
last sync happened to see. Here an opportunity is versioned exactly like a
contact — same type-2 rule, same watermark contract, same refusal to guess.

The load order is copied deliberately rather than shared by inheritance: read the
mark, walk every page (a partial walk loads nothing), load rows, then advance the
mark as far as what was *loaded*. If those lines ever diverge from
:mod:`ghl_warehouse.extractor`, the tests for both extractors will disagree —
which is the alarm you want.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .extractor import Collection, ExtractStats, IncompleteExtractError
from .schema import contact_key, date_key, opportunity_key, stage_key
from .sink import InvalidRecordError
from .watermark import (
    DEFAULT_OVERLAP_SECONDS,
    WatermarkStore,
    advance,
    parse_timestamp,
)

__all__ = [
    "KNOWN_STATUSES",
    "OPEN_STATUSES",
    "InMemoryOpportunityWarehouse",
    "IncrementalOpportunityExtractor",
    "OpportunityLoadResult",
    "OpportunityRecord",
    "OpportunitySink",
    "OpportunitySource",
    "normalise_opportunity",
    "opportunity_row_hash",
]

#: Statuses the provider is documented to return. Anything else is rejected rather
#: than bucketed: a new "partially won" status silently counted as closed would
#: move every win-rate report without failing a single test.
KNOWN_STATUSES: frozenset[str] = frozenset({"open", "won", "lost", "abandoned"})
OPEN_STATUSES: frozenset[str] = frozenset({"open"})

TRACKED_ATTRIBUTES: tuple[str, ...] = (
    "name",
    "status",
    "stage_id",
    "pipeline_id",
    "assigned_to",
    "monetary_value",
    "contact_id",
)


class OpportunitySource(Protocol):
    """Anything that pages opportunities, including ``OpportunitiesResource``."""

    def search(self, **kwargs: Any) -> Collection: ...


@dataclass(frozen=True)
class OpportunityRecord:
    opportunity_id: str
    location_id: str
    updated_at: str
    created_at: str
    name: str | None = None
    status: str = "open"
    stage_id: str | None = None
    pipeline_id: str | None = None
    stage_name: str | None = None
    assigned_to: str | None = None
    contact_id: str | None = None
    monetary_value: float = 0.0

    @property
    def key(self) -> str:
        return opportunity_key(self.location_id, self.opportunity_id)

    @property
    def contact_key(self) -> str | None:
        if not self.contact_id:
            return None
        return contact_key(self.location_id, self.contact_id)

    @property
    def created_date_key(self) -> int:
        return date_key(self.created_at)

    @property
    def is_open(self) -> int:
        return 1 if self.status in OPEN_STATUSES else 0


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _money(value: Any) -> float:
    """Parse monetary value, refusing to invent one.

    ``None`` and empty are a real absence and become ``0.0`` (documented in
    ``docs/REPORTING.md``). Any other non-numeric text is a data error and raises:
    a value read as zero because it failed to parse is a report that is wrong in
    the expensive direction.
    """

    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise InvalidRecordError(f"monetaryValue cannot be a boolean: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError as exc:
        raise InvalidRecordError(f"monetaryValue is not a number: {value!r}") from exc


def normalise_opportunity(raw: dict[str, Any]) -> OpportunityRecord:
    """Map a provider payload onto the opportunity dimension, or refuse it.

    Missing ``id`` or update timestamp is fatal for the same reasons as contacts.
    An unrecognised status is fatal too — see ``KNOWN_STATUSES``.
    """

    opportunity_id = _clean(raw.get("id") or raw.get("opportunityId"))
    if not opportunity_id:
        raise InvalidRecordError("opportunity payload without an id")

    updated_at = _clean(raw.get("dateUpdated") or raw.get("updatedAt"))
    if not updated_at:
        raise InvalidRecordError(f"opportunity {opportunity_id} has no dateUpdated")

    created_at = _clean(raw.get("dateAdded") or raw.get("createdAt") or updated_at)

    raw_status = _clean(raw.get("status")) or "open"
    status = raw_status.lower()
    if status not in KNOWN_STATUSES:
        raise InvalidRecordError(
            f"opportunity {opportunity_id} has unknown status {raw_status!r}; add it to "
            "KNOWN_STATUSES with an explicit open/closed decision"
        )

    return OpportunityRecord(
        opportunity_id=opportunity_id,
        location_id=_clean(raw.get("locationId")) or "",
        updated_at=updated_at,
        created_at=created_at or updated_at,
        name=_clean(raw.get("name")),
        status=status,
        stage_id=_clean(raw.get("pipelineStageId") or raw.get("stageId")),
        pipeline_id=_clean(raw.get("pipelineId")),
        stage_name=_clean(raw.get("pipelineStageName") or raw.get("stageName")),
        assigned_to=_clean(raw.get("assignedTo") or raw.get("assignedToId")),
        contact_id=_clean(raw.get("contactId")),
        monetary_value=_money(raw.get("monetaryValue")),
    )


def opportunity_row_hash(record: OpportunityRecord) -> str:
    """Hash of tracked attributes only, for the same reason contacts exclude them.

    ``status`` and ``stage_id`` *are* tracked — a stage move is the single most
    reportable event on an opportunity — while the provider's own ``dateUpdated``
    is not, because it changes without any attribute changing.
    """

    payload = {name: getattr(record, name) for name in TRACKED_ATTRIBUTES}
    payload["monetary_value"] = round(record.monetary_value, 6)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class OpportunityLoadResult:
    opportunity_key: str
    version: int
    status: str
    attributes_changed: int = 0


class OpportunitySink(Protocol):
    def load_opportunity(
        self, record: OpportunityRecord, *, observed_at: datetime
    ) -> OpportunityLoadResult: ...


@dataclass
class InMemoryOpportunityWarehouse:
    """Reference sink for the opportunity grain.

    Same caveat as ``InMemoryWarehouse``: it proves the load rules, it is not
    storage. No report points at it.
    """

    dim_opportunity: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    fact_opportunity_version: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    dim_pipeline_stage: dict[str, dict[str, Any]] = field(default_factory=dict)
    dim_date: dict[int, dict[str, Any]] = field(default_factory=dict)
    loads: int = 0

    def current_version(self, key: str) -> dict[str, Any] | None:
        for (row_key, _), row in self.dim_opportunity.items():
            if row_key == key and row["is_current"]:
                return row
        return None

    def versions(self, key: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            row
            for (row_key, _), row in sorted(
                self.dim_opportunity.items(), key=lambda item: item[0][1]
            )
            if row_key == key
        )

    def opportunity_count(self) -> int:
        return len({key for key, _ in self.dim_opportunity})

    def open_value(self) -> float:
        return sum(
            row["monetary_value"] or 0.0
            for row in self.dim_opportunity.values()
            if row["is_current"] and row["status"] in OPEN_STATUSES
        )

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

    def _ensure_stage(self, record: OpportunityRecord) -> str | None:
        if not (record.pipeline_id and record.stage_id):
            return None
        key = stage_key(record.pipeline_id, record.stage_id)
        self.dim_pipeline_stage.setdefault(
            key,
            {
                "stage_key": key,
                "stage_id": record.stage_id,
                "pipeline_id": record.pipeline_id,
                "stage_name": record.stage_name,
                "position": None,
            },
        )
        return key

    def load_opportunity(
        self, record: OpportunityRecord, *, observed_at: datetime
    ) -> OpportunityLoadResult:
        self.loads += 1
        key = record.key
        digest = opportunity_row_hash(record)
        current = self.current_version(key)

        if current is not None:
            if record.updated_at < current["source_updated_at"]:
                return OpportunityLoadResult(
                    opportunity_key=key,
                    version=int(current["version"]),
                    status="stale_ignored",
                )
            if digest == current["row_hash"]:
                return OpportunityLoadResult(
                    opportunity_key=key,
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
                if getattr(record, name) not in (None, "", 0.0)
            )

        created_key = record.created_date_key
        observed_date = observed_at.astimezone(timezone.utc).date().isoformat()
        observed_key = date_key(observed_date)
        self._ensure_date(created_key, record.created_at[:10])
        self._ensure_date(observed_key, observed_date)
        stage = self._ensure_stage(record)

        self.dim_opportunity[(key, version)] = {
            "opportunity_key": key,
            "opportunity_id": record.opportunity_id,
            "location_id": record.location_id,
            "version": version,
            "row_hash": digest,
            "contact_key": record.contact_key,
            "name": record.name,
            "status": record.status,
            "stage_key": stage,
            "assigned_to": record.assigned_to,
            "source": None,
            "monetary_value": record.monetary_value,
            "created_date_key": created_key,
            "source_updated_at": record.updated_at,
            "valid_from": record.updated_at,
            "valid_to": None,
            "is_current": 1,
        }

        if (key, version) not in self.fact_opportunity_version:
            self.fact_opportunity_version[(key, version)] = {
                "opportunity_key": key,
                "version": version,
                "observed_date_key": observed_key,
                "monetary_value": record.monetary_value,
                "attributes_changed": changed,
                "days_since_created": _days_between(record.created_at, record.updated_at),
                "is_open": record.is_open,
                "first_seen_at": observed_at.astimezone(timezone.utc).isoformat(),
            }

        return OpportunityLoadResult(
            opportunity_key=key,
            version=version,
            status="new_version",
            attributes_changed=changed,
        )


def _days_between(start: str, end: str) -> int:
    delta = parse_timestamp(end) - parse_timestamp(start)
    return max(int(delta.total_seconds() // 86400), 0)


class IncrementalOpportunityExtractor:
    def __init__(
        self,
        source: OpportunitySource,
        sink: OpportunitySink,
        watermarks: WatermarkStore,
        *,
        entity: str = "opportunities",
        overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
        max_rejection_samples: int = 5,
    ) -> None:
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds must not be negative")
        self._source = source
        self._sink = sink
        self._watermarks = watermarks
        self.entity = entity
        self.overlap_seconds = overlap_seconds
        self.max_rejection_samples = max_rejection_samples

    def run(
        self,
        now: datetime | None = None,
        page_limit: int | None = None,
        dry_run: bool = False,
    ) -> ExtractStats:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        mark = self._watermarks.get(self.entity)
        stats = ExtractStats(
            entity=self.entity,
            watermark_before=mark.value if mark else "",
            dry_run=dry_run,
        )

        collection = self._source.search(**({"limit": page_limit} if page_limit else {}))
        stats.pages = collection.pages_fetched
        if not collection.complete:
            raise IncompleteExtractError(
                "opportunity collection is incomplete "
                f"({collection.truncated_reason or 'unknown reason'}); refusing to load a "
                "partial extract and leaving the watermark untouched"
            )

        loaded: list[OpportunityRecord] = []
        high: tuple[str, str] | None = None

        for raw in collection.items:
            stats.scanned += 1
            try:
                record = normalise_opportunity(raw)
            except InvalidRecordError as exc:
                stats.rejected += 1
                if len(stats.rejections) < self.max_rejection_samples:
                    stats.rejections.append(str(exc))
                continue
            stats.accepted += 1
            if mark is not None and not mark.covers(
                record.updated_at, overlap_seconds=self.overlap_seconds
            ):
                stats.outside_window += 1
                continue
            loaded.append(record)
            candidate = (parse_timestamp(record.updated_at).isoformat(), record.opportunity_id)
            if high is None or candidate > high:
                high = candidate

        if dry_run:
            stats.new_versions = len(loaded)
            stats.watermark_after = stats.watermark_before
            return stats

        for record in loaded:
            result = self._sink.load_opportunity(record, observed_at=observed_at)
            if result.status == "new_version":
                stats.new_versions += 1
            elif result.status == "unchanged":
                stats.unchanged += 1
            else:
                stats.stale_ignored += 1

        if high is not None:
            new_mark, moved = advance(
                mark,
                entity=self.entity,
                value=high[0],
                tie_breaker=high[1],
                now=observed_at,
                rows_loaded=stats.new_versions,
            )
            if moved:
                self._watermarks.put(new_mark)
                stats.watermark_after = new_mark.value
                stats.watermark_moved = True
            else:
                stats.watermark_after = mark.value if mark else ""
        else:
            stats.watermark_after = mark.value if mark else ""

        return stats

    def pending_keys(self, records: Iterable[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(
            opportunity_key(record.location_id, record.opportunity_id)
            for record in (normalise_opportunity(raw) for raw in records)
        )
