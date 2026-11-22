"""Append-only audit log: one line per tool call.

Arguments and results are recorded as digests, never as values. An audit log that
contains the data it audits becomes a second copy of the PII problem, in a file nobody
rotated. Hashes are enough to answer the question an audit log exists to answer —
"was this call made, and was it the call that was approved?" — without holding the
contact records.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class AuditLog:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._path = Path(path) if path else None
        self._entries: list[dict[str, Any]] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        tool: str,
        decision: str,
        scope: str,
        mode: str,
        args_hash: str | None = None,
        result_hash: str | None = None,
        duration_ms: float | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": self._clock(),
            "call_id": call_id or uuid.uuid4().hex[:12],
            "tool": tool,
            "scope": scope,
            "mode": mode,
            "decision": decision,
            "args_hash": args_hash,
            "result_hash": result_hash,
        }
        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 3)
        if reason:
            entry["reason"] = reason
        if details:
            entry["details"] = details
        self._entries.append(entry)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def tail(self, count: int = 10) -> list[dict[str, Any]]:
        return self._entries[-count:]

    def decisions(self) -> list[str]:
        return [entry["decision"] for entry in self._entries]
