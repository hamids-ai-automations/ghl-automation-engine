"""Attribution events: the shape we accept from form, ad, or webhook intake.

An event is a single touch plus a contact identity. Validation is strict
and aggregated: one call reports every problem, because a webhook that
silently drops a malformed field is worse than one that refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = ["AttributionEvent", "InvalidAttributionEvent", "Touch"]

_DIRECT_MARKERS = {"", "direct", "(direct)", "(none)", "none", "direct traffic"}


class InvalidAttributionEvent(ValueError):
    """Raised when an inbound payload cannot be interpreted safely."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_time(value: Any, problems: list[str], field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value in (None, ""):
        parsed = datetime.now(timezone.utc)
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            problems.append(f"{field_name}: not an ISO-8601 timestamp ({value!r})")
            return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Touch:
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    content: str | None = None
    landing_page: str | None = None
    gclid: str | None = None
    fbclid: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_direct(self) -> bool:
        """Direct traffic carries no campaign signal and must not overwrite one."""

        return (self.source or "").strip().lower() in _DIRECT_MARKERS

    @property
    def has_campaign_signal(self) -> bool:
        """True only when the touch carries something worth attributing.

        A bare ``utm_source=direct`` is not a campaign, so it must not pass as
        one — otherwise organic noise would mint contacts.
        """

        source = None if self.is_direct else self.source
        return bool(
            source or self.medium or self.campaign or self.content or self.landing_page or self.gclid or self.fbclid
        )

    def timestamp(self) -> str:
        return self.occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class AttributionEvent:
    event_id: str
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    touch: Touch = field(default_factory=Touch)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AttributionEvent":
        problems: list[str] = []

        event_id = _clean(payload.get("event_id") or payload.get("eventId"))
        if not event_id:
            problems.append("event_id: required for idempotent intake")

        email = _clean(payload.get("email"))
        if email:
            email = email.lower()
            if "@" not in email:
                problems.append(f"email: not an address ({email!r})")

        phone = _clean(payload.get("phone"))
        if not email and not phone:
            problems.append("identity: email or phone required")

        occurred_at = _parse_time(payload.get("occurred_at"), problems, "occurred_at")

        touch = Touch(
            source=_clean(payload.get("utm_source")),
            medium=_clean(payload.get("utm_medium")),
            campaign=_clean(payload.get("utm_campaign")),
            content=_clean(payload.get("utm_content")),
            landing_page=_clean(payload.get("landing_page")),
            gclid=_clean(payload.get("gclid")),
            fbclid=_clean(payload.get("fbclid")),
            occurred_at=occurred_at,
        )

        if problems:
            raise InvalidAttributionEvent(problems)

        return cls(
            event_id=str(event_id),
            email=email,
            phone=phone,
            first_name=_clean(payload.get("first_name")),
            last_name=_clean(payload.get("last_name")),
            touch=touch,
        )

    @property
    def identity(self) -> str:
        return self.email or self.phone or ""
