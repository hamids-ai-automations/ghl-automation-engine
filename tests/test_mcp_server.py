"""Protocol and interceptor: what a client sees when it asks for something it may not do.

The acceptance line from the roadmap is "write call without approval token fails closed;
read-only mode proven by test". Both are here, and so is the failure that matters most in
practice: a refusal must arrive as ``isError`` with a reason, never as a cheerful empty
result the model will narrate as fact.
"""

from __future__ import annotations

import io
import json

from ghl_client import PrivateIntegrationToken
from ghl_client.client import GhlClient
from ghl_mcp import create_server
from ghl_mcp.audit import AuditLog
from ghl_mcp.governance import LocationGuard, Mode, args_digest
from ghl_mcp.server import McpServer, _rpc_error
from ghl_mcp.tools import build_registry


def read_only(client, **kwargs):
    return create_server(client, mode=Mode.READ_ONLY, audit_path=None, **kwargs)


def read_write(client, **kwargs):
    kwargs.setdefault("approval_secret", "test-secret")
    return create_server(client, mode=Mode.READ_WRITE, **kwargs)


def rpc(server, method, params=None, message_id=1):
    return server.handle({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})


# ------------------------------------------------------------------ protocol


def test_initialize_handshake(mock_client):
    _state, client = mock_client
    result = rpc(read_only(client), "initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "ghl-mcp"
    assert "read-only" in result["instructions"].lower()


def test_notifications_get_no_response(mock_client):
    _state, client = mock_client
    assert rpc(read_only(client), "notifications/initialized") is None


def test_tools_list_carries_scope_annotations(mock_client):
    _state, client = mock_client
    tools = rpc(read_only(client), "tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "search_contacts",
        "get_contact",
        "upsert_contact",
        "rate_limit_status",
        "server_governance",
    }
    write = next(tool for tool in tools if tool["name"] == "upsert_contact")
    assert write["annotations"]["readOnlyHint"] is False
    assert write["annotations"]["risk"] == "high"
    read = next(tool for tool in tools if tool["name"] == "search_contacts")
    assert read["annotations"]["readOnlyHint"] is True


def test_unknown_method_is_a_jsonrpc_error(mock_client):
    _state, client = mock_client
    assert rpc(read_only(client), "tools/destroy")["error"]["code"] == -32601


def test_missing_jsonrpc_version_is_rejected(mock_client):
    _state, client = mock_client
    response = read_only(client).handle({"id": 1, "method": "ping"})
    assert response["error"]["code"] == -32600


def test_ping(mock_client):
    _state, client = mock_client
    assert rpc(read_only(client), "ping")["result"] == {}


# ------------------------------------------------------------------ refusals


def test_unknown_tool_is_an_error_not_an_empty_success(mock_client):
    _state, client = mock_client
    result = rpc(read_only(client), "tools/call", {"name": "delete_everything", "arguments": {}})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["reason"] == "unknown_tool"
    assert "upsert_contact" in result["structuredContent"]["error"]["details"]["available"]


def test_invalid_arguments_name_the_problem(mock_client):
    _state, client = mock_client
    result = rpc(
        read_only(client),
        "tools/call",
        {"name": "search_contacts", "arguments": {"limit": 9999}},
    )["result"]
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["reason"] == "invalid_input"
    assert any("limit" in problem for problem in error["details"]["problems"])


def test_read_only_mode_refuses_a_write(mock_client):
    _state, client = mock_client
    server = read_only(client)
    result = server.call_tool("upsert_contact", {"email": "a@b.com"})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["reason"] == "read_only_mode"
    assert "denied_read_only" in server.audit.decisions()


def test_read_only_mode_refuses_a_write_even_with_a_valid_token(mock_client):
    _state, client = mock_client
    # A token issued elsewhere must not unlock a read-only server.
    server = read_write(client)
    token = server.authority.issue("upsert_contact", {"email": "a@b.com"})
    locked = read_only(client)
    result = locked.call_tool("upsert_contact", {"email": "a@b.com", "approval_token": token})
    assert result["structuredContent"]["error"]["reason"] == "read_only_mode"


def test_write_without_approval_fails_closed(mock_client):
    _state, client = mock_client
    server = read_write(client)
    result = server.call_tool("upsert_contact", {"email": "a@b.com"})
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["reason"] == "approval_required"
    assert "dry_run" in error["message"]
    assert server.budget.writes == 0


def test_write_with_a_replayed_token_is_refused(mock_client):
    _state, client = mock_client
    server = read_write(client)
    arguments = {"email": "a@b.com"}
    token = server.authority.issue("upsert_contact", arguments)
    assert server.call_tool("upsert_contact", {**arguments, "approval_token": token})["isError"] is False
    again = server.call_tool("upsert_contact", {**arguments, "approval_token": token})
    assert again["structuredContent"]["error"]["reason"] == "approval_reused"


def test_token_issued_for_other_arguments_is_refused(mock_client):
    _state, client = mock_client
    server = read_write(client)
    token = server.authority.issue("upsert_contact", {"email": "a@b.com"})
    result = server.call_tool("upsert_contact", {"email": "c@d.com", "approval_token": token})
    assert result["structuredContent"]["error"]["reason"] == "approval_invalid"
    assert "different arguments" in result["structuredContent"]["error"]["message"]


def test_location_argument_cannot_be_smuggled_in(mock_client):
    """The schema has no locationId, so cross-account access is structurally impossible."""

    _state, client = mock_client
    server = read_write(client)
    result = server.call_tool(
        "upsert_contact",
        {"email": "a@b.com", "locationId": "loc_other", "approval_token": "x"},
    )
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["reason"] == "invalid_input"
    assert any("locationId" in problem for problem in error["details"]["problems"])


def test_unbound_client_refuses_location_scoped_calls(mock_client):
    _state, client = mock_client
    unbound = GhlClient(PrivateIntegrationToken("tok"), base_url="http://127.0.0.1:1", location_id=None)
    server = read_write(unbound)
    result = server.call_tool("search_contacts", {})
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["reason"] == "cross_location"
    assert "no bound sub-account" in error["message"]


def test_unbound_client_can_still_read_local_status(mock_client):
    _state, client = mock_client
    unbound = GhlClient(PrivateIntegrationToken("tok"), base_url="http://127.0.0.1:1", location_id=None)
    assert read_write(unbound).call_tool("server_governance", {})["isError"] is False


def test_budget_exhaustion_stops_the_session(mock_client):
    _state, client = mock_client
    server = read_write(client, max_writes=1)
    first = {"email": "one@example.com"}
    server.call_tool(
        "upsert_contact", {**first, "approval_token": server.authority.issue("upsert_contact", first)}
    )
    second = {"email": "two@example.com"}
    result = server.call_tool(
        "upsert_contact", {**second, "approval_token": server.authority.issue("upsert_contact", second)}
    )
    assert result["structuredContent"]["error"]["reason"] == "session_budget_exhausted"
    assert "denied_budget" in server.audit.decisions()


def test_dry_run_is_allowed_in_read_only_mode(mock_client):
    _state, client = mock_client
    result = read_only(client).call_tool("upsert_contact", {"email": "x@y.com", "dry_run": True})
    assert result["isError"] is False
    assert result["structuredContent"]["mutated"] is False


# ------------------------------------------------------------------ audit


def test_audit_log_holds_digests_not_arguments(mock_client):
    _state, client = mock_client
    audit = AuditLog()
    server = McpServer(
        build_registry(client),
        mode=Mode.READ_ONLY,
        guard=LocationGuard("loc_test"),
        audit=audit,
    )
    server.call_tool("search_contacts", {"query": "secret@example.com"})

    entry = audit.entries[-1]
    assert entry["tool"] == "search_contacts"
    assert entry["decision"] == "ok"
    assert entry["args_hash"] == args_digest({"query": "secret@example.com"})
    assert "secret@example.com" not in json.dumps(audit.entries)


def test_audit_records_refusals_with_a_reason(mock_client):
    _state, client = mock_client
    server = read_only(client)
    server.call_tool("upsert_contact", {"email": "a@b.com"})
    entry = server.audit.entries[-1]
    assert entry["decision"] == "denied_read_only"
    assert entry["reason"] == "read_only_mode"
    assert entry["scope"] == "write"


def test_audit_file_is_appended(tmp_path, mock_client):
    _state, client = mock_client
    path = tmp_path / "audit.jsonl"
    server = read_write(client, audit_path=str(path))
    arguments = {"email": "file@example.com"}
    server.call_tool(
        "upsert_contact",
        {**arguments, "approval_token": server.authority.issue("upsert_contact", arguments)},
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "upsert_contact"
    assert record["result_hash"]


# ------------------------------------------------------------------ stdio loop


def test_stdio_loop_handles_multiple_messages_and_a_parse_error(mock_client):
    _state, client = mock_client
    server = read_only(client)
    source = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                "{not json",
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
        + "\n"
    )
    sink = io.StringIO()
    server.serve(source, sink)

    responses = [json.loads(line) for line in sink.getvalue().strip().splitlines()]
    assert responses[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert responses[1]["error"]["code"] == -32700
    assert len(responses[2]["result"]["tools"]) == 5


def test_rpc_error_helper_shape():
    assert _rpc_error(7, -32600, "bad") == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32600, "message": "bad"},
    }

# ------------------------------------------------------------------ cli


def test_cli_refuses_to_start_without_a_token(monkeypatch, capsys):
    """Starting without a token can only produce 401s, which agents narrate as empty data."""

    monkeypatch.delenv("GHL_PIT_TOKEN", raising=False)
    from ghl_mcp.server import main

    assert main([]) == 2
    assert "GHL_PIT_TOKEN is not set" in capsys.readouterr().err


def test_approve_cli_requires_a_secret(monkeypatch, capsys):
    monkeypatch.delenv("GHL_MCP_APPROVAL_SECRET", raising=False)
    from ghl_mcp.server import main

    assert main(["approve", "--tool", "upsert_contact", "--args", "{}"]) == 2
    assert "GHL_MCP_APPROVAL_SECRET" in capsys.readouterr().err


def test_approve_cli_issues_a_usable_token(monkeypatch, capsys):
    from ghl_mcp.governance import ApprovalAuthority
    from ghl_mcp.server import main

    monkeypatch.setenv("GHL_MCP_APPROVAL_SECRET", "cli-secret")
    assert main(["approve", "--tool", "upsert_contact", "--args", '{"email":"a@b.com"}']) == 0
    token = capsys.readouterr().out.strip()
    authority = ApprovalAuthority("cli-secret")
    grant = authority.verify(token, "upsert_contact", {"email": "a@b.com"})
    assert grant.tool == "upsert_contact"
    assert authority.consumed == 1
