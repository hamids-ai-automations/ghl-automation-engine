from __future__ import annotations
import pytest

import json

from ghl_client.auth import PrivateIntegrationToken
from ghl_client.client import GhlClient, redact
from ghl_client.errors import (
    GhlAuthError,
    GhlRateLimitError,
    GhlServerError,
    GhlValidationError,
)
from ghl_client.ratelimit import SlidingWindowLimiter
from ghl_client.retry import RetryPolicy
from ghl_client.resources.contacts import ContactsResource
from ghl_client.transport import MockTransport, json_response


def make_client(handler, *, retries: int = 1, location_id: str = "loc_1"):
    transport = MockTransport(handler)
    client = GhlClient(
        PrivateIntegrationToken(token="pit"),
        base_url="https://api.test",
        transport=transport,
        limiter=SlidingWindowLimiter(capacity=10_000, window=1.0),
        retry_policy=RetryPolicy(max_attempts=retries, base_delay=0.0, jitter=False),
        location_id=location_id,
    )
    return client, transport


# -- headers & versioning ---------------------------------------------------


def test_requests_carry_auth_version_and_location():
    client, transport = make_client(lambda r: json_response(200, {}))
    client.get("/contacts/abc")

    headers = transport.requests[0]["headers"]
    assert headers["authorization"] == "Bearer pit"
    assert headers["version"] == "2021-07-28"
    assert headers["x-location-id"] == "loc_1"


def test_query_params_are_encoded_and_none_dropped():
    client, transport = make_client(lambda r: json_response(200, {}))
    client.get("/contacts", params={"a": "x y", "b": None})

    assert transport.requests[0]["url"] == "https://api.test/contacts?a=x+y"


# -- error mapping ----------------------------------------------------------


def test_status_codes_map_to_specific_error_types():
    cases = [
        (401, GhlAuthError),
        (422, GhlValidationError),
        (503, GhlServerError),
    ]
    for status, expected in cases:
        client, _ = make_client(lambda r, s=status: json_response(s, {"error": "x"}))
        try:
            client.get("/contacts/abc")
        except expected as exc:
            assert exc.status == status
            assert exc.method == "GET"
        else:  # pragma: no cover
            raise AssertionError(f"{status} did not raise {expected.__name__}")


def test_429_carries_retry_after_header():
    client, _ = make_client(
        lambda r: json_response(429, {"error": "slow"}, headers={"Retry-After": "3"})
    )
    try:
        client.get("/contacts/abc")
    except GhlRateLimitError as exc:
        assert exc.retry_after == 3.0
        assert exc.retryable is True
    else:  # pragma: no cover
        raise AssertionError("expected GhlRateLimitError")


def test_retries_then_succeeds_against_scripted_transport():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return json_response(502, {"error": "bad gateway"})
        return json_response(200, {"ok": True})

    client, _ = make_client(handler, retries=4)
    assert client.get("/contacts/abc") == {"ok": True}
    assert calls["n"] == 3


# -- PII containment --------------------------------------------------------


def test_redact_masks_credentials_and_contact_pii():
    payload = {
        "email": "jane.doe@example.com",
        "phone": "+1 (555) 867-5309",
        "Authorization": "Bearer secret-token",
        "nested": {"refresh_token": "rt", "note": "call me on 0300-1234567"},
    }
    cleaned = redact(payload)

    dumped = json.dumps(cleaned)
    assert "jane.doe@example.com" not in dumped
    assert "867-5309" not in dumped
    assert "secret-token" not in dumped
    assert "0300-1234567" not in dumped
    assert cleaned["Authorization"] == "***"
    assert cleaned["nested"]["refresh_token"] == "***"


def test_debug_logging_never_contains_raw_email(caplog):
    import logging

    client, _ = make_client(lambda r: json_response(200, {"contact": {"id": "c1"}}))
    with caplog.at_level(logging.DEBUG, logger="ghl_client"):
        client.post("/contacts/upsert", json_body={"email": "jane.doe@example.com"})

    assert "jane.doe@example.com" not in caplog.text
    assert "***@***" in caplog.text


# -- idempotent upsert ------------------------------------------------------


def test_repeated_upsert_hits_the_api_once():
    payload_calls = {"n": 0}

    def handler(request):
        payload_calls["n"] += 1
        return json_response(200, {"contact": {"id": "c-1"}, "new": True})

    client, transport = make_client(handler)
    contacts = ContactsResource(client)

    first = contacts.upsert({"email": "A@Example.com", "firstName": "Ann"})
    second = contacts.upsert({"email": "a@example.com", "firstName": "Ann"})

    assert first == second
    assert payload_calls["n"] == 1
    assert len(transport.requests) == 1
    assert contacts.ledger.hits == 1


def test_upsert_injects_location_and_new_contact_makes_a_second_call():
    client, transport = make_client(
        lambda r: json_response(200, {"contact": {"id": "c"}, "new": True})
    )
    contacts = ContactsResource(client)

    contacts.upsert({"email": "a@example.com"})
    contacts.upsert({"email": "b@example.com"})

    assert len(transport.requests) == 2
    first_body = json.loads(transport.requests[0]["body"])
    assert first_body["locationId"] == "loc_1"


def test_explicit_idempotency_key_beats_fingerprint():
    client, transport = make_client(
        lambda r: json_response(200, {"contact": {"id": "c"}})
    )
    contacts = ContactsResource(client)

    contacts.upsert({"email": "a@example.com"}, idempotency_key="run-1")
    contacts.upsert({"email": "a@example.com"}, idempotency_key="run-2")

    assert len(transport.requests) == 2

# ----------------------------------------------------- dropped connections (Windows)


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError(10054, "reset"), ConnectionAbortedError(10053, "aborted"),
     BrokenPipeError(10053, "pipe")],
    ids=["reset", "aborted", "broken-pipe"],
)
def test_a_dropped_connection_becomes_a_retryable_transport_error(error):
    """Windows reports WSAECONNABORTED where POSIX reports ECONNRESET; urllib wraps neither."""
    import io

    from ghl_client.errors import GhlTransportError
    from ghl_client.transport import UrllibTransport

    class Opener:
        def open(self, request, timeout=None):
            raise error

    transport = UrllibTransport(opener=Opener())

    with pytest.raises(GhlTransportError) as caught:
        transport.send("GET", "https://example.invalid/contacts", headers={}, body=None, timeout=5)

    assert caught.value.retryable is True


def test_a_truncated_response_is_a_transport_error():
    import http.client

    from ghl_client.errors import GhlTransportError
    from ghl_client.transport import UrllibTransport

    class Opener:
        def open(self, request, timeout=None):
            raise http.client.BadStatusLine("")

    transport = UrllibTransport(opener=Opener())

    with pytest.raises(GhlTransportError):
        transport.send("GET", "https://example.invalid/contacts", headers={}, body=None, timeout=5)
