"""Credentials: private integration tokens and OAuth 2.0 with single-flight refresh.

The refresh problems this exists to solve, all observed in real integrations:

* 40 workers wake up, see an expired token, and all refresh at once. The provider
  rotates the refresh token, 39 of them come back with an invalid_grant, and the
  integration hard-fails.
* The refreshed token is held in RAM only, so the next process start refreshes
  again and burns the rotation budget.
* Private tokens silently expire after 90 days and every scheduled workflow stops
  at 02:00 on a Sunday with a 401 that nobody sees for a week.

Fix: one lock, double-checked expiry, persist on every successful refresh.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import GhlAuthError
from .retry import RetryPolicy, call_with_retry
from .transport import Response, Transport

PRIVATE_TOKEN_WARN_DAYS = 90
DEFAULT_REFRESH_WINDOW = 300.0


class Credential(Protocol):
    def headers(self) -> Mapping[str, str]: ...


class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...
    def save(self, state: dict[str, Any]) -> None: ...


class InMemoryTokenStore:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._state = dict(state) if state else None

    def load(self) -> dict[str, Any] | None:
        return dict(self._state) if self._state else None

    def save(self, state: dict[str, Any]) -> None:
        self._state = dict(state)


class FileTokenStore:
    """Atomic-ish JSON store, 0600 on POSIX. Writes a temp file then replaces."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)


@dataclass
class PrivateIntegrationToken:
    """Static token from a Location-level private integration.

    ``issued_at`` is optional but strongly recommended: it is the only way to warn
    before the well-known 90-day expiry instead of after.
    """

    token: str
    issued_at: float | None = None

    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def age_days(self, *, clock: Callable[[], float] = time.time) -> float | None:
        if self.issued_at is None:
            return None
        return (clock() - self.issued_at) / 86400.0

    def needs_rotation(self, *, clock: Callable[[], float] = time.time) -> bool:
        age = self.age_days(clock=clock)
        return age is not None and age >= PRIVATE_TOKEN_WARN_DAYS


class OAuthCredential:
    """OAuth token with a single-flight, persisted refresh.

    ``transport`` is only used for the token endpoint. Everything is injected so
    the refresh path is testable without a network or a real provider.
    """

    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str,
        expires_at: float,
        client_id: str,
        client_secret: str,
        token_url: str,
        transport: Transport,
        store: TokenStore | None = None,
        refresh_window: float = DEFAULT_REFRESH_WINDOW,
        clock: Callable[[], float] = time.time,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._transport = transport
        self._store = store
        self._refresh_window = refresh_window
        self._clock = clock
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self._timeout = timeout

        self._lock = threading.Lock()
        self.refresh_count = 0
        self.last_refresh_at: float | None = None

        stored = store.load() if store else None
        if stored:
            self._access_token = stored.get("access_token", self._access_token)
            self._refresh_token = stored.get("refresh_token", self._refresh_token)
            self._expires_at = float(stored.get("expires_at", self._expires_at))

    # -- state ---------------------------------------------------------------

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def expires_at(self) -> float:
        return self._expires_at

    def is_expired(self, *, skew: float = 0.0) -> bool:
        return self._clock() >= (self._expires_at - self._refresh_window + skew)

    def state(self) -> dict[str, Any]:
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
        }

    # -- public API ----------------------------------------------------------

    def access(self) -> str:
        """Valid access token, refreshing at most once across all threads."""

        if not self.is_expired():
            return self._access_token
        with self._lock:
            # Double-check: another thread may have refreshed while we waited.
            if not self.is_expired():
                return self._access_token
            self._refresh_locked()
            return self._access_token

    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.access()}"}

    # -- internals -----------------------------------------------------------

    def _refresh_locked(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        ).encode("utf-8")
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        }

        def _once() -> Response:
            return self._transport.send(
                "POST",
                self._token_url,
                headers=headers,
                body=body,
                timeout=self._timeout,
            )

        response = call_with_retry(
            _once, policy=self._retry_policy, sleep=lambda _s: None
        )
        if response.status >= 400:
            raise GhlAuthError(
                "token refresh rejected; refresh token may be rotated or revoked",
                status=response.status,
                path=self._token_url,
                body=response.body.decode("utf-8", "replace"),
            )
        payload = response.json() or {}
        access = payload.get("access_token")
        if not access:
            raise GhlAuthError(
                "token endpoint returned no access_token",
                status=response.status,
                path=self._token_url,
            )
        self._access_token = access
        # Providers may or may not rotate the refresh token. Only replace when given.
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        expires_in = float(payload.get("expires_in", 3600))
        self._expires_at = self._clock() + expires_in
        self.refresh_count += 1
        self.last_refresh_at = self._clock()
        if self._store is not None:
            self._store.save(self.state())
