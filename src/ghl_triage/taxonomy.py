"""Intent taxonomy for inbound HighLevel conversations.

GHL-022 deliverable. The taxonomy lives in code rather than prose so that the
triage worker (GHL-023), the eval harness (GHL-024) and ``docs/TRIAGE.md``
cannot drift apart: there is exactly one list of intents, and adding a ninth
intent without a definition, signals, an owner queue and a human-only flag
fails the package invariants at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Entity",
    "Intent",
    "IntentSpec",
    "INTENTS",
    "all_intents",
    "spec",
    "intents_for_queue",
    "validate_taxonomy",
]


class Intent(str, Enum):
    """The eight intents a triage decision can carry."""

    LEAD_INQUIRY = "lead_inquiry"
    APPOINTMENT = "appointment"
    CLIENT_SUPPORT = "client_support"
    BILLING = "billing"
    QUOTE_REQUEST = "quote_request"
    PARTNERSHIP = "partnership"
    SPAM_OPTOUT = "spam_optout"
    UNCLEAR = "unclear"


class Entity(str, Enum):
    """Facts the worker must know before it is allowed to act on an intent."""

    CONTACT_NAME = "contact_name"
    COMPANY = "company"
    SERVICE = "service"
    DATE_OR_TIME = "date_or_time"
    ORDER_OR_ACCOUNT = "order_or_account"
    INVOICE_OR_AMOUNT = "invoice_or_amount"


#: Queues an intent can be routed to. ``UNROUTED`` exists so that a failure
#: never quietly lands in a working queue.
QUEUE_LEADS = "leads"
QUEUE_BOOKINGS = "bookings"
QUEUE_SERVICE = "service"
QUEUE_BILLING = "billing"
QUEUE_PARTNERSHIPS = "partnerships"
QUEUE_PAPER_TRAIL = "optouts"
QUEUE_HUMAN = "human_review"


@dataclass(frozen=True)
class IntentSpec:
    """Definition of one intent plus everything the worker needs to route it.

    ``signals`` are descriptions of what a message of this kind contains, not
    literal keyword lists: keyword matching alone produces confident misroutes,
    which is exactly the failure mode this taxonomy is meant to prevent.
    """

    intent: Intent
    definition: str
    signals: tuple[str, ...]
    excludes: tuple[str, ...]
    required_entities: tuple[Entity, ...]
    queue: str
    human_only: bool = False


INTENTS: dict[Intent, IntentSpec] = {
    Intent.LEAD_INQUIRY: IntentSpec(
        intent=Intent.LEAD_INQUIRY,
        definition=(
            "A person who is not yet a customer asks whether the business can "
            "help them, usually before any price or scope is agreed."
        ),
        signals=(
            "asks whether the business offers a service at all",
            "describes a problem or goal in their own words, without a defined scope",
            "asks about availability or general pricing bands",
        ),
        excludes=(
            "names a service, a budget and a start date (that is a quote request)",
            "asks to move an existing booking (that is an appointment change)",
            "is an existing customer reporting a fault (that is client support)",
        ),
        required_entities=(Entity.CONTACT_NAME,),
        queue=QUEUE_LEADS,
    ),
    Intent.APPOINTMENT: IntentSpec(
        intent=Intent.APPOINTMENT,
        definition=(
            "The contact wants to create, move or cancel a scheduled slot, or "
            "asks what slots exist."
        ),
        signals=(
            "names a day, date or time window",
            "asks to reschedule, cancel or confirm an existing slot",
            "asks for the next available slot",
        ),
        excludes=(
            "asks for a price before choosing a slot (that is a quote request)",
            "reports that an appointment already happened and went wrong (client support)",
        ),
        required_entities=(Entity.DATE_OR_TIME,),
        queue=QUEUE_BOOKINGS,
    ),
    Intent.CLIENT_SUPPORT: IntentSpec(
        intent=Intent.CLIENT_SUPPORT,
        definition=(
            "An existing customer reports a problem with work already delivered "
            "or a service already running."
        ),
        signals=(
            "refers to work already completed, delivered or scheduled",
            "reports a fault, a missed visit, a broken campaign or a wrong deliverable",
            "asks for a fix or a repeat of work already paid for",
        ),
        excludes=(
            "disputes an invoice or asks for money back (that is billing)",
            "asks about a service never purchased (that is a lead inquiry)",
        ),
        required_entities=(Entity.ORDER_OR_ACCOUNT,),
        queue=QUEUE_SERVICE,
    ),
    Intent.BILLING: IntentSpec(
        intent=Intent.BILLING,
        definition=(
            "Money is the subject: invoices, charges, refunds, payment methods, "
            "disputed amounts or failed payments."
        ),
        signals=(
            "references an invoice, receipt, amount, charge or refund",
            "reports a failed or duplicated payment",
            "questions what they were billed for",
        ),
        excludes=(
            "asks what something would cost before buying (that is a quote request)",
            "asks for the price list with no existing charge (lead inquiry)",
        ),
        required_entities=(Entity.INVOICE_OR_AMOUNT,),
        queue=QUEUE_BILLING,
        human_only=True,
    ),
    Intent.QUOTE_REQUEST: IntentSpec(
        intent=Intent.QUOTE_REQUEST,
        definition=(
            "The contact asks for a formal price, proposal or scope for defined "
            "work, and expects a written number back."
        ),
        signals=(
            "names a scope, a volume or a deadline to be priced",
            "asks for a quote, proposal, estimate or statement of work",
            "asks what a defined package would cost",
        ),
        excludes=(
            "asks only whether the service exists (that is a lead inquiry)",
            "disputes an existing charge (that is billing)",
        ),
        required_entities=(Entity.SERVICE,),
        queue=QUEUE_LEADS,
    ),
    Intent.PARTNERSHIP: IntentSpec(
        intent=Intent.PARTNERSHIP,
        definition=(
            "A supplier, agency, recruiter or platform is pitching to the business "
            "rather than buying from it."
        ),
        signals=(
            "offers to sell, supply or resell something to the business",
            "offers a collaboration, integration or referral arrangement",
            "pitches recruitment, outsourcing or a media placement",
        ),
        excludes=(
            "asks to buy the business's own service (that is a lead inquiry)",
            "is an existing customer with a supplier-side question (client support)",
        ),
        required_entities=(Entity.COMPANY,),
        queue=QUEUE_PARTNERSHIPS,
    ),
    Intent.SPAM_OPTOUT: IntentSpec(
        intent=Intent.SPAM_OPTOUT,
        definition=(
            "The contact is not a sales conversation: an opt-out request, a stop "
            "instruction, automated bulk content, or an irrelevant message."
        ),
        signals=(
            "asks to stop, unsubscribe or be removed",
            "contains bulk marketing content or a link with no personal question",
            "carries no question the business can answer",
        ),
        excludes=(
            "an angry customer complaint, which stays client support",
            "a one-word reply that only makes sense inside an existing thread",
        ),
        required_entities=(),
        queue=QUEUE_PAPER_TRAIL,
        human_only=True,
    ),
    Intent.UNCLEAR: IntentSpec(
        intent=Intent.UNCLEAR,
        definition=(
            "The message does not support a confident choice between the other "
            "seven intents, or the classifier could not run at all."
        ),
        signals=(
            "too little text to classify",
            "mixes two intents without making one dominant",
            "the classifier failed, timed out or returned an intent outside the taxonomy",
        ),
        excludes=(
            "a short but unambiguous reply such as a slot confirmation",
            "a message that only looks odd because of typos",
        ),
        required_entities=(),
        queue=QUEUE_HUMAN,
        human_only=True,
    ),
}


def all_intents() -> tuple[Intent, ...]:
    """Every intent, in declaration order."""

    return tuple(INTENTS)


def spec(intent: Intent) -> IntentSpec:
    """Return the spec for ``intent``."""

    return INTENTS[Intent(intent)]


def intents_for_queue(queue: str) -> tuple[Intent, ...]:
    """Intents routed to ``queue``, in declaration order."""

    return tuple(i for i, s in INTENTS.items() if s.queue == queue)


def validate_taxonomy() -> None:
    """Fail loudly if the taxonomy has drifted out of its agreed shape.

    Called at import time. The checks encode GHL-022's acceptance line: eight
    intents, each with a usable definition, signals, exclusions, an owner queue
    and an explicit answer to "may a human-only intent be auto-handled".
    """

    expected = 8
    if len(INTENTS) != expected:
        raise AssertionError(f"taxonomy must hold exactly {expected} intents, found {len(INTENTS)}")

    if set(INTENTS) != set(Intent):
        raise AssertionError("INTENTS must cover every Intent member")

    for intent, item in INTENTS.items():
        if item.intent is not intent:
            raise AssertionError(f"spec key/member mismatch for {intent.value}")
        if len(item.definition) < 40:
            raise AssertionError(f"{intent.value}: definition is too thin to classify against")
        if len(item.signals) < 2:
            raise AssertionError(f"{intent.value}: needs at least two signals")
        if not item.excludes:
            raise AssertionError(f"{intent.value}: needs at least one exclusion")
        if not item.queue:
            raise AssertionError(f"{intent.value}: needs an owner queue")

    for intent in (Intent.BILLING, Intent.SPAM_OPTOUT, Intent.UNCLEAR):
        if not INTENTS[intent].human_only:
            raise AssertionError(f"{intent.value} must be human-only")

    if INTENTS[Intent.UNCLEAR].queue != QUEUE_HUMAN:
        raise AssertionError("unclear must route to human review")


validate_taxonomy()
