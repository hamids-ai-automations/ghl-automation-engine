"""Client-side rate budgeting.

HighLevel publishes 100 requests per 10 seconds per app per resource. We budget
90 so the headroom absorbs clock skew and retries instead of producing 429s.

A sliding-window log is used rather than a token bucket on purpose: it is exact
for a burst of 90 then a hard stop, and it is trivially testable with a fake
clock. Clock and sleep are injected so tests never actually wait.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        capacity: int = 90,
        window: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0")
        self.capacity = capacity
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._events: deque[float] = deque()
        self.total_waited = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def acquire(self) -> float:
        """Block until a slot is free, then record it. Returns seconds waited."""

        waited = 0.0
        while True:
            now = self._clock()
            self._prune(now)
            if len(self._events) < self.capacity:
                self._events.append(now)
                self.total_waited += waited
                return waited
            # Sleep exactly until the oldest event falls out of the window.
            delay = self._events[0] + self.window - now
            delay = max(delay, 0.001)
            self._sleep(delay)
            waited += delay

    @property
    def used(self) -> int:
        self._prune(self._clock())
        return len(self._events)

    def remaining(self) -> int:
        return max(self.capacity - self.used, 0)
