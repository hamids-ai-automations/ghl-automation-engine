"""Transport-agnostic intake.

:meth:`Receiver.handle` takes headers and raw bytes and returns an :class:`Intake`. It
does not know about Flask, FastAPI, Lambda or anything else, and it never returns a
success for something it did not process.

Order of checks is deliberate and load-bearing:

1. **size** — refuse oversized bodies before touching them
2. **signature** — nothing is parsed until the bytes are proven to come from HighLevel
3. **timestamp window** — a valid signature over a month-old timestamp is still a replay
4. **parse** — only now does the payload shape matter
5. **replay claim** — after every other check, so an attacker cannot burn event ids

Claiming before verification would let anyone with a captured signature lock out the
real delivery. Claiming before parsing would need a key we do not yet have.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .errors import WebhookError, WebhookSignatureError
from .events import Event, parse_event
from .replay import ReplayGuard
from .signature import SignatureConfig, SignatureVerifier


class IntakeStatus(str, Enum):
    """Every possible outcome of a delivery."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Intake:
    """The result of one delivery, with the reason it landed there."""

    status: IntakeStatus
    http_status: int
    reason: str
    event: Event | None = None
    matched_config: str | None = None
    error: WebhookError | None = None

    @property
    def should_process(self) -> bool:
        """True only when the caller has work to do.

        A duplicate is a ``200`` to the sender but ``False`` here — the two questions
        "did you receive it" and "should you act on it" have different answers.
        """
        return self.status is IntakeStatus.ACCEPTED

    def __str__(self) -> str:  # pragma: no cover - trivial
        detail = f"{self.status.value} ({self.http_status}): {self.reason}"
        if self.event:
            detail += f" [{self.event.type} id={self.event.id}]"
        return detail


class Receiver:
    """Verifies, de-duplicates and parses HighLevel webhook deliveries."""

    def __init__(
        self,
        secrets: list[str] | tuple[str, ...],
        *,
        configs: tuple[SignatureConfig, ...] | None = None,
        replay: ReplayGuard | None = None,
        max_body_bytes: int = 1_000_000,
        require_signature: bool = True,
    ) -> None:
        if not require_signature:
            # Allowed for local experiments, but it must be asked for explicitly.
            self._verifier: SignatureVerifier | None = None
        else:
            self._verifier = SignatureVerifier(
                secrets, configs=configs or (SignatureConfig(),)
            )
        self._replay = replay or ReplayGuard()
        self._max_body = max_body_bytes
        self._require_signature = require_signature

    @property
    def verifier(self) -> SignatureVerifier | None:
        return self._verifier

    def handle(
        self,
        headers: Mapping[str, str] | dict[str, str],
        body: bytes,
    ) -> Intake:
        """Process one delivery. Never raises for a rejected event."""
        try:
            return self._handle(dict(headers), body)
        except WebhookError as exc:
            return Intake(
                status=IntakeStatus.REJECTED,
                http_status=getattr(exc, "status_code", 400),
                reason=str(exc),
                error=exc,
            )

    def _handle(self, headers: dict[str, str], body: bytes) -> Intake:
        if len(body) > self._max_body:
            raise WebhookError(
                "webhook body exceeds the size limit",
                size=len(body),
                limit=self._max_body,
            ) from None

        lowered = {k.lower(): v for k, v in headers.items()}
        timestamp = None
        matched: SignatureConfig | None = None

        if self._verifier is not None:
            if not self._verifier.looks_signed(headers):
                raise WebhookSignatureError(
                    "webhook is unsigned and this receiver requires signatures",
                    headers_seen=",".join(sorted(lowered)),
                )
            matched = self._verifier.verify(body, headers)
            if matched.timestamp_header:
                timestamp = lowered.get(matched.timestamp_header.lower())

        event = parse_event(body)

        # Prefer the signed transport timestamp; fall back to the payload's own field
        # only when the signature framing does not carry one.
        window_value = timestamp if timestamp is not None else event.timestamp
        if self._verifier is not None or window_value is not None:
            self._replay.check_timestamp(window_value)

        try:
            self._replay.claim(event.replay_key)
        except WebhookError as exc:
            duplicate = getattr(exc, "status_code", 0) == 409
            return Intake(
                # A duplicate is acknowledged with 200 so the sender stops retrying,
                # but should_process stays False: received is not the same as acted on.
                status=IntakeStatus.DUPLICATE if duplicate else IntakeStatus.REJECTED,
                http_status=200 if duplicate else getattr(exc, "status_code", 400),
                reason=str(exc),
                event=event,
                matched_config=matched.label if matched else None,
                error=exc,
            )

        note = "event accepted"
        if not event.is_known:
            note = f"event accepted; unrecognised type {event.type!r} preserved for the caller"
        return Intake(
            status=IntakeStatus.ACCEPTED,
            http_status=200,
            reason=note,
            event=event,
            matched_config=matched.label if matched else None,
        )


def body_digest(body: bytes) -> str:
    """SHA-256 of the raw body, useful as a log correlation value."""
    return hashlib.sha256(body).hexdigest()


def adapter_response(intake: Intake) -> dict[str, Any]:
    """A minimal JSON body an HTTP adapter can return as-is."""
    payload: dict[str, Any] = {
        "status": intake.status.value,
        "process": intake.should_process,
    }
    if intake.event is not None:
        payload["type"] = intake.event.type
        payload["id"] = intake.event.id
    if intake.status is not IntakeStatus.ACCEPTED:
        payload["reason"] = intake.reason
    return payload
