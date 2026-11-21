"""Typed error hierarchy.

Every error carries enough context to open a support ticket without asking the
caller to reproduce the request: method, path, status, request id, location.

Rule enforced throughout the package: a partial or failed read never surfaces as
a clean success. That is the whole point of this library.
"""

from __future__ import annotations

from typing import Any


class GhlError(Exception):
    """Base error. ``retryable`` drives the retry loop and nothing else."""

    retryable: bool = False

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = {k: v for k, v in context.items() if v is not None}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


class GhlTransportError(GhlError):
    """Connection refused, DNS failure, socket reset."""

    retryable = True


class GhlTimeoutError(GhlTransportError):
    """Request exceeded its deadline."""


class GhlApiError(GhlError):
    """A response arrived and its status was not 2xx."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int,
        method: str | None = None,
        path: str | None = None,
        request_id: str | None = None,
        location_id: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status=status,
            method=method,
            path=path,
            request_id=request_id,
            location_id=location_id,
            body=(body or "")[:512] or None,
        )
        self.status = status
        self.method = method
        self.path = path
        self.request_id = request_id
        self.body = body


class GhlAuthError(GhlApiError):
    """401/403. Never retried; the credential is wrong or revoked."""


class GhlRateLimitError(GhlApiError):
    """429. Retryable, but only after ``retry_after`` has been honoured."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class GhlServerError(GhlApiError):
    """5xx from the platform."""

    retryable = True


class GhlValidationError(GhlApiError):
    """4xx that is the caller's fault: bad payload, unknown field, wrong version."""


class GhlPaginationError(GhlError):
    """The cursor did not advance, so continuing would loop forever."""


class GhlPartialResultError(GhlError):
    """A multi-page read failed after some pages succeeded.

    Deliberately an error, not a warning: silently returning page 1 of 5 is the
    exact bug this library exists to prevent.
    """

    def __init__(self, message: str, *, collected: list[Any], errors: list[GhlError]) -> None:
        super().__init__(message, collected=len(collected), failures=len(errors))
        self.collected = collected
        self.errors = errors


def error_for_status(
    status: int,
    *,
    method: str,
    path: str,
    request_id: str | None,
    location_id: str | None,
    body: str,
    retry_after: float | None = None,
) -> GhlApiError:
    """Map an HTTP status onto the hierarchy. One place, one mapping."""

    common = {
        "status": status,
        "method": method,
        "path": path,
        "request_id": request_id,
        "location_id": location_id,
        "body": body,
    }
    if status == 429:
        return GhlRateLimitError(
            "rate limited by HighLevel", retry_after=retry_after, **common
        )
    if status in (401, 403):
        return GhlAuthError("authentication rejected", **common)
    if 400 <= status < 500:
        return GhlValidationError(f"request rejected with {status}", **common)
    return GhlServerError(f"platform error {status}", **common)
