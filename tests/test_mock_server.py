"""End-to-end tests against the real mock server over real HTTP.

These prove the client works against a live socket, not just a stub, and that the
mock can reproduce the failures the client claims to survive.
"""

from __future__ import annotations

import pytest

from ghl_client.auth import PrivateIntegrationToken
from ghl_client.client import GhlClient
from ghl_client.errors import GhlAuthError, GhlPartialResultError
from ghl_client.ratelimit import SlidingWindowLimiter
from ghl_client.resources.contacts import ContactsResource
from ghl_client.retry import RetryPolicy
from ghl_client.transport import UrllibTransport
from ghl_mock import DEFAULT_TOKEN, Fault


def client_for(base_url: str, *, token: str = DEFAULT_TOKEN, retries: int = 3) -> GhlClient:
    return GhlClient(
        PrivateIntegrationToken(token=token),
        base_url=base_url,
        transport=UrllibTransport(),
        limiter=SlidingWindowLimiter(capacity=10_000, window=1.0),
        retry_policy=RetryPolicy(max_attempts=retries, base_delay=0.0, jitter=False),
        location_id="loc_test",
    )


def seed(contacts: ContactsResource, count: int) -> None:
    for i in range(count):
        contacts.upsert({"email": f"user{i}@example.com", "firstName": f"U{i}"})


def test_health_endpoint_is_reachable(mock_server):
    _state, base_url = mock_server
    client = client_for(base_url)
    assert client.get("/health")["status"] == "ok"


def test_full_contact_list_is_returned_across_pages(mock_server):
    state, base_url = mock_server
    contacts = ContactsResource(client_for(base_url), page_size=10)
    seed(contacts, 25)

    result = contacts.search()

    assert result.complete is True
    assert len(result.items) == 25
    assert result.pages_fetched == 3
    assert state.store.created == 25


def test_search_filter_narrows_results(mock_server):
    _state, base_url = mock_server
    contacts = ContactsResource(client_for(base_url), page_size=10)
    seed(contacts, 6)

    result = contacts.search(query="user3@")

    assert len(result.items) == 1
    assert result.items[0]["email"] == "user3@example.com"


def test_missing_token_is_rejected(mock_server):
    _state, base_url = mock_server
    client = client_for(base_url, token="wrong-token")
    with pytest.raises(GhlAuthError):
        client.get("/contacts/does-not-exist")


def test_rate_limit_is_injected_and_survived(mock_server):
    state, base_url = mock_server
    client = client_for(base_url, retries=3)
    state.queue_fault("/contacts/search", Fault(
        status=429, times=1, retry_after="0"
    ))

    result = ContactsResource(client).search()

    assert result.complete is True
    statuses = [r["status"] for r in state.requests if r["path"] == "/contacts/search"]
    assert statuses == [429, 200]


def test_persistent_failure_after_first_page_raises_partial_result(mock_server):
    state, base_url = mock_server
    contacts = ContactsResource(client_for(base_url, retries=1), page_size=10)
    seed(contacts, 25)

    state.queue_fault(
        "/contacts/search", Fault(status=500, times=5, skip=1)
    )

    with pytest.raises(GhlPartialResultError) as excinfo:
        contacts.search()

    assert len(excinfo.value.collected) == 10  # page one only, explicitly reported


def test_upsert_twice_leaves_one_contact(mock_server):
    """Provider-side dedupe on email, not the client ledger: distinct keys force
    both calls through, and the store must still hold a single contact."""

    state, base_url = mock_server
    contacts = ContactsResource(client_for(base_url))

    contacts.upsert({"email": "dup@example.com", "firstName": "Dup"}, idempotency_key="a")
    contacts.upsert({"email": "DUP@example.com", "firstName": "Dup"}, idempotency_key="b")

    assert state.store.created == 1
    assert state.store.updated == 1


def test_replayed_upsert_after_network_view_does_not_duplicate(mock_server):
    """Simulates a retry: same ledger key, provider must still hold one contact."""

    state, base_url = mock_server
    contacts = ContactsResource(client_for(base_url))

    contacts.upsert({"email": "once@example.com"})
    requests_before = len(state.requests)
    contacts.upsert({"email": "once@example.com"})

    assert len(state.requests) == requests_before  # served from the ledger
    assert state.store.created == 1
