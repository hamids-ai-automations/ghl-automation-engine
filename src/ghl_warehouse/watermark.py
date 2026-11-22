"""Watermarks for incremental loads (GHL-025).

A watermark is the high-water mark of what has been *successfully loaded*, not of
what has been *seen*. Advancing it anywhere else is how a sync loses rows during
an outage: the extractor reads page 3, the loader dies, the mark moves on, and the
missing contacts are never asked for again.

Two deliberate properties:

* **Monotonic.** ``advance`` refuses to move backwards. A redelivered or
  out-of-order batch cannot rewind the mark and cause the next run to re-read
  yesterday.
* **Overlapped on read.** The next scan starts ``overlap_seconds`` before the
  mark, because GoHighLevel commits do not arrive in timestamp order: a record
  written at 10:00:05 can become visible after one written at 10:00:09. The
  overlap is applied when computing the scan window, never to the stored mark, so
  the mark itself stays truthful.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "DEFAULT_OVERLAP_SECONDS",
    "InMemoryWatermarkStore",
    "JsonFileWatermarkStore",
    "Watermark",
    "WatermarkStore",
    "advance",
    "parse_timestamp",
]

DEFAULT_OVERLAP_SECONDS = 300


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalise it to UTC.

    Naive input is treated as UTC rather than local time — guessing the machine's
    zone is how a nightly sync silently skips an hour twice a year.
    """

    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Watermark:
    """The point up to which an entity has been loaded."""

    entity: str
    value: str = ""
    tie_breaker: str = ""
    updated_at: str = ""
    rows_loaded: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.value

    def scan_since(
        self, *, overlap_seconds: int = DEFAULT_OVERLAP_SECONDS
    ) -> datetime | None:
        """Start of the next scan window, or ``None`` for a first (full) run."""

        if self.is_empty:
            return None
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds must not be negative")
        return parse_timestamp(self.value) - timedelta(seconds=overlap_seconds)

    def covers(self, updated_at: str, *, overlap_seconds: int = DEFAULT_OVERLAP_SECONDS) -> bool:
        """True when a record falls inside the next scan window."""

        since = self.scan_since(overlap_seconds=overlap_seconds)
        if since is None:
            return True
        return parse_timestamp(updated_at) >= since

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "value": self.value,
            "tie_breaker": self.tie_breaker,
            "updated_at": self.updated_at,
            "rows_loaded": self.rows_loaded,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Watermark":
        return cls(
            entity=str(payload.get("entity", "")),
            value=str(payload.get("value", "")),
            tie_breaker=str(payload.get("tie_breaker", "")),
            updated_at=str(payload.get("updated_at", "")),
            rows_loaded=int(payload.get("rows_loaded", 0) or 0),
        )


class WatermarkStore(Protocol):
    def get(self, entity: str) -> Watermark | None: ...

    def put(self, watermark: Watermark) -> None: ...


class InMemoryWatermarkStore:
    """Default store for tests and single-process runs."""

    def __init__(self, initial: Watermark | None = None) -> None:
        self._marks: dict[str, Watermark] = {}
        if initial is not None:
            self.put(initial)

    def get(self, entity: str) -> Watermark | None:
        return self._marks.get(entity)

    def put(self, watermark: Watermark) -> None:
        self._marks[watermark.entity] = watermark

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(sorted(self._marks))


class JsonFileWatermarkStore:
    """Durable store: atomic replace, so a crash cannot leave a half-written mark.

    Writes to a sibling temp file and ``os.replace``s it into place — on both NTFS
    and ext4 that is atomic, and a truncated JSON file is the one failure mode
    that would otherwise silently reset every watermark to empty.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ValueError(f"watermark file {self.path} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"watermark file {self.path} must contain an object")
        return payload

    def get(self, entity: str) -> Watermark | None:
        payload = self._read().get(entity)
        return Watermark.from_dict(payload) if isinstance(payload, dict) else None

    def put(self, watermark: Watermark) -> None:
        payload = self._read()
        payload[watermark.entity] = watermark.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def advance(
    current: Watermark | None,
    *,
    entity: str,
    value: str,
    tie_breaker: str = "",
    now: datetime | None = None,
    rows_loaded: int = 0,
) -> tuple[Watermark, bool]:
    """Move a watermark forward. Returns ``(mark, moved)``.

    A candidate at or behind the current mark does not move it, and the existing
    mark is returned unchanged. This is the guard that makes a replayed batch a
    no-op instead of a rewind.
    """

    candidate = _iso(parse_timestamp(value))
    stamp = _iso(now or datetime.now(timezone.utc))

    if current is None or current.is_empty:
        return Watermark(
            entity=entity,
            value=candidate,
            tie_breaker=tie_breaker,
            updated_at=stamp,
            rows_loaded=rows_loaded,
        ), True

    current_value = parse_timestamp(current.value)
    candidate_value = parse_timestamp(value)

    if candidate_value < current_value:
        return current, False
    if candidate_value == current_value and tie_breaker <= current.tie_breaker:
        return current, False

    return Watermark(
        entity=entity,
        value=candidate,
        tie_breaker=tie_breaker,
        updated_at=stamp,
        rows_loaded=rows_loaded,
    ), True
