"""Escalation policy for triaged inbound messages.

GHL-022 deliverable. Two rules drive everything here:

1. A message is handed to a human whenever the system is not sure, the intent
   is one a human must own, or the contact asked for a person. Confidence is
   never a reason to keep quiet: under-specified beats over-confident.
2. Nothing is ever sent automatically. ``TriagePlan.auto_reply_allowed`` is
   ``False`` for every plan this module can produce, including the happy path;
   the worker may draft, a human approves, and that approval is logged.

Failure is closed, not open: if the classifier raises, times out or returns an
intent outside the taxonomy, the message lands in ``human_review`` with the
failure recorded as an escalation reason instead of being guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .taxonomy import INTENTS, QUEUE_HUMAN, Entity, Intent, spec

__all__ = [
    "EscalationPolicy",
    "EscalationReason",
    "HUMAN_REQUEST_MARKERS",
    "TriagePlan",
    "decide",
    "fail_closed",
    "requests_human",
]


class EscalationReason(str, Enum):
    """Why a message was taken away from automation."""

    CLASSIFIER_ERROR = "classifier_error"
    EMPTY_MESSAGE = "empty_message"
    HUMAN_ONLY_INTENT = "human_only_intent"
    HUMAN_REQUESTED = "human_requested"
    MISSING_ENTITY = "missing_entity"
    LOW_CONFIDENCE = "low_confidence"
    REPEATED_CONTACT = "repeated_contact"
    ALREADY_ESCALATED = "already_escalated"


#: Fixed order so identical inputs always produce an identical plan.
REASON_PRECEDENCE: tuple[EscalationReason, ...] = (
    EscalationReason.CLASSIFIER_ERROR,
    EscalationReason.EMPTY_MESSAGE,
    EscalationReason.HUMAN_ONLY_INTENT,
    EscalationReason.HUMAN_REQUESTED,
    EscalationReason.MISSING_ENTITY,
    EscalationReason.LOW_CONFIDENCE,
    EscalationReason.REPEATED_CONTACT,
    EscalationReason.ALREADY_ESCALATED,
)

#: Fragments that mean "stop automating this conversation". Case-folded before
#: comparison so "Speak To A Human" and "speak to a human" behave the same.
HUMAN_REQUEST_MARKERS: tuple[str, ...] = (
    "speak to a human",
    "speak with a human",
    "talk to a human",
    "talk to a person",
    "real person",
    "human agent",
    "actual human",
    "call me instead",
    "stop the bot",
    "not a bot",
)


def requests_human(text: str) -> bool:
    """True when the contact explicitly asked for a person."""

    folded = " ".join(text.casefold().split())
    return any(marker in folded for marker in HUMAN_REQUEST_MARKERS)


@dataclass(frozen=True)
class EscalationPolicy:
    """Thresholds. Defaults are the agreed GHL-022 values; change deliberately."""

    confidence_floor: float = 0.55
    max_messages_before_escalation: int = 3
    escalate_on_human_request: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence_floor <= 1.0:
            raise ValueError("confidence_floor must be between 0.0 and 1.0")
        if self.max_messages_before_escalation < 1:
            raise ValueError("max_messages_before_escalation must be at least 1")


@dataclass(frozen=True)
class TriagePlan:
    """The routing decision for one message. Pure data: no side effects."""

    intent: Intent
    confidence: float
    queue: str
    escalate: bool
    reasons: tuple[EscalationReason, ...] = ()

    @property
    def auto_reply_allowed(self) -> bool:
        """Always false. Drafts require human approval before anything is sent."""

        return False

    @property
    def draft_allowed(self) -> bool:
        """A draft is worth writing only for messages automation still owns."""

        return not self.escalate

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 4),
            "queue": self.queue,
            "escalate": self.escalate,
            "reasons": [reason.value for reason in self.reasons],
            "auto_reply_allowed": self.auto_reply_allowed,
            "draft_allowed": self.draft_allowed,
        }


def _ordered(reasons: Iterable[EscalationReason]) -> tuple[EscalationReason, ...]:
    found = set(reasons)
    return tuple(reason for reason in REASON_PRECEDENCE if reason in found)


def decide(
    intent: Intent,
    confidence: float,
    *,
    text: str = "",
    present_entities: Iterable[Entity] = (),
    message_count: int = 1,
    already_escalated: bool = False,
    policy: EscalationPolicy | None = None,
) -> TriagePlan:
    """Route a classified message.

    ``confidence`` is the classifier's score for ``intent``. Anything the
    policy cannot vouch for escalates with the reason recorded, never with the
    decision silently downgraded.
    """

    policy = policy or EscalationPolicy()

    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")

    intent = Intent(intent)
    intent_spec = spec(intent)

    reasons: list[EscalationReason] = []

    if not text.strip():
        reasons.append(EscalationReason.EMPTY_MESSAGE)
    if intent_spec.human_only:
        reasons.append(EscalationReason.HUMAN_ONLY_INTENT)
    if policy.escalate_on_human_request and requests_human(text):
        reasons.append(EscalationReason.HUMAN_REQUESTED)

    available = set(present_entities)
    if any(entity not in available for entity in intent_spec.required_entities):
        reasons.append(EscalationReason.MISSING_ENTITY)

    if confidence < policy.confidence_floor:
        reasons.append(EscalationReason.LOW_CONFIDENCE)
    if message_count >= policy.max_messages_before_escalation:
        reasons.append(EscalationReason.REPEATED_CONTACT)
    if already_escalated:
        reasons.append(EscalationReason.ALREADY_ESCALATED)

    ordered = _ordered(reasons)
    escalate = bool(ordered)

    return TriagePlan(
        intent=intent,
        confidence=confidence,
        queue=QUEUE_HUMAN if escalate else intent_spec.queue,
        escalate=escalate,
        reasons=ordered,
    )


def fail_closed(
    reason: EscalationReason = EscalationReason.CLASSIFIER_ERROR,
    *,
    policy: EscalationPolicy | None = None,
) -> TriagePlan:
    """Plan used when classification could not happen at all.

    Keeps the failure inside the taxonomy: intent ``unclear``, confidence 0.0,
    human queue, reason recorded. Never raises on classifier output that is
    missing or malformed.
    """

    if reason not in (EscalationReason.CLASSIFIER_ERROR, EscalationReason.EMPTY_MESSAGE):
        raise ValueError("fail_closed only records classifier_error or empty_message")

    del policy  # thresholds are irrelevant once classification failed

    return TriagePlan(
        intent=Intent.UNCLEAR,
        confidence=0.0,
        queue=QUEUE_HUMAN,
        escalate=True,
        reasons=(reason,),
    )


if "unclear" not in {intent.value for intent in INTENTS}:  # pragma: no cover - import guard
    raise AssertionError("taxonomy lost the unclear intent")
