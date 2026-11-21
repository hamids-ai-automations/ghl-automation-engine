from __future__ import annotations

import threading
import time

import pytest

from ghl_client.auth import (
    FileTokenStore,
    InMemoryTokenStore,
    OAuthCredential,
    PrivateIntegrationToken,
)
from ghl_client.errors import GhlAuthError
from ghl_client.transport import MockTransport, json_response


def _oauth(clock, transport, store=None, *, expires_in: float = 5.0, window: float = 300.0):
    return OAuthCredential(
        access_token="stale",
        refresh_token="rt-1",
        expires_at=clock() + expires_in,
        client_id="cid",
        client_secret="csecret",
        token_url="https://auth.example/token",
        transport=transport,
        store=store,
        refresh_window=window,
        clock=clock,
    )


def test_expired_token_triggers_one_refresh(clock):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return json_response(
            200, {"access_token": f"new-{calls['n']}", "expires_in": 3600}
        )

    cred = _oauth(clock, MockTransport(handler))
    assert cred.is_expired() is True

    assert cred.access() == "new-1"
    assert cred.access() == "new-1"  # cached
    assert calls["n"] == 1
    assert cred.refresh_count == 1


def test_concurrent_callers_refresh_only_once(clock):
    """12 threads hit an expired token; the provider must see exactly one call.

    This is the bug that burns refresh-token rotation budgets in production.
    """

    calls = {"n": 0}
    gate = threading.Lock()

    def handler(request):
        with gate:
            calls["n"] += 1
        time.sleep(0.02)  # widen the race window
        return json_response(200, {"access_token": "shared", "expires_in": 3600})

    cred = _oauth(clock, MockTransport(handler))

    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(cred.access())
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert calls["n"] == 1
    assert cred.refresh_count == 1
    assert results == ["shared"] * 12


def test_refreshed_token_is_persisted_and_reused(clock, tmp_path):
    store = FileTokenStore(tmp_path / "token.json")

    def handler(request):
        return json_response(
            200,
            {
                "access_token": "persisted",
                "refresh_token": "rt-2",
                "expires_in": 3600,
            },
        )

    cred = _oauth(clock, MockTransport(handler), store=store)
    cred.access()

    saved = store.load()
    assert saved["access_token"] == "persisted"
    assert saved["refresh_token"] == "rt-2"

    # A new process starting up must not refresh again.
    def exploding(request):  # pragma: no cover - must never run
        raise AssertionError("refresh should not be needed")

    revived = OAuthCredential(
        access_token="",
        refresh_token="",
        expires_at=0,
        client_id="cid",
        client_secret="csecret",
        token_url="https://auth.example/token",
        transport=MockTransport(exploding),
        store=store,
        refresh_window=300,
        clock=clock,
    )
    assert revived.access_token == "persisted"


def test_keeps_old_refresh_token_when_provider_does_not_rotate(clock):
    cred = _oauth(clock, MockTransport(lambda r: json_response(
        200, {"access_token": "a2", "expires_in": 3600}
    )))
    cred.access()
    assert cred.state()["refresh_token"] == "rt-1"


def test_refresh_failure_raises_auth_error(clock):
    cred = _oauth(clock, MockTransport(lambda r: json_response(
        400, {"error": "invalid_grant", "error_description": "revoked"}
    )), expires_in=-1.0, window=0.0)
    with pytest.raises(GhlAuthError) as excinfo:
        cred.access()
    assert excinfo.value.status == 400
    assert "invalid_grant" in (excinfo.value.body or "")


def test_token_endpoint_without_access_token_is_an_error(clock):
    cred = _oauth(clock, MockTransport(lambda r: json_response(200, {"ok": True})))
    with pytest.raises(GhlAuthError):
        cred.access()


def test_private_token_rotation_warning_at_ninety_days(clock):
    issued = clock() - 91 * 86400
    cred = PrivateIntegrationToken(token="pit", issued_at=issued)
    assert cred.needs_rotation(clock=clock) is True
    assert cred.headers() == {"Authorization": "Bearer pit"}

    fresh = PrivateIntegrationToken(token="pit", issued_at=clock() - 10 * 86400)
    assert fresh.needs_rotation(clock=clock) is False
    assert PrivateIntegrationToken(token="pit").needs_rotation(clock=clock) is False
