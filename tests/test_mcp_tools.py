"""The five tools, against the real mock over a real socket.

The tests that matter here are the ones about *honesty*: a limited read says it is
limited, a dry run changes nothing, an upsert without an identity is refused instead of
creating a duplicate, and the payload a model receives has been narrowed to the allowlist.
"""

from __future__ import annotations

import pytest

from ghl_client import GhlPartialResultError
from ghl_client.resources.contacts import ContactsResource
from ghl_mcp import create_server
from ghl_mcp.governance import Mode
from ghl_mcp.projection import DEFAULT_CONTACT_ALLOWLIST, project, project_many
from ghl_mcp.tools import build_registry


def seed(client, count: int = 3) -> None:
    contacts = ContactsResource(client)
    for index in range(count):
        contacts.upsert({"email": f"user{index}@example.com", "firstName": f"U{index}"})


def server_for(client, *, mode: Mode = Mode.READ_WRITE, **kwargs):
    return create_server(client, mode=mode, approval_secret="test-secret", **kwargs)


def paths(state) -> list[str]:
    return [entry["path"] for entry in state.requests]


# --------------------------------------------------------------------- projection


def test_projection_drops_fields_outside_the_allowlist():
    contact = {
        "id": "c1",
        "email": "a@b.com",
        "customFields": [{"id": "x", "value": "private"}],
        "attributions": [{"source": "ad"}],
    }
    assert project(contact, DEFAULT_CONTACT_ALLOWLIST) == {"id": "c1", "email": "a@b.com"}


def test_projection_reports_what_it_dropped():
    contact = {"id": "c1", "customFields": [1], "attributions": [2]}
    rows, dropped = project_many([contact, {"id": "c2"}], DEFAULT_CONTACT_ALLOWLIST)
    assert rows == [{"id": "c1"}, {"id": "c2"}]
    assert dropped == ["attributions", "customFields"]


# ------------------------------------------------------------------- reads


def test_search_returns_projected_contacts_and_reports_completeness(mock_client):
    state, client = mock_client
    seed(client)
    result = server_for(client).call_tool("search_contacts", {"query": "user"})
    payload = result["structuredContent"]

    assert result["isError"] is False
    assert payload["returned"] == 3
    assert payload["complete"] is True
    assert set(payload["contacts"][0]) <= set(DEFAULT_CONTACT_ALLOWLIST)
    assert "/contacts/search" in paths(state)


def test_limited_search_reports_incomplete(mock_client):
    _state, client = mock_client
    seed(client, 5)
    payload = server_for(client).call_tool("search_contacts", {"limit": 2})["structuredContent"]
    assert payload["returned"] == 2
    assert payload["complete"] is False


def test_search_reports_withheld_fields_when_the_record_has_them(mock_client):
    _state, client = mock_client

    class FatResource(ContactsResource):
        def search(self, **kwargs):
            from ghl_client.pagination import Collection

            return Collection(
                items=[
                    {
                        "id": "c1",
                        "email": "a@b.com",
                        "customFields": [{"value": "secret"}],
                        "lastAttributionSource": {"utm": "x"},
                    }
                ]
            )

    registry = build_registry(client, contacts=FatResource(client))
    payload = registry.get("search_contacts").handler({"query": "a"})
    assert payload["contacts"] == [{"id": "c1", "email": "a@b.com"}]
    assert "customFields" in payload["redaction"]


def test_partial_search_is_an_error_with_the_rows_attached(mock_client):
    _state, client = mock_client

    class FailingResource(ContactsResource):
        def search(self, **kwargs):
            from ghl_client.errors import GhlTransportError

            raise GhlPartialResultError(
                "pagination stopped after 1 page(s)", collected=[{"id": "c1"}], errors=[GhlTransportError("boom")]
            )

    registry = build_registry(client, contacts=FailingResource(client))
    spec = registry.get("search_contacts")
    with pytest.raises(Exception) as caught:
        spec.handler({"query": "a"})
    error = caught.value
    assert error.reason == "partial_result"
    assert error.details["collected"] == [{"id": "c1"}]


def test_get_contact_projects_and_reports_redaction(mock_client):
    _state, client = mock_client
    contact = ContactsResource(client).upsert({"email": "a@b.com", "firstName": "Ada"})["contact"]
    payload = server_for(client).call_tool("get_contact", {"contact_id": contact["id"]})[
        "structuredContent"
    ]
    assert payload["contact"]["email"] == "a@b.com"
    assert payload["contact"]["firstName"] == "Ada"
    assert set(payload["contact"]) <= set(DEFAULT_CONTACT_ALLOWLIST)


def test_get_contact_requires_an_id(mock_client):
    _state, client = mock_client
    result = server_for(client).call_tool("get_contact", {})
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["reason"] == "invalid_input"


# ------------------------------------------------------------------- writes


def test_dry_run_reports_create_without_writing(mock_client):
    state, client = mock_client
    before = len(paths(state))
    payload = server_for(client).call_tool(
        "upsert_contact", {"email": "new@example.com", "first_name": "Ada", "dry_run": True}
    )["structuredContent"]

    assert payload["action"] == "create"
    assert payload["mutated"] is False
    assert payload["would_apply"]["email"] == {"from": None, "to": "new@example.com"}
    assert "/contacts/upsert" not in paths(state)[before:]


def test_dry_run_reports_the_update_diff(mock_client):
    _state, client = mock_client
    seed(client, 1)
    payload = server_for(client).call_tool(
        "upsert_contact",
        {"email": "user0@example.com", "first_name": "Changed", "dry_run": True},
    )["structuredContent"]

    assert payload["action"] == "update"
    assert payload["would_apply"]["firstName"] == {"from": "U0", "to": "Changed"}
    assert payload["existing"]["email"] == "user0@example.com"


def test_dry_run_with_identical_values_reports_no_op(mock_client):
    _state, client = mock_client
    seed(client, 1)
    payload = server_for(client).call_tool(
        "upsert_contact", {"email": "user0@example.com", "first_name": "U0", "dry_run": True}
    )["structuredContent"]
    assert payload["action"] == "no-op"
    assert payload["would_apply"] == {}


def test_dry_run_does_not_consume_the_write_budget(mock_client):
    _state, client = mock_client
    server = server_for(client, max_writes=1)
    server.call_tool("upsert_contact", {"email": "a@b.com", "dry_run": True})
    assert server.budget.writes == 0


def test_upsert_without_identity_is_refused(mock_client):
    _state, client = mock_client
    result = server_for(client).call_tool(
        "upsert_contact", {"first_name": "NoIdentity", "approval_token": "x"}
    )
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["reason"] in {"approval_invalid", "upstream_failure"}


def test_upsert_is_idempotent_on_replay(mock_client):
    _state, client = mock_client
    resource = ContactsResource(client)
    first = resource.upsert({"email": "dup@example.com", "firstName": "Once"})["contact"]
    second = resource.upsert({"email": "dup@example.com", "firstName": "Once"})["contact"]
    assert first["id"] == second["id"]


def test_real_upsert_writes_and_reports_mutation(mock_client):
    _state, client = mock_client
    server = server_for(client)
    arguments = {"email": "real@example.com", "first_name": "Real"}
    token = server.authority.issue("upsert_contact", arguments)
    result = server.call_tool("upsert_contact", {**arguments, "approval_token": token})

    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["mutated"] is True
    assert payload["contact"]["email"] == "real@example.com"
    assert payload["created"] is True


# ------------------------------------------------------------------- self-report


def test_rate_limit_status_reports_local_budget(mock_client):
    _state, client = mock_client
    payload = server_for(client).call_tool("rate_limit_status", {})["structuredContent"]
    assert payload["remaining"] == 10_000
    assert payload["used"] == 0


def test_server_governance_publishes_its_own_gaps(mock_client):
    _state, client = mock_client
    payload = server_for(client).call_tool("server_governance", {})["structuredContent"]
    assert "upsert_contact" in payload["tools_implemented"]
    blocked = {entry["name"] for entry in payload["tools_blocked"]}
    assert "update_contact" in blocked
    assert payload["guarantees"]["write_without_approval_fails_closed"] is True
