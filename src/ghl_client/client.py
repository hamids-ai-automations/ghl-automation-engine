"""The HTTP client: versioning, budgeting, retry, redaction, request ids.

Design note: nothing here is clever. Every request goes through the same five
steps in the same order, so behaviour is predictable in production:
budget -> send -> classify -> retry-or-raise -> return typed payload.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Mapping

from .auth import Credential
from .errors import GhlError, error_for_status
from .ratelimit import SlidingWindowLimiter
from .retry import RetryPolicy, call_with_retry, parse_retry_after
from .transport import Response, Transport, UrllibTransport

logger = logging.getLogger("ghl_client")

DEFAULT_BASE_URL = "https://services.leadconnectorhq.com"
DEFAULT_API_VERSION = "2021-07-28"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_SECRET_KEYS = {"authorization", "token", "access_token", "refresh_token", "api_key"}


def redact(value: Any) -> Any:
    """Recursively mask credentials and contact PII before anything is logged.

    Logs are the most common accidental PII leak in CRM integrations: a debug line
    prints the contact upsert body and now every email is in a log aggregator.
    """

    if isinstance(value, Mapping):
        return {
            k: ("***" if str(k).lower() in _SECRET_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        masked = _EMAIL_RE.sub("***@***", value)
        masked = _PHONE_RE.sub("***", masked)
        return masked
    return value


class GhlClient:
    def __init__(
        self,
        credential: Credential,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
        limiter: SlidingWindowLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
        user_agent: str = "ghl-automation-engine/0.1",
        location_id: str | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.transport = transport or UrllibTransport()
        self.limiter = limiter or SlidingWindowLimiter()
        self.retry_policy = retry_policy or RetryPolicy()
        self.api_version = api_version
        self.timeout = timeout
        self.user_agent = user_agent
        self.location_id = location_id
        self.log = logger_ or logger

    # -- low level -----------------------------------------------------------

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": self.credential.headers().get("Authorization", ""),
            "Version": self.api_version,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.location_id:
            headers["X-Location-Id"] = self.location_id
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        url = self.base_url + ("/" + path.lstrip("/") if path else "")
        if params:
            from urllib.parse import urlencode

            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"

        body: bytes | None = None
        extra = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            extra.setdefault("Content-Type", "application/json")

        def _send() -> Response:
            self.limiter.acquire()
            response = self.transport.send(
                method, url, headers=self._headers(extra), body=body, timeout=self.timeout
            )
            if response.status >= 400:
                retry_after = parse_retry_after(response.header("retry-after"))
                raise error_for_status(
                    response.status,
                    method=method,
                    path=path,
                    request_id=response.request_id,
                    location_id=self.location_id,
                    body=response.body.decode("utf-8", "replace"),
                    retry_after=retry_after,
                )
            return response

        self.log.debug(
            "request %s %s body=%s", method, path, redact(json_body) if json_body else None
        )
        response = call_with_retry(
            _send,
            policy=self.retry_policy,
            on_retry=lambda ev: self.log.warning(
                "retrying %s %s attempt=%s delay=%.2fs truncated=%s error=%s",
                method,
                path,
                ev.attempt,
                ev.delay,
                ev.truncated,
                type(ev.error).__name__,
            ),
        )
        self.log.debug(
            "response %s %s status=%s request_id=%s",
            method,
            path,
            response.status,
            response.request_id,
        )
        if not expect_json:
            return response.body
        return response.json()

    # -- convenience ---------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)
