"""Offline HighLevel mock server.

Runs on stdlib ``http.server`` only, so CI needs no Docker and no network. It can
be told to fail on purpose, which is the point: a test suite that only ever sees
200 OK cannot prove the client survives 429 or a mid-pagination 500.

Endpoints
    GET  /health
    POST /contacts/upsert
    POST /contacts/search
    GET  /contacts/{id}
    GET  /locations/{locationId}/customFields
    POST /__admin/inject     {path, status, times, retry_after}
    POST /__admin/reset
    GET  /__admin/requests
    GET  /__admin/stats
    POST /__admin/fields     {customFields: [...]}  -- simulate schema drift
    POST /__admin/fields/retype {fieldKey, dataType}
    POST /__admin/fields/drop {fieldKey}
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .store import ContactStore, CustomFieldStore

DEFAULT_TOKEN = "test-token"


@dataclass
class Fault:
    status: int
    times: int = 1
    retry_after: str | None = None
    body: dict[str, Any] | None = None
    skip: int = 0


@dataclass
class MockState:
    store: ContactStore = field(default_factory=ContactStore)
    fields: CustomFieldStore = field(default_factory=CustomFieldStore)
    faults: dict[str, list[Fault]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    token: str = DEFAULT_TOKEN
    require_auth: bool = True

    def queue_fault(self, path: str, fault: Fault) -> None:
        self.faults.setdefault(path, []).append(fault)

    def take_fault(self, path: str) -> Fault | None:
        queue = self.faults.get(path)
        if not queue:
            return None
        fault = queue[0]
        if fault.skip > 0:
            # Let the first N matching requests through, then start failing.
            fault.skip -= 1
            return None
        fault.times -= 1
        if fault.times <= 0:
            queue.pop(0)
            if not queue:
                self.faults.pop(path, None)
        return fault

    def record(self, method: str, path: str, status: int) -> None:
        self.requests.append({"method": method, "path": path, "status": status})

    def reset(self) -> None:
        self.store.reset()
        self.fields = CustomFieldStore()
        self.faults.clear()
        self.requests.clear()


class _Handler(BaseHTTPRequestHandler):
    server_version = "ghl-mock/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr spam
        return

    def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", str(uuid.uuid4()))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _authorized(self) -> bool:
        if not self.state.require_auth:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.state.token}"

    # -- verbs ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        fault = self.state.take_fault(path)
        if fault is not None:
            headers = {}
            if fault.retry_after:
                headers["Retry-After"] = fault.retry_after
            self.state.record(method, path, fault.status)
            self._send(fault.status, fault.body or {"error": "injected"}, headers)
            return

        status, payload, headers = self._route(method, path, parse_qs(parsed.query))
        self.state.record(method, path, status)
        self._send(status, payload, headers)

    def _route(self, method: str, path: str, query: dict[str, list[str]]):
        state = self.state

        if path == "/health":
            return 200, {"status": "ok", "service": "ghl-mock"}, None

        if path == "/__admin/inject" and method == "POST":
            data = self._read_json()
            state.queue_fault(
                data["path"],
                Fault(
                    status=int(data.get("status", 500)),
                    times=int(data.get("times", 1)),
                    retry_after=data.get("retry_after"),
                    body=data.get("body"),
                ),
            )
            return 200, {"queued": True}, None

        if path == "/__admin/reset" and method == "POST":
            state.reset()
            return 200, {"reset": True}, None

        if path == "/__admin/requests" and method == "GET":
            return 200, {"requests": state.requests}, None

        if path == "/__admin/fields" and method == "POST":
            data = self._read_json()
            return 200, {"customFields": state.fields.replace(data.get("customFields") or [])}, None

        if path == "/__admin/fields/retype" and method == "POST":
            data = self._read_json()
            changed = state.fields.retype(str(data.get("fieldKey") or ""), str(data.get("dataType") or ""))
            return 200, {"changed": changed, "customFields": state.fields.list()}, None

        if path == "/__admin/fields/drop" and method == "POST":
            data = self._read_json()
            dropped = state.fields.drop(str(data.get("fieldKey") or ""))
            return 200, {"dropped": dropped, "customFields": state.fields.list()}, None

        if path.startswith("/locations/") and path.endswith("/customFields") and method == "GET":
            if not self._authorized():
                return 401, {"error": "unauthorized", "message": "missing or bad token"}, None
            return 200, {"customFields": state.fields.list()}, None

        if path == "/__admin/stats" and method == "GET":
            return (
                200,
                {
                    "contacts": len(list(state.store.all_contacts())),
                    "created": state.store.created,
                    "updated": state.store.updated,
                    "customFieldWrites": state.store.custom_field_writes,
                    "requests": len(state.requests),
                },
                None,
            )

        if path.startswith("/contacts"):
            if not self._authorized():
                return 401, {"error": "unauthorized", "message": "missing or bad token"}, None

        if path == "/contacts/upsert" and method == "POST":
            body = self._read_json()
            if not body.get("email") and not body.get("phone"):
                return 400, {"error": "validation", "message": "email or phone required"}, None
            contact, created = state.store.upsert(body)
            return 200, {"contact": contact.to_dict(), "new": created}, None

        if path == "/contacts/search" and method == "POST":
            body = self._read_json()
            result = state.store.search(
                location_id=body.get("locationId"),
                query=body.get("query"),
                page_limit=int(body.get("pageLimit") or 100),
                start_after_id=body.get("startAfterId"),
            )
            return 200, result, None

        if path.startswith("/contacts/") and method == "GET":
            contact_id = path.split("/", 2)[2]
            contact = state.store.get(contact_id)
            if contact is None:
                return 404, {"error": "not_found"}, None
            return 200, {"contact": contact.to_dict()}, None

        return 404, {"error": "not_found", "path": path}, None


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    state: MockState | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.state = state or MockState()  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def serve_in_thread(state: MockState | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Start on an ephemeral port. Returns (server, base_url)."""

    server = create_server(state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{port}"
