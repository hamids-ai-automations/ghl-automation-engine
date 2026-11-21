"""Replay protection.

A valid signature proves the body came from HighLevel. It does not prove the body is
new: an attacker who captures one delivery can resend it forever, and HighLevel's own
retries look identical to an attack at the byte level. Two defences are needed and both
are required here:

1. **Timestamp window** — a signed timestamp inside the request narrows the window an
   attacker can operate in. Checked in :mod:`ghl_webhooks.signature` framing and again
   in :class:`ReplayGuard`, because a signature over an old timestamp is still a valid
   signature.
2. **Seen-set** — an explicit record of event ids already accepted, so a replay inside
   the window is rejected rather than processed twice.

The seen-set is a tiny interface on purpose: in-memory for a single process or a test,
Redis or a database for anything running more than one worker. Nothing here pretends an
in-memory set is durable across restarts — see :class:`ReplayStore`.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol, runtime_checkable

from .errors import WebhookReplayError, WebhookTimestampError


@runtime_checkable
class ReplayStore(Protocol):
    """Minimal contract a durable seen-set must satisfy.

    ``remember`` must be atomic: it has to answer "was this the first time?" in the
    same operation that records the key. A ``contains`` + ``add`` pair is a race, and
    under concurrent delivery of the same event it admits both copies.
    """

    def remember(self, key: str, ttl_seconds: int) -> bool:
        """Record ``key``. Return ``True`` if it was unseen, ``False`` if already present."""
        ...


class InMemoryReplayStore:
    """Process-local seen-set with expiry.

    Correct for a single process and for tests. **Not** shared between workers and lost
    on restart; use it only where those limits are acceptable, and say so in the
    deployment notes rather than implying durability that does not exist.
    """

    def __init__(self, *, clock=time.time) -> None:
        self._entries: dict[str, float] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def remember(self, key: str, ttl_seconds: int) -> bool:
        now = self._clock()
        with self._lock:
            self._sweep(now)
            if key in self._entries:
                return False
            self._entries[key] = now + ttl_seconds
            return True

    def _sweep(self, now: float) -> None:
        expired = [k for k, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class ReplayGuard:
    """Applies the timestamp window and enforces single acceptance per event id."""

    def __init__(
        self,
        store: ReplayStore | None = None,
        *,
        tolerance_seconds: int = 300,
        ttl_seconds: int | None = None,
        clock=time.time,
    ) -> None:
        if tolerance_seconds <= 0:
            raise ValueError("tolerance_seconds must be positive")
        self._store = store if store is not None else InMemoryReplayStore(clock=clock)
        self._tolerance = tolerance_seconds
        # Keep a key long enough that a replay can never outlive the seen-set.
        self._ttl = ttl_seconds if ttl_seconds is not None else max(
            tolerance_seconds * 2, 600
        )
        self._clock = clock

    @property
    def tolerance_seconds(self) -> int:
        return self._tolerance

    def check_timestamp(self, timestamp: str | None) -> float:
        """Validate ``timestamp`` against the window and return it as epoch seconds.

        Accepts integer or float epoch seconds. Raises :class:`WebhookTimestampError`
        for anything unparseable, too old, or too far in the future — a future
        timestamp is rejected because it would extend attacker reach, not shorten it.
        """
        if timestamp is None:
            raise WebhookTimestampError(
                "webhook carried no timestamp; replay window cannot be bounded"
            )
        text = timestamp.strip()
        try:
            stamp = float(text)
        except ValueError:
            raise WebhookTimestampError(
                "webhook timestamp is not numeric", timestamp=text
            ) from None

        now = self._clock()
        drift = now - stamp
        if drift > self._tolerance:
            raise WebhookTimestampError(
                "webhook timestamp is older than the tolerance window",
                age_seconds=round(drift, 3),
                tolerance_seconds=self._tolerance,
            )
        if -drift > self._tolerance:
            raise WebhookTimestampError(
                "webhook timestamp is further in the future than the tolerance window",
                skew_seconds=round(-drift, 3),
                tolerance_seconds=self._tolerance,
            )
        return stamp

    def claim(self, key: str) -> None:
        """Record ``key`` or raise :class:`WebhookReplayError` if already seen."""
        if not key:
            raise ValueError("replay key must be a non-empty string")
        if not self._store.remember(key, self._ttl):
            raise WebhookReplayError(
                "event was already accepted; duplicate delivery ignored",
                replay_key=key,
            )
