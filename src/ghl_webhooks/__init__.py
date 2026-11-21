"""Webhook intake for HighLevel events.

The design constraint is the same as the rest of the engine: a webhook handler that
returns ``200`` while quietly failing to process the event is worse than one that
returns ``500``. Everything here is built so the caller can always say what happened.
"""

from __future__ import annotations

from .errors import (
    WebhookError,
    WebhookPayloadError,
    WebhookReplayError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from .events import Event, EventType
from .receiver import Intake, IntakeStatus, Receiver
from .replay import InMemoryReplayStore, ReplayGuard, ReplayStore
from .signature import SignatureConfig, SignatureVerifier

__all__ = [
    "Event",
    "EventType",
    "InMemoryReplayStore",
    "Intake",
    "IntakeStatus",
    "Receiver",
    "ReplayGuard",
    "ReplayStore",
    "SignatureConfig",
    "SignatureVerifier",
    "WebhookError",
    "WebhookPayloadError",
    "WebhookReplayError",
    "WebhookSignatureError",
    "WebhookTimestampError",
]
