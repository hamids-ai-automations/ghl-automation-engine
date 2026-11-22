"""Attribution write policy.

The rule that matters: a contact's earned attribution is immutable once
written. Later touches may advance activity timestamps, but they do not
re-credit the lead. Re-crediting on every touch is how "direct" traffic
silently erases a paid campaign -- the failure this policy exists to
prevent.

Override is explicit (``overwrite_existing=True``) and off by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .events import AttributionEvent

__all__ = ["AttributionPlan", "AttributionPolicy"]

#: Fields that carry credit for the original touch. Written once.
CREDIT_KEYS: tuple[str, ...] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "landing_page",
    "gclid",
    "fbclid",
    "first_touch_at",
)

#: Timestamp that follows the latest touch instead of freezing.
ACTIVITY_KEYS: tuple[str, ...] = ("last_touch_at",)


@dataclass(frozen=True)
class AttributionPlan:
    values: Mapping[str, str]
    applied: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    overwritten: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.applied

    @property
    def changes(self) -> int:
        return len(self.applied)

    def to_dict(self) -> dict[str, object]:
        return {
            "values": dict(self.values),
            "applied": list(self.applied),
            "preserved": list(self.preserved),
            "skipped": list(self.skipped),
            "overwritten": list(self.overwritten),
        }


def _normalise(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class AttributionPolicy:
    overwrite_existing: bool = False

    def candidates(self, event: AttributionEvent) -> dict[str, str]:
        touch = event.touch
        stamp = touch.timestamp()
        out: dict[str, str] = {}

        # Direct traffic has no campaign signal to record and must never
        # stand in for one.
        if not touch.is_direct:
            for key, value in (
                ("utm_source", touch.source),
                ("utm_medium", touch.medium),
                ("utm_campaign", touch.campaign),
                ("utm_content", touch.content),
                ("landing_page", touch.landing_page),
            ):
                cleaned = _normalise(value)
                if cleaned:
                    out[key] = cleaned
        for key, value in (("gclid", touch.gclid), ("fbclid", touch.fbclid)):
            cleaned = _normalise(value)
            if cleaned:
                out[key] = cleaned

        out["first_touch_at"] = stamp
        out["last_touch_at"] = stamp
        return out

    def compute(
        self,
        existing: Mapping[str, object],
        event: AttributionEvent,
    ) -> AttributionPlan:
        candidates = self.candidates(event)
        applied: list[str] = []
        preserved: list[str] = []
        skipped: list[str] = []
        overwritten: list[str] = []

        for key, candidate in candidates.items():
            current = _normalise(existing.get(key))

            if key in ACTIVITY_KEYS:
                if current is None or candidate > current:
                    applied.append(key)
                else:
                    preserved.append(key)
                continue

            if current is None:
                applied.append(key)
                continue

            if current == candidate:
                preserved.append(key)
                continue

            if self.overwrite_existing and key in CREDIT_KEYS:
                applied.append(key)
                overwritten.append(key)
            else:
                preserved.append(key)

        for key in tuple(existing):
            if _normalise(existing.get(key)) and key not in candidates:
                skipped.append(key)

        return AttributionPlan(
            values={k: candidates[k] for k in applied},
            applied=tuple(applied),
            preserved=tuple(sorted(preserved)),
            skipped=tuple(sorted(skipped)),
            overwritten=tuple(sorted(overwritten)),
        )
