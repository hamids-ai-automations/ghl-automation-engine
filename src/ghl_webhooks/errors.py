"""Webhook-specific errors.

These subclass :class:`ghl_client.errors.GhlError` so that a single ``except GhlError``
at the edge of an application catches both API and webhook failures.
"""

from __future__ import annotations

from ghl_client.errors import GhlError


class WebhookError(GhlError):
    """Base class for every webhook rejection."""

    status_code = 400


class WebhookSignatureError(WebhookError):
    """The signature did not verify against any configured secret."""

    status_code = 401


class WebhookTimestampError(WebhookError):
    """The event timestamp is outside the accepted skew window."""

    status_code = 401


class WebhookReplayError(WebhookError):
    """The event was already accepted once."""

    status_code = 409


class WebhookPayloadError(WebhookError):
    """The body could not be parsed as an event, or exceeded the size limit."""

    status_code = 400
