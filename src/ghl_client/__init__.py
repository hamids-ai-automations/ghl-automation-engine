"""ghl-client: a HighLevel v2 client that cannot silently truncate results.

Public surface is intentionally small.
"""

from __future__ import annotations

from .auth import (
    InMemoryTokenStore,
    OAuthCredential,
    PrivateIntegrationToken,
    TokenStore,
)
from .client import DEFAULT_API_VERSION, DEFAULT_BASE_URL, GhlClient, redact
from .errors import (
    GhlApiError,
    GhlAuthError,
    GhlError,
    GhlPaginationError,
    GhlPartialResultError,
    GhlRateLimitError,
    GhlServerError,
    GhlTimeoutError,
    GhlTransportError,
    GhlValidationError,
)
from .pagination import Collection, Page, paginate
from .ratelimit import SlidingWindowLimiter
from .resources.contacts import ContactsResource, IdempotencyLedger
from .retry import RetryPolicy, call_with_retry, parse_retry_after
from .transport import MockTransport, Response, Transport, UrllibTransport

__version__ = "0.1.0"

__all__ = [
    "GhlClient",
    "ContactsResource",
    "IdempotencyLedger",
    "OAuthCredential",
    "PrivateIntegrationToken",
    "InMemoryTokenStore",
    "TokenStore",
    "Collection",
    "Page",
    "paginate",
    "SlidingWindowLimiter",
    "RetryPolicy",
    "call_with_retry",
    "parse_retry_after",
    "Response",
    "Transport",
    "UrllibTransport",
    "MockTransport",
    "redact",
    "GhlError",
    "GhlApiError",
    "GhlAuthError",
    "GhlRateLimitError",
    "GhlServerError",
    "GhlValidationError",
    "GhlTransportError",
    "GhlTimeoutError",
    "GhlPaginationError",
    "GhlPartialResultError",
    "DEFAULT_BASE_URL",
    "DEFAULT_API_VERSION",
    "__version__",
]
