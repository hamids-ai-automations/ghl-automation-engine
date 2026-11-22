"""MCP server: JSON-RPC over stdio, with the governance interceptor in front of every call.

Protocol handling is deliberately dull — ``initialize``, ``tools/list``, ``tools/call``,
``ping``. The interesting code is ``call_tool``, which is the only path to a handler and
runs the same five checks in the same order for every tool:

1. the tool exists (an unknown name is an error, never an empty success)
2. the arguments match the schema
3. no argument names a different sub-account
4. the session budget allows another call
5. for a real write: the mode allows it *and* a single-use approval token verifies

A ``dry_run`` write skips check 5 and runs as a read. That is not a loophole: the handler
is written so the dry-run branch returns before any mutating call, and the test suite
asserts the mock saw no request.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, IO, Mapping

from ghl_client import GhlClient, GhlError, PrivateIntegrationToken

from .audit import AuditLog
from .errors import (
    ApprovalRequired,
    InvalidInput,
    McpToolError,
    ReadOnlyMode,
    UpstreamFailure,
)
from .governance import (
    READ,
    RESERVED_ARGUMENTS,
    WRITE,
    ApprovalAuthority,
    LocationGuard,
    Mode,
    SessionBudget,
    args_digest,
    result_digest,
)
from .projection import DEFAULT_CONTACT_ALLOWLIST
from .registry import ToolRegistry
from .schema import validate
from .tools import build_registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ghl-mcp"
SERVER_VERSION = "0.1.0"


class McpServer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        mode: Mode = Mode.READ_ONLY,
        authority: ApprovalAuthority | None = None,
        budget: SessionBudget | None = None,
        guard: LocationGuard | None = None,
        audit: AuditLog | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry
        self.mode = mode
        self.authority = authority
        self.budget = budget or SessionBudget()
        self.guard = guard or LocationGuard(None)
        self.audit = audit or AuditLog()
        self._clock = clock

    # ------------------------------------------------------------------ tool calls

    def _error_result(
        self,
        error: McpToolError,
        *,
        tool: str,
        scope: str,
        args_hash: str | None,
        started: float,
    ) -> dict[str, Any]:
        self.audit.record(
            tool=tool,
            decision=self._decision_for(error),
            scope=scope,
            mode=self.mode.value,
            args_hash=args_hash,
            duration_ms=(self._clock() - started) * 1000,
            reason=error.reason,
            details=error.details or None,
        )
        return {
            "content": [{"type": "text", "text": json.dumps({"error": error.as_dict()})}],
            "structuredContent": {"error": error.as_dict()},
            "isError": True,
        }

    @staticmethod
    def _decision_for(error: McpToolError) -> str:
        return {
            "unknown_tool": "denied_unknown_tool",
            "invalid_input": "denied_invalid_input",
            "read_only_mode": "denied_read_only",
            "approval_required": "denied_approval_required",
            "approval_invalid": "denied_approval_invalid",
            "approval_expired": "denied_approval_expired",
            "approval_reused": "denied_approval_reused",
            "cross_location": "denied_cross_location",
            "session_budget_exhausted": "denied_budget",
        }.get(error.reason, "error")

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        started = self._clock()
        arguments = dict(arguments or {})
        business_args = {
            key: value for key, value in arguments.items() if key not in RESERVED_ARGUMENTS
        }

        try:
            spec = self.registry.get(name)
        except McpToolError as exc:
            return self._error_result(
                exc, tool=name, scope="unknown", args_hash=None, started=started
            )

        dry_run_write = bool(spec.supports_dry_run and business_args.get("dry_run") is True)
        effective_scope = READ if dry_run_write else spec.scope
        digest = args_digest(business_args)

        try:
            problems = validate(business_args, spec.input_schema)
            if problems:
                raise InvalidInput(
                    "arguments do not match the tool schema",
                    problems=problems,
                    tool=name,
                )
            if spec.requires_location:
                self.guard.require_bound(tool=name)
            self.guard.check(business_args)
            self.budget.check(effective_scope)
            if effective_scope == WRITE:
                if not self.mode.allows_writes:
                    raise ReadOnlyMode(
                        "this server is read-only; writes require GHL_MCP_MODE=read-write "
                        "and a per-call approval token",
                        tool=name,
                        mode=self.mode.value,
                    )
                token = arguments.get("approval_token")
                if not token:
                    raise ApprovalRequired(
                        "a mutating call requires an approval token issued for this tool and "
                        "these arguments; re-run with dry_run=true to preview instead",
                        tool=name,
                    )
                assert self.authority is not None, "write mode requires an approval authority"
                self.authority.verify(str(token), name, business_args)
        except McpToolError as exc:
            return self._error_result(
                exc, tool=name, scope=effective_scope, args_hash=digest, started=started
            )

        self.budget.consume(effective_scope)

        try:
            payload = spec.handler(business_args)
        except McpToolError as exc:
            return self._error_result(
                exc, tool=name, scope=effective_scope, args_hash=digest, started=started
            )
        except GhlError as exc:
            return self._error_result(
                UpstreamFailure(f"{name} failed: {exc}", error_type=type(exc).__name__),
                tool=name,
                scope=effective_scope,
                args_hash=digest,
                started=started,
            )

        self.audit.record(
            tool=name,
            decision="ok",
            scope=effective_scope,
            mode=self.mode.value,
            args_hash=digest,
            result_hash=result_digest(payload),
            duration_ms=(self._clock() - started) * 1000,
            details={"dry_run": dry_run_write} if dry_run_write else None,
        )
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
            "isError": False,
        }

    # ------------------------------------------------------------------ JSON-RPC

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        if message.get("jsonrpc") != "2.0":
            return _rpc_error(message.get("id"), -32600, "expected jsonrpc 2.0")
        method = message.get("method")
        message_id = message.get("id")

        if isinstance(method, str) and method.startswith("notifications/"):
            return None

        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            return _rpc_result(
                message_id,
                {
                    "protocolVersion": requested or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Read-only by default. Mutating tools require an approval token. "
                        "Every write supports dry_run=true. Call server_governance for the "
                        "current policy and the list of tools that are not implemented."
                    ),
                },
            )

        if method == "ping":
            return _rpc_result(message_id, {})

        if method == "tools/list":
            return _rpc_result(
                message_id, {"tools": [spec.public() for spec in self.registry.specs()]}
            )

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if not isinstance(name, str):
                return _rpc_error(message_id, -32602, "tools/call requires a tool name")
            return _rpc_result(message_id, self.call_tool(name, params.get("arguments")))

        return _rpc_error(message_id, -32601, f"unknown method: {method!r}")

    def serve(self, instream: IO[str] | None = None, outstream: IO[str] | None = None) -> None:
        source = instream or sys.stdin
        sink = outstream or sys.stdout
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: dict[str, Any] | None = _rpc_error(None, -32700, f"parse error: {exc}")
            else:
                response = self.handle(message)
            if response is not None:
                sink.write(json.dumps(response) + "\n")
                sink.flush()


def _rpc_result(message_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": dict(result)}


def _rpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def create_server(
    client: GhlClient,
    *,
    mode: Mode | None = None,
    approval_secret: str | bytes | None = None,
    max_calls: int = 200,
    max_writes: int = 20,
    audit_path: str | None = None,
    allowlist: tuple[str, ...] = DEFAULT_CONTACT_ALLOWLIST,
    clock: Callable[[], float] = time.time,
) -> McpServer:
    """Assemble a server around an existing client. Read-only unless told otherwise."""

    resolved_mode = mode if mode is not None else Mode.from_env()
    authority = (
        ApprovalAuthority(approval_secret, clock=clock)
        if approval_secret is not None
        else None
    )
    if resolved_mode.allows_writes and authority is None:
        raise ValueError(
            "read-write mode needs an approval secret; without one every write would be "
            "either unauthorised or impossible, and both are the wrong answer"
        )
    registry = build_registry(client, allowlist=allowlist)
    return McpServer(
        registry,
        mode=resolved_mode,
        authority=authority,
        budget=SessionBudget(max_calls=max_calls, max_writes=max_writes),
        guard=LocationGuard(getattr(client, "location_id", None)),
        audit=AuditLog(audit_path, clock=clock),
        clock=clock,
    )


def build_client_from_env(environ: Mapping[str, str] | None = None) -> GhlClient:
    env = environ if environ is not None else os.environ
    base_url = env.get("GHL_API_BASE_URL", "").strip() or "https://services.leadconnectorhq.com"
    token = env.get("GHL_PIT_TOKEN", "").strip()
    location_id = env.get("GHL_LOCATION_ID", "").strip() or None
    credential = PrivateIntegrationToken(token) if token else None
    return GhlClient(credential=credential, base_url=base_url, location_id=location_id)


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python -m ghl_mcp`` speaks MCP on stdio."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "approve":
        return _approve_cli(args[1:])

    if not os.environ.get("GHL_PIT_TOKEN", "").strip():
        # Fail before accepting a single message. A server that starts without a token can
        # only answer 401s, and an agent will narrate those as "no contacts found".
        print(
            "GHL_PIT_TOKEN is not set; refusing to start. Set a Private Integration token "
            "and GHL_LOCATION_ID, or point GHL_API_BASE_URL at the mock.",
            file=sys.stderr,
        )
        return 2

    client = build_client_from_env()
    server = create_server(
        client,
        mode=Mode.from_env(),
        approval_secret=os.environ.get("GHL_MCP_APPROVAL_SECRET"),
        max_calls=int(os.environ.get("GHL_MCP_MAX_CALLS", "200")),
        max_writes=int(os.environ.get("GHL_MCP_MAX_WRITES", "20")),
        audit_path=os.environ.get("GHL_MCP_AUDIT_PATH") or None,
    )
    server.serve(instream=sys.stdin, outstream=sys.stdout)
    return 0



def _approve_cli(argv: list[str]) -> int:
    """``python -m ghl_mcp approve --tool upsert_contact --args '{...}'``

    Kept as a separate command so approval is a human act with its own invocation, not a
    flag the model can pass to itself.
    """

    tool = ""
    raw_args = "{}"
    ttl = 300.0
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--tool" and index + 1 < len(argv):
            tool = argv[index + 1]
            index += 2
        elif token == "--args" and index + 1 < len(argv):
            raw_args = argv[index + 1]
            index += 2
        elif token == "--ttl" and index + 1 < len(argv):
            ttl = float(argv[index + 1])
            index += 2
        else:
            print(f"unknown argument: {token}", file=sys.stderr)
            return 2
    if not tool:
        print("usage: python -m ghl_mcp approve --tool <name> --args '<json>' [--ttl 300]", file=sys.stderr)
        return 2
    secret = os.environ.get("GHL_MCP_APPROVAL_SECRET")
    if not secret:
        print("GHL_MCP_APPROVAL_SECRET is not set", file=sys.stderr)
        return 2
    authority = ApprovalAuthority(secret)
    print(authority.issue(tool, json.loads(raw_args), ttl_seconds=ttl))
    return 0

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
