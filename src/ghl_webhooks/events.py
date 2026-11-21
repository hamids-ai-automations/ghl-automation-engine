"""Event parsing.

The payload shape is normalised into a small dataclass, but nothing is thrown away: the
decoded body stays on ``raw``. Unknown event types are parsed, marked ``is_known=False``
and delivered to the caller — an unrecognised type is information, not an error, and
dropping it silently is exactly the failure mode this library exists to prevent.

Field names are read from several plausible spellings because the event envelope has
varied across API versions. The first non-empty match wins and the spelling that matched
is recorded in ``field_sources``, so a caller can tell which shape actually arrived
instead of assuming.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import WebhookPayloadError


class EventType(str, Enum):
    """Known event families.

    Deliberately small. Anything outside this set still parses, as
    :attr:`Event.is_known` will report ``False``.
    """

    CONTACT_CREATE = "ContactCreate"
    CONTACT_UPDATE = "ContactUpdate"
    CONTACT_DELETE = "ContactDelete"
    OPPORTUNITY_CREATE = "OpportunityCreate"
    OPPORTUNITY_UPDATE = "OpportunityUpdate"
    OPPORTUNITY_STAGE_UPDATE = "OpportunityStageUpdate"
    APPOINTMENT_CREATE = "AppointmentCreate"
    APPOINTMENT_UPDATE = "AppointmentUpdate"
    INBOUND_MESSAGE = "InboundMessage"
    OUTBOUND_MESSAGE = "OutboundMessage"
    TASK_CREATE = "TaskCreate"
    TASK_COMPLETE = "TaskComplete"
    UNKNOWN = "Unknown"


_ID_KEYS = ("id", "eventId", "event_id", "webhookId")
_TYPE_KEYS = ("type", "event", "eventType", "event_type")
_LOCATION_KEYS = ("locationId", "location_id", "locationId._id")
_TIME_KEYS = ("timestamp", "createdAt", "created_at", "time")
_DATA_KEYS = ("data", "payload", "body")


@dataclass(frozen=True)
class Event:
    """A parsed webhook event."""

    type: str
    id: str | None
    location_id: str | None
    timestamp: str | None
    data: dict[str, Any]
    raw: dict[str, Any]
    is_known: bool
    field_sources: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type:
            raise WebhookPayloadError("event has no type")

    @property
    def replay_key(self) -> str:
        """Stable key for replay detection.

        Prefers the event id; falls back to the signed timestamp plus type. If neither
        exists the receiver refuses the event earlier, so this never returns a value
        that would collapse unrelated events onto one key.
        """
        if self.id:
            return f"id:{self.id}"
        return f"t:{self.timestamp}:{self.type}"


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, str]:
    for key in keys:
        value = payload.get(key)
        if value in (None, "", {}):
            continue
        if isinstance(value, dict):
            nested = value.get("_id") or value.get("id")
            if nested:
                return str(nested), key
            continue
        return str(value), key
    return None, ""


def parse_event(body: bytes) -> Event:
    """Parse ``body`` into an :class:`Event`.

    Raises :class:`WebhookPayloadError` only when the bytes are not a JSON object or
    carry no event type. Everything else is preserved and returned.
    """
    try:
        decoded = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError:
        raise WebhookPayloadError("webhook body is not valid UTF-8") from None
    except json.JSONDecodeError as exc:
        raise WebhookPayloadError(
            "webhook body is not valid JSON", position=str(exc.pos)
        ) from None

    if not isinstance(decoded, dict):
        raise WebhookPayloadError(
            "webhook body must be a JSON object",
            received=type(decoded).__name__,
        )

    event_type, type_key = _first(decoded, _TYPE_KEYS)
    if not event_type:
        raise WebhookPayloadError(
            "webhook body has no event type",
            keys_seen=",".join(sorted(decoded)),
        )

    event_id, id_key = _first(decoded, _ID_KEYS)
    location_id, loc_key = _first(decoded, _LOCATION_KEYS)
    timestamp, time_key = _first(decoded, _TIME_KEYS)

    data: dict[str, Any] = {}
    for key in _DATA_KEYS:
        candidate = decoded.get(key)
        if isinstance(candidate, dict):
            data = candidate
            break

    known = {member.value for member in EventType if member is not EventType.UNKNOWN}
    return Event(
        type=event_type,
        id=event_id,
        location_id=location_id,
        timestamp=timestamp,
        data=data if data else {k: v for k, v in decoded.items() if k not in {type_key}},
        raw=decoded,
        is_known=event_type in known,
        field_sources={
            k: v
            for k, v in {
                "type": type_key,
                "id": id_key,
                "location_id": loc_key,
                "timestamp": time_key,
            }.items()
            if v
        },
    )
