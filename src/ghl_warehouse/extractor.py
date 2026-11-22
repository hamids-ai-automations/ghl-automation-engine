"""Incremental contact extractor (GHL-025).

Ordering matters and is the whole design:

1. read the watermark;
2. walk every page of the source, refusing a partial result;
3. load rows;
4. *then* move the watermark, and only as far as what was loaded.

Step 4 after step 3 is what makes a crash mid-load safe: the next run re-reads the
same window and the sink's hash check turns the replay into a no-op. Doing it the
other way round would lose exactly the rows that were in flight.

Cost is reported, not hidden. Today the source is walked in full and filtered
client-side against the scan window, because filtering by ``dateUpdated``
server-side has not been verified against a live sub-account yet; the scan window
still prevents redundant writes and keeps the load idempotent. ``stats.pages`` and
``stats.scanned`` exist so that cost is measurable before anyone claims it is
cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .schema import contact_key
from .sink import ContactRecord, ContactSink, InvalidRecordError, normalise_contact
from .watermark import (
    DEFAULT_OVERLAP_SECONDS,
    WatermarkStore,
    advance,
    parse_timestamp,
)

__all__ = [
    "Collection",
    "ContactSource",
    "ExtractStats",
    "IncompleteExtractError",
    "IncrementalContactExtractor",
]


class IncompleteExtractError(Exception):
    """The source could not be exhausted. Nothing is loaded and nothing advances."""


class Collection(Protocol):
    """The subset of ``ghl_client.pagination.Collection`` this module relies on."""

    items: list[dict[str, Any]]
    pages_fetched: int
    complete: bool
    truncated_reason: str | None

    def __len__(self) -> int: ...


class ContactSource(Protocol):
    """Anything that can page contacts, including the mock server and ``ContactsResource``."""

    def search(self, **kwargs: Any) -> Collection: ...


@dataclass
class ExtractStats:
    entity: str = "contacts"
    pages: int = 0
    scanned: int = 0
    accepted: int = 0
    rejected: int = 0
    outside_window: int = 0
    new_versions: int = 0
    unchanged: int = 0
    stale_ignored: int = 0
    watermark_before: str = ""
    watermark_after: str = ""
    watermark_moved: bool = False
    dry_run: bool = False
    rejections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "pages": self.pages,
            "scanned": self.scanned,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "outside_window": self.outside_window,
            "new_versions": self.new_versions,
            "unchanged": self.unchanged,
            "stale_ignored": self.stale_ignored,
            "watermark_before": self.watermark_before,
            "watermark_after": self.watermark_after,
            "watermark_moved": self.watermark_moved,
            "dry_run": self.dry_run,
            "rejections": list(self.rejections),
        }


class IncrementalContactExtractor:
    def __init__(
        self,
        source: ContactSource,
        sink: ContactSink,
        watermarks: WatermarkStore,
        *,
        entity: str = "contacts",
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
        *,
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
                "source collection is incomplete "
                f"({collection.truncated_reason or 'unknown reason'}); refusing to load "
                "a partial extract and leave the watermark untouched"
            )

        loaded: list[ContactRecord] = []
        high: tuple[str, str] | None = None

        for raw in collection.items:
            stats.scanned += 1
            try:
                record = normalise_contact(raw)
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
            candidate = (parse_timestamp(record.updated_at).isoformat(), record.contact_id)
            if high is None or candidate > high:
                high = candidate

        if dry_run:
            stats.new_versions = len(loaded)
            stats.watermark_after = stats.watermark_before
            return stats

        for record in loaded:
            result = self._sink.load_contact(record, observed_at=observed_at)
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
        """Deterministic keys for a batch, useful for dry-run diffing in reviews."""

        keys: list[str] = []
        for raw in records:
            record = normalise_contact(raw)
            keys.append(contact_key(record.location_id, record.contact_id))
        return tuple(keys)
