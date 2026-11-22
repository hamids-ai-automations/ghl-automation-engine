"""Idempotency claims.

A claim is taken before any side effect and released on failure, so a
retry can succeed while a replay cannot run twice. Callers must not use
a claim as a result cache: the claim only answers "may I proceed".
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

__all__ = ["ClaimStore", "InMemoryClaimStore"]


@runtime_checkable
class ClaimStore(Protocol):
    def claim(self, key: str) -> bool:  # pragma: no cover - protocol
        ...

    def release(self, key: str) -> None:  # pragma: no cover - protocol
        ...

    def held(self, key: str) -> bool:  # pragma: no cover - protocol
        ...


class InMemoryClaimStore:
    """Process-local claims. Swap for Redis/Postgres in production."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: set[str] = set()
        self.taken = 0
        self.rejected = 0
        self.released = 0

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._claims:
                self.rejected += 1
                return False
            self._claims.add(key)
            self.taken += 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            if key in self._claims:
                self._claims.discard(key)
                self.released += 1

    def held(self, key: str) -> bool:
        with self._lock:
            return key in self._claims

    def __len__(self) -> int:
        with self._lock:
            return len(self._claims)
