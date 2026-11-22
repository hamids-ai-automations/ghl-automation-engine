"""Governance: the controls that stop an agent from damaging a CRM by accident.

Four controls, each with exactly one job:

``Mode``
    Read-only unless the operator turned writes on. The default denies.
``ApprovalAuthority``
    A write needs a token a human issued for *that tool and those exact arguments*,
    single-use, short-lived.
``SessionBudget``
    A runaway loop stops at a ceiling instead of hammering the platform until the
    rate limiter or the bill intervenes.
``LocationGuard``
    The server is bound to one sub-account. A location id in the arguments is a hard
    error, not something to authorise — crossing accounts is a different product.

Why tokens bound to arguments rather than a boolean: an approval that is not tied to
content is a permission, and permission is what an agent already has too much of. Hashing
the arguments to the token means the human approved *this change*, so the same token
cannot be spent on a different one.

Everything here is process-local by construction. The used-nonce set and the call
counters live in memory; a multi-process deployment needs a shared store, which is the
same follow-up the replay guard in ``ghl_webhooks`` records.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .errors import (
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalReused,
    BudgetExhausted,
    CrossLocation,
)

READ = "read"
WRITE = "write"

MODE_ENV = "GHL_MCP_MODE"
TOKEN_VERSION = "ghl1"
RESERVED_ARGUMENTS = ("approval_token",)


class Mode(str, Enum):
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Mode":
        """Read the mode from the environment, defaulting to read-only.

        An unrecognised value raises instead of falling back. Falling back to
        read-only would be safe but would hide a typo — the server would keep refusing
        writes and the operator would spend an hour on the wrong problem.
        """

        raw = (environ if environ is not None else os.environ).get(MODE_ENV, "")
        value = (raw or "").strip().lower()
        if not value:
            return cls.READ_ONLY
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"{MODE_ENV}={raw!r} is not a mode; use 'read-only' or 'read-write'"
            ) from exc

    @property
    def allows_writes(self) -> bool:
        return self is Mode.READ_WRITE


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def args_digest(arguments: Mapping[str, Any] | None) -> str:
    """Stable digest of a call's business arguments.

    Reserved control arguments are excluded so a token does not have to be reissued
    when the transport adds one.
    """

    payload = {
        key: value
        for key, value in (arguments or {}).items()
        if key not in RESERVED_ARGUMENTS
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()[:32]


def result_digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:32]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class Grant:
    """What a valid token proves."""

    tool: str
    args_hash: str
    expires_at: float
    nonce: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args_hash": self.args_hash,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }


class ApprovalAuthority:
    """Issues and redeems single-use approvals bound to one tool call."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = 300.0,
    ) -> None:
        if not secret:
            raise ValueError(
                "approval secret must not be empty: an empty key would validate every "
                "forged token"
            )
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._clock = clock
        self.ttl_seconds = ttl_seconds
        self._used: set[str] = set()

    def _sign(self, body: bytes) -> bytes:
        return hmac.new(self._secret, body, hashlib.sha256).digest()

    def issue(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        *,
        ttl_seconds: float | None = None,
        nonce: str | None = None,
    ) -> str:
        lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        grant = Grant(
            tool=tool,
            args_hash=args_digest(arguments),
            expires_at=self._clock() + lifetime,
            nonce=nonce or secrets.token_hex(8),
        )
        body = canonical(
            {
                "t": grant.tool,
                "a": grant.args_hash,
                "e": grant.expires_at,
                "n": grant.nonce,
            }
        ).encode("utf-8")
        return f"{TOKEN_VERSION}.{_b64(body)}.{_b64(self._sign(body))}"

    def verify(
        self,
        token: str,
        tool: str,
        arguments: Mapping[str, Any] | None,
    ) -> Grant:
        parts = (token or "").split(".")
        if len(parts) != 3 or parts[0] != TOKEN_VERSION:
            raise ApprovalInvalid(
                "token is not a GHL approval token",
                expected_version=TOKEN_VERSION,
            )
        try:
            body = _unb64(parts[1])
            signature = _unb64(parts[2])
            claims = json.loads(body)
        except Exception as exc:  # noqa: BLE001 - any decode failure means "not valid"
            raise ApprovalInvalid(f"token is malformed: {type(exc).__name__}") from exc

        if not hmac.compare_digest(signature, self._sign(body)):
            raise ApprovalInvalid("token signature does not verify")

        if claims.get("t") != tool:
            raise ApprovalInvalid(
                f"token was issued for tool {claims.get('t')!r}, not {tool!r}",
                issued_for=claims.get("t"),
                called= tool,
            )

        presented = args_digest(arguments)
        if claims.get("a") != presented:
            raise ApprovalInvalid(
                "token was issued for different arguments; approve the change you are "
                "actually making",
                issued_for=claims.get("a"),
                presented=presented,
            )

        if float(claims.get("e", 0)) < self._clock():
            raise ApprovalExpired(
                "approval has expired; reissue it for this call",
                expired_at=claims.get("e"),
            )

        nonce = str(claims.get("n"))
        if nonce in self._used:
            raise ApprovalReused(
                "this approval has already been spent; one approval authorises one call",
                nonce=nonce,
            )
        self._used.add(nonce)
        return Grant(
            tool=tool,
            args_hash=presented,
            expires_at=float(claims.get("e", 0)),
            nonce=nonce,
        )

    @property
    def consumed(self) -> int:
        return len(self._used)


class SessionBudget:
    """Ceiling on calls and writes for one server session."""

    def __init__(self, *, max_calls: int = 200, max_writes: int = 20) -> None:
        if max_calls < 1 or max_writes < 0:
            raise ValueError("budgets must be positive (writes may be zero)")
        self.max_calls = max_calls
        self.max_writes = max_writes
        self.calls = 0
        self.writes = 0

    def check(self, scope: str) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExhausted(
                "session call budget exhausted; the server stops rather than pushing a "
                "looping agent into the platform's rate limiter",
                max_calls=self.max_calls,
                calls=self.calls,
            )
        if scope == WRITE and self.writes >= self.max_writes:
            raise BudgetExhausted(
                "session write budget exhausted",
                max_writes=self.max_writes,
                writes=self.writes,
            )

    def consume(self, scope: str) -> None:
        self.check(scope)
        self.calls += 1
        if scope == WRITE:
            self.writes += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "calls_remaining": max(0, self.max_calls - self.calls),
            "writes": self.writes,
            "writes_remaining": max(0, self.max_writes - self.writes),
        }


_LOCATION_KEYS = frozenset({"locationid", "location_id", "location"})


class LocationGuard:
    """Rejects any argument that names a different sub-account than the bound one."""

    def __init__(self, bound_location: str | None) -> None:
        self.bound_location = bound_location

    def require_bound(self, *, tool: str) -> None:
        """A location-scoped tool on an unbound client can only guess which account it means."""

        if self.bound_location is None:
            raise CrossLocation(
                f"{tool} is location-scoped but this server has no bound sub-account; "
                "set GHL_LOCATION_ID so every call is scoped to one account",
                tool=tool,
            )

    def check(self, value: Any, *, path: str = "$") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).lower() in _LOCATION_KEYS:
                    self._compare(item, f"{path}.{key}")
                else:
                    self.check(item, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self.check(item, path=f"{path}[{index}]")

    def _compare(self, found: Any, path: str) -> None:
        if self.bound_location is None:
            raise CrossLocation(
                f"{path} names a sub-account but this server is not bound to one; "
                "location-scoped tools require a bound location",
                path=path,
                supplied=str(found),
            )
        if str(found) != str(self.bound_location):
            raise CrossLocation(
                f"{path} names sub-account {found!r}; this server is bound to "
                f"{self.bound_location!r}. Cross-account access is refused, not authorised.",
                path=path,
                supplied=str(found),
                bound=str(self.bound_location),
            )
