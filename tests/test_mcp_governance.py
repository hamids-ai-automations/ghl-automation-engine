"""Governance controls: the default, the approval, the budget, the account binding.

Each test here corresponds to one way this server could quietly do the wrong thing, so
the assertions are written as "must refuse", not "should behave".
"""

from __future__ import annotations

import pytest

from ghl_mcp.errors import (
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalReused,
    BudgetExhausted,
    CrossLocation,
)
from ghl_mcp.governance import (
    READ,
    WRITE,
    ApprovalAuthority,
    LocationGuard,
    Mode,
    SessionBudget,
    args_digest,
)


# --------------------------------------------------------------------------- mode


def test_mode_defaults_to_read_only():
    assert Mode.from_env({}) is Mode.READ_ONLY
    assert Mode.from_env({"GHL_MCP_MODE": ""}) is Mode.READ_ONLY


def test_mode_reads_explicit_value():
    assert Mode.from_env({"GHL_MCP_MODE": "read-write"}) is Mode.READ_WRITE
    assert Mode.from_env({"GHL_MCP_MODE": "  READ-WRITE "}) is Mode.READ_WRITE


def test_mode_rejects_nonsense_instead_of_falling_back():
    with pytest.raises(ValueError, match="is not a mode"):
        Mode.from_env({"GHL_MCP_MODE": "readwite"})


def test_only_read_write_allows_writes():
    assert Mode.READ_WRITE.allows_writes is True
    assert Mode.READ_ONLY.allows_writes is False


# ------------------------------------------------------------------------ digest


def test_args_digest_is_key_order_independent():
    assert args_digest({"a": 1, "b": 2}) == args_digest({"b": 2, "a": 1})


def test_args_digest_ignores_the_token_field():
    assert args_digest({"a": 1}) == args_digest({"a": 1, "approval_token": "x"})


def test_args_digest_changes_when_arguments_change():
    assert args_digest({"email": "a@b.com"}) != args_digest({"email": "a@b.co"})


# ---------------------------------------------------------------------- approval


def test_issue_and_verify_roundtrip():
    authority = ApprovalAuthority("secret", clock=lambda: 100.0, ttl_seconds=60)
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    grant = authority.verify(token, "upsert_contact", {"email": "a@b.com"})
    assert grant.tool == "upsert_contact"
    assert grant.args_hash == args_digest({"email": "a@b.com"})
    assert grant.expires_at == 160.0


def test_empty_secret_is_refused():
    with pytest.raises(ValueError, match="must not be empty"):
        ApprovalAuthority("")


def test_token_for_another_tool_is_invalid():
    authority = ApprovalAuthority("secret")
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    with pytest.raises(ApprovalInvalid, match="issued for tool"):
        authority.verify(token, "delete_contact", {"email": "a@b.com"})


def test_token_for_other_arguments_is_invalid():
    authority = ApprovalAuthority("secret")
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    with pytest.raises(ApprovalInvalid, match="different arguments"):
        authority.verify(token, "upsert_contact", {"email": "c@d.com"})


def test_token_expires():
    now = [100.0]
    authority = ApprovalAuthority("secret", clock=lambda: now[0], ttl_seconds=30)
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    now[0] = 131.0
    with pytest.raises(ApprovalExpired):
        authority.verify(token, "upsert_contact", {"email": "a@b.com"})


def test_token_is_single_use():
    authority = ApprovalAuthority("secret")
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    authority.verify(token, "upsert_contact", {"email": "a@b.com"})
    with pytest.raises(ApprovalReused):
        authority.verify(token, "upsert_contact", {"email": "a@b.com"})


def test_tampered_token_is_invalid():
    authority = ApprovalAuthority("secret")
    token = authority.issue("upsert_contact", {"email": "a@b.com"})
    head, body, signature = token.split(".")
    flipped = signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
    with pytest.raises(ApprovalInvalid, match="signature"):
        authority.verify(f"{head}.{body}.{flipped}", "upsert_contact", {"email": "a@b.com"})


def test_token_from_a_different_secret_is_invalid():
    mine = ApprovalAuthority("secret")
    theirs = ApprovalAuthority("other-secret")
    token = theirs.issue("upsert_contact", {"email": "a@b.com"})
    with pytest.raises(ApprovalInvalid):
        mine.verify(token, "upsert_contact", {"email": "a@b.com"})


def test_garbage_token_is_invalid_not_a_crash():
    authority = ApprovalAuthority("secret")
    with pytest.raises(ApprovalInvalid):
        authority.verify("not-a-token", "upsert_contact", {})


# ------------------------------------------------------------------------ budget


def test_budget_stops_at_the_call_ceiling():
    budget = SessionBudget(max_calls=2, max_writes=5)
    budget.consume(READ)
    budget.consume(READ)
    with pytest.raises(BudgetExhausted, match="call budget"):
        budget.consume(READ)


def test_budget_stops_writes_separately():
    budget = SessionBudget(max_calls=50, max_writes=1)
    budget.consume(WRITE)
    with pytest.raises(BudgetExhausted, match="write budget"):
        budget.consume(WRITE)
    budget.consume(READ)  # reads keep working


def test_budget_snapshot_shape():
    budget = SessionBudget(max_calls=3, max_writes=1)
    budget.consume(WRITE)
    snapshot = budget.snapshot()
    assert snapshot == {
        "calls": 1,
        "calls_remaining": 2,
        "writes": 1,
        "writes_remaining": 0,
    }


def test_budget_rejects_negative_ceiling():
    with pytest.raises(ValueError):
        SessionBudget(max_calls=0)


# ---------------------------------------------------------------- location guard


def test_location_guard_allows_the_bound_account():
    LocationGuard("loc_1").check({"locationId": "loc_1"})


def test_location_guard_refuses_another_account():
    with pytest.raises(CrossLocation, match="bound to"):
        LocationGuard("loc_1").check({"locationId": "loc_2"})


def test_location_guard_refuses_when_unbound():
    with pytest.raises(CrossLocation, match="not bound"):
        LocationGuard(None).check({"locationId": "loc_1"})


def test_location_guard_walks_nested_arguments():
    with pytest.raises(CrossLocation):
        LocationGuard("loc_1").check({"filters": [{"location_id": "loc_9"}]})


def test_location_guard_ignores_unrelated_keys():
    LocationGuard("loc_1").check({"note": "location", "tags": ["location"]})
