"""Webhook intake tests: signature framing, replay, parsing, and the reject paths."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from ghl_webhooks import (
    InMemoryReplayStore,
    IntakeStatus,
    Receiver,
    ReplayGuard,
    SignatureConfig,
    SignatureVerifier,
    WebhookPayloadError,
    WebhookReplayError,
    WebhookSignatureError,
    WebhookTimestampError,
)
from ghl_webhooks.events import EventType, parse_event
from ghl_webhooks.receiver import adapter_response, body_digest

SECRET = "whsec_test_do_not_use"
NOW = 1_800_000_000.0


def sign(body: bytes, secret: str = SECRET, timestamp: float = NOW, prefix: str = "") -> str:
    payload = f"{int(timestamp)}.{body.decode()}".encode()
    return prefix + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def headers(body: bytes, secret: str = SECRET, timestamp: float = NOW) -> dict[str, str]:
    return {
        "X-GHL-Signature": sign(body, secret, timestamp),
        "X-GHL-Timestamp": str(int(timestamp)),
        "Content-Type": "application/json",
    }


def contact_event(event_id: str = "evt_1", **overrides) -> bytes:
    payload = {
        "id": event_id,
        "type": "ContactCreate",
        "locationId": "loc_9",
        "timestamp": str(int(NOW)),
        "data": {"contactId": "c_1", "email": "a@example.com"},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def make_receiver(**kwargs) -> Receiver:
    clock = kwargs.pop("clock", lambda: NOW)
    return Receiver(
        [SECRET],
        replay=ReplayGuard(clock=clock, tolerance_seconds=300),
        **kwargs,
    )


# --- signature verification -------------------------------------------------


def test_valid_signature_is_accepted_and_config_reported():
    body = contact_event()
    intake = make_receiver().handle(headers(body), body)
    assert intake.status is IntakeStatus.ACCEPTED
    assert intake.matched_config == "default"
    assert intake.event.type == "ContactCreate"


def test_wrong_secret_is_rejected():
    body = contact_event()
    intake = make_receiver().handle(headers(body, secret="wrong"), body)
    assert intake.status is IntakeStatus.REJECTED
    assert intake.error is not None and isinstance(intake.error, WebhookSignatureError)
    assert intake.http_status == 401


def test_tampered_body_is_rejected():
    body = contact_event()
    signed = headers(body)
    intake = make_receiver().handle(signed, body.replace(b"c_1", b"c_2"))
    assert intake.status is IntakeStatus.REJECTED


def test_unsigned_webhook_is_rejected_by_default():
    body = contact_event()
    intake = make_receiver().handle({"Content-Type": "application/json"}, body)
    assert intake.status is IntakeStatus.REJECTED
    assert "unsigned" in intake.reason


def test_missing_signature_header_when_timestamp_present_is_rejected():
    body = contact_event()
    hdrs = {"X-GHL-Timestamp": str(int(NOW))}
    intake = make_receiver().handle(hdrs, body)
    assert intake.status is IntakeStatus.REJECTED


def test_header_lookup_is_case_insensitive():
    body = contact_event()
    signed = headers(body)
    lowered = {k.lower(): v for k, v in signed.items()}
    assert make_receiver().handle(lowered, body).status is IntakeStatus.ACCEPTED


def test_signature_prefix_is_tolerated():
    body = contact_event()
    hdrs = headers(body)
    hdrs["X-GHL-Signature"] = "sha256=" + hdrs["X-GHL-Signature"]
    assert make_receiver().handle(hdrs, body).status is IntakeStatus.ACCEPTED


def test_malformed_hex_signature_is_rejected_not_crashed():
    body = contact_event()
    hdrs = headers(body)
    hdrs["X-GHL-Signature"] = "zzzz"
    intake = make_receiver().handle(hdrs, body)
    assert intake.status is IntakeStatus.REJECTED
    assert "not valid hex" in intake.reason


def test_secret_rotation_accepts_old_secret_until_removed():
    body = contact_event()
    verifier = SignatureVerifier([SECRET, "whsec_new"])
    hdrs = headers(body, secret="whsec_new")
    assert verifier.verify(body, hdrs).label == "default"


def test_empty_secret_list_fails_closed_at_construction():
    with pytest.raises(ValueError, match="at least one signing secret"):
        SignatureVerifier([])
    with pytest.raises(ValueError, match="at least one signing secret"):
        Receiver([""])


def test_base64_and_alternate_header_config_verifies():
    config = SignatureConfig(
        header="x-wh-signature",
        timestamp_header="x-wh-timestamp",
        encoding="base64",
        label="alt",
    )
    body = contact_event()
    payload = f"{int(NOW)}.{body.decode()}".encode()
    digest = hmac.new(SECRET.encode(), payload, hashlib.sha256).digest()
    hdrs = {
        "X-WH-Signature": base64.b64encode(digest).decode(),
        "X-WH-Timestamp": str(int(NOW)),
    }
    r = Receiver([SECRET], configs=(config,), replay=ReplayGuard(clock=lambda: NOW))
    assert r.handle(hdrs, body).matched_config == "alt"


def test_config_rejects_unauthenticated_timestamp():
    with pytest.raises(ValueError, match="unauthenticated"):
        SignatureConfig(timestamp_header="x-ghl-timestamp", payload_template="{body}")


def test_config_rejects_template_without_body():
    with pytest.raises(ValueError, match="must contain"):
        SignatureConfig(payload_template="{timestamp}")


# --- timestamp window -------------------------------------------------------


def test_stale_timestamp_is_rejected():
    body = contact_event()
    old = NOW - 400
    intake = make_receiver().handle(headers(body, timestamp=old), body)
    assert intake.status is IntakeStatus.REJECTED
    assert isinstance(intake.error, WebhookTimestampError)


def test_future_timestamp_beyond_window_is_rejected():
    body = contact_event()
    intake = make_receiver().handle(headers(body, timestamp=NOW + 400), body)
    assert intake.status is IntakeStatus.REJECTED
    assert isinstance(intake.error, WebhookTimestampError)


def test_non_numeric_timestamp_is_rejected():
    """A garbage timestamp cannot match any signature, so it fails at verification."""
    body = contact_event()
    hdrs = headers(body)
    hdrs["X-GHL-Timestamp"] = "yesterday"
    intake = make_receiver().handle(hdrs, body)
    assert intake.status is IntakeStatus.REJECTED
    assert intake.http_status == 401


def test_timestamp_window_is_enforced_directly():
    guard = ReplayGuard(clock=lambda: NOW, tolerance_seconds=60)
    assert guard.check_timestamp(str(int(NOW - 59))) == int(NOW - 59)
    with pytest.raises(WebhookTimestampError, match="older"):
        guard.check_timestamp(str(int(NOW - 61)))
    with pytest.raises(WebhookTimestampError, match="future"):
        guard.check_timestamp(str(int(NOW + 61)))
    with pytest.raises(WebhookTimestampError, match="not numeric"):
        guard.check_timestamp("yesterday")
    with pytest.raises(WebhookTimestampError, match="no timestamp"):
        guard.check_timestamp(None)


# --- replay -----------------------------------------------------------------


def test_second_delivery_of_same_event_is_duplicate_not_error():
    body = contact_event()
    receiver = make_receiver()
    first = receiver.handle(headers(body), body)
    second = receiver.handle(headers(body), body)
    assert first.should_process is True
    assert second.status is IntakeStatus.DUPLICATE
    assert second.http_status == 200
    assert second.should_process is False


def test_duplicate_is_recognised_across_body_whitespace_changes():
    receiver = make_receiver()
    a = contact_event()
    b = json.dumps(json.loads(a), indent=2).encode()
    assert receiver.handle(headers(a), a).status is IntakeStatus.ACCEPTED
    assert receiver.handle(headers(b), b).status is IntakeStatus.DUPLICATE


def test_different_events_are_not_collapsed():
    receiver = make_receiver()
    one, two = contact_event("evt_1"), contact_event("evt_2")
    assert receiver.handle(headers(one), one).status is IntakeStatus.ACCEPTED
    assert receiver.handle(headers(two), two).status is IntakeStatus.ACCEPTED


def test_same_id_at_a_later_timestamp_is_still_duplicate():
    receiver = make_receiver()
    early = contact_event()
    late = contact_event()
    assert receiver.handle(headers(early, timestamp=NOW - 100), early).status is IntakeStatus.ACCEPTED
    assert receiver.handle(headers(late, timestamp=NOW), late).status is IntakeStatus.DUPLICATE


def test_failed_delivery_does_not_burn_the_event_id():
    """A rejected attempt must not stop the genuine retry from being accepted."""
    receiver = make_receiver()
    body = contact_event()
    assert receiver.handle(headers(body, secret="wrong"), body).status is IntakeStatus.REJECTED
    assert receiver.handle(headers(body), body).status is IntakeStatus.ACCEPTED


def test_replay_guard_raises_on_second_claim():
    guard = ReplayGuard(clock=lambda: NOW)
    guard.claim("k")
    with pytest.raises(WebhookReplayError):
        guard.claim("k")


def test_replay_guard_rejects_empty_key():
    with pytest.raises(ValueError, match="non-empty"):
        ReplayGuard(clock=lambda: NOW).claim("")


def test_in_memory_store_expires_entries():
    now = [NOW]
    store = InMemoryReplayStore(clock=lambda: now[0])
    assert store.remember("k", ttl_seconds=10) is True
    now[0] += 11
    assert store.remember("k", ttl_seconds=10) is True
    assert len(store) == 1


def test_custom_store_is_used():
    class AlwaysSeen:
        def __init__(self):
            self.calls = []

        def remember(self, key, ttl_seconds):
            self.calls.append((key, ttl_seconds))
            return False

    store = AlwaysSeen()
    receiver = Receiver([SECRET], replay=ReplayGuard(store, clock=lambda: NOW))
    body = contact_event()
    intake = receiver.handle(headers(body), body)
    assert intake.status is IntakeStatus.DUPLICATE
    assert store.calls and store.calls[0][0] == "id:evt_1"


# --- parsing ----------------------------------------------------------------


def test_unknown_event_type_is_preserved_and_flagged():
    body = contact_event(type="SomethingBrandNew")
    intake = make_receiver().handle(headers(body), body)
    assert intake.status is IntakeStatus.ACCEPTED
    assert intake.event is not None
    assert intake.event.is_known is False
    assert "unrecognised" in intake.reason
    assert intake.event.raw["type"] == "SomethingBrandNew"


def test_known_types_resolve_to_enum_members():
    body = contact_event(type="OpportunityStageUpdate")
    event = parse_event(body)
    assert event.is_known is True
    assert EventType(event.type) is EventType.OPPORTUNITY_STAGE_UPDATE


def test_field_sources_record_which_spelling_arrived():
    body = json.dumps(
        {"event_id": "e1", "eventType": "ContactUpdate", "location_id": "l1"}
    ).encode()
    event = parse_event(body)
    assert event.id == "e1"
    assert event.location_id == "l1"
    assert event.field_sources["type"] == "eventType"
    assert event.field_sources["id"] == "event_id"


def test_non_json_body_is_rejected():
    body = b"not json"
    intake = make_receiver().handle(headers(body), body)
    assert intake.status is IntakeStatus.REJECTED
    assert isinstance(intake.error, WebhookPayloadError)
    assert intake.http_status == 400


def test_json_array_body_is_rejected():
    body = b"[1,2,3]"
    intake = make_receiver().handle(headers(body), body)
    assert intake.status is IntakeStatus.REJECTED


def test_event_without_type_is_rejected():
    body = json.dumps({"id": "x", "data": {}}).encode()
    with pytest.raises(WebhookPayloadError, match="no event type"):
        parse_event(body)


def test_oversized_body_is_rejected_before_parsing():
    body = contact_event() + b" " * 5000
    intake = make_receiver(max_body_bytes=100).handle(headers(body), body)
    assert intake.status is IntakeStatus.REJECTED
    assert "size limit" in intake.reason


def test_nested_location_object_is_unwrapped():
    body = json.dumps(
        {"type": "TaskCreate", "id": "t1", "locationId": {"_id": "loc_nested"}}
    ).encode()
    assert parse_event(body).location_id == "loc_nested"


def test_payload_fields_are_not_lost_without_data_key():
    body = json.dumps({"type": "TaskCreate", "id": "t1", "custom": 42}).encode()
    event = parse_event(body)
    assert event.data["custom"] == 42


# --- adapter surface --------------------------------------------------------


def test_receiver_can_run_unsigned_only_when_explicitly_asked():
    body = contact_event()
    receiver = make_receiver(require_signature=False)
    intake = receiver.handle({}, body)
    assert intake.status is IntakeStatus.ACCEPTED
    assert intake.matched_config is None


def test_adapter_response_shape_for_accept_duplicate_and_reject():
    receiver = make_receiver()
    body = contact_event()
    first = adapter_response(receiver.handle(headers(body), body))
    second = adapter_response(receiver.handle(headers(body), body))
    rejected = adapter_response(
        make_receiver().handle(headers(body, secret="no"), body)
    )
    assert first == {"status": "accepted", "process": True, "type": "ContactCreate", "id": "evt_1"}
    assert second["status"] == "duplicate" and second["process"] is False
    assert rejected["status"] == "rejected" and "reason" in rejected


def test_body_digest_is_stable():
    body = contact_event()
    assert body_digest(body) == hashlib.sha256(body).hexdigest()
    assert body_digest(body) != body_digest(body + b" ")
