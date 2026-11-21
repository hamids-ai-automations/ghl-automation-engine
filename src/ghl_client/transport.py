"""Transport layer: the only place that knows how bytes reach the network.

Injectable by design. Tests use ``MockTransport``; the mock server demo uses the
real ``UrllibTransport`` against localhost. Neither requires a third-party
dependency, so ``git clone && pytest`` works on a clean machine.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .errors import GhlTimeoutError, GhlTransportError


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str = ""

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def request_id(self) -> str | None:
        return self.header("x-request-id")


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response: ...


class UrllibTransport:
    """Standard-library HTTP. No requests, no httpx, no install step."""

    def __init__(self, opener: Any = None) -> None:
        self._opener = opener

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        opener = self._opener or urllib.request.build_opener()
        try:
            with opener.open(request, timeout=timeout) as raw:
                return Response(
                    status=raw.status,
                    headers={k.lower(): v for k, v in raw.headers.items()},
                    body=raw.read(),
                    url=url,
                )
        except urllib.error.HTTPError as exc:  # a response arrived; not a transport fault
            return Response(
                status=exc.code,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=exc.read() if hasattr(exc, "read") else b"",
                url=url,
            )
        except (socket.timeout, TimeoutError) as exc:
            raise GhlTimeoutError(f"request timed out after {timeout}s", url=url) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
                raise GhlTimeoutError(f"request timed out after {timeout}s", url=url) from exc
            raise GhlTransportError(f"transport failure: {exc.reason}", url=url) from exc


class MockTransport:
    """Scripted transport for tests.

    ``handler`` maps a request dict to a ``Response``. Recording every request is
    what lets a test assert "the client made exactly one refresh call".
    """

    def __init__(
        self, handler: Callable[[dict[str, Any]], Response]
    ) -> None:
        self._handler = handler
        self.requests: list[dict[str, Any]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        record = {
            "method": method,
            "url": url,
            "headers": {k.lower(): v for k, v in headers.items()},
            "body": body,
            "timeout": timeout,
        }
        self.requests.append(record)
        return self._handler(record)


def json_response(
    status: int, payload: Any, *, headers: Mapping[str, str] | None = None
) -> Response:
    merged = {"content-type": "application/json"}
    if headers:
        merged.update({k.lower(): v for k, v in headers.items()})
    return Response(
        status=status,
        headers=merged,
        body=json.dumps(payload).encode("utf-8") if payload is not None else b"",
    )
