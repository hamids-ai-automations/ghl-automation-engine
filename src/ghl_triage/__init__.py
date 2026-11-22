"""ghl-triage: inbound conversations routed without guessing.

Public surface:
    Intent            -- the eight intents a message can be classified as
    IntentSpec        -- definition, signals, exclusions, entities, owner queue
    EscalationPolicy  -- confidence floor and repeat-contact threshold
    decide            -- route a classified message, recording every reason
    fail_closed       -- the plan used when classification could not run
"""

from .escalation import (
    HUMAN_REQUEST_MARKERS,
    EscalationPolicy,
    EscalationReason,
    TriagePlan,
    decide,
    fail_closed,
    requests_human,
)
from .taxonomy import (
    INTENTS,
    Entity,
    Intent,
    IntentSpec,
    all_intents,
    intents_for_queue,
    spec,
    validate_taxonomy,
)

__all__ = [
    "Entity",
    "EscalationPolicy",
    "EscalationReason",
    "HUMAN_REQUEST_MARKERS",
    "INTENTS",
    "Intent",
    "IntentSpec",
    "TriagePlan",
    "all_intents",
    "decide",
    "fail_closed",
    "intents_for_queue",
    "requests_human",
    "spec",
    "validate_taxonomy",
]
