"""Retry policy.

Two hard rules:

1. Only retry what is safe to retry: 429, 5xx, timeouts, transport faults.
2. Honour ``Retry-After`` but cap it. A server that asks for 3600 seconds must not
   freeze a caller for an hour; we cap at ``max_delay`` (default 120 s) and surface
   the truncation in the retry event so it is never invisible.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Callable, Sequence, TypeVar

from .errors import GhlError, GhlRateLimitError

T = TypeVar("T")

DEFAULT_MAX_DELAY = 120.0


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP-date (RFC 9110 §10.2.3).

    The HTTP-date form is easy to forget and produces wildly wrong delays when
    parsed as an int.
    """

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(seconds, 0.0)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    cap_delay: float = 30.0
    max_delay: float = DEFAULT_MAX_DELAY
    jitter: bool = True

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """attempt is 1-based: the delay *after* attempt N failed."""

        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        raw = self.base_delay * (2 ** (attempt - 1))
        capped = min(raw, self.cap_delay)
        if self.jitter:
            # Full jitter: uniform in [0, capped]. Prevents synchronised retries.
            return random.uniform(0.0, capped)
        return capped


@dataclass
class RetryEvent:
    attempt: int
    delay: float
    error: GhlError
    truncated: bool = False


@dataclass
class RetryOutcome:
    attempts: int = 1
    events: list[RetryEvent] = field(default_factory=list)

    @property
    def retried(self) -> bool:
        return bool(self.events)


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[RetryEvent], None] | None = None,
) -> T:
    policy = policy or RetryPolicy()
    attempt = 1
    while True:
        try:
            return fn()
        except GhlError as exc:
            if not exc.retryable or attempt >= policy.max_attempts:
                raise
            retry_after = exc.retry_after if isinstance(exc, GhlRateLimitError) else None
            delay = policy.delay_for(attempt, retry_after)
            truncated = (
                retry_after is not None and retry_after > policy.max_delay
            )
            event = RetryEvent(
                attempt=attempt, delay=delay, error=exc, truncated=truncated
            )
            if on_retry is not None:
                on_retry(event)
            sleep(delay)
            attempt += 1


def collect_retry_events(events: list[RetryEvent]) -> Callable[[RetryEvent], None]:
    """Small helper so callers can assert on retry behaviour in tests."""

    def _sink(event: RetryEvent) -> None:
        events.append(event)

    return _sink


__all__: Sequence[str] = (
    "RetryPolicy",
    "RetryEvent",
    "RetryOutcome",
    "call_with_retry",
    "parse_retry_after",
    "collect_retry_events",
    "DEFAULT_MAX_DELAY",
)
