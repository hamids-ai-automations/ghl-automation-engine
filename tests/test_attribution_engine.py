import pytest

from ghl_attribution import (
    AttributionEngine,
    AttributionMappingError,
    IngestStatus,
    InvalidAttributionEvent,
)
from ghl_mock.app import Fault


def _payload(**overrides):
    payload = {
        "event_id": "evt-1",
        "email": "lead@example.com",
        "first_name": "Ada",
        "utm_source": "google",
        "utm_medium": "cpc",
        "utm_campaign": "spring",
        "occurred_at": "2026-11-20T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def _fields_by_key(contact):
    return {item["id"]: item["value"] for item in contact["customFields"]}


def test_engine_refuses_to_start_when_schema_drifted(mock_client):
    state, client = mock_client
    state.fields.drop("contact.attribution_campaign")

    with pytest.raises(AttributionMappingError) as excinfo:
        AttributionEngine(client)

    assert excinfo.value.drift[0].key == "utm_campaign"
    assert state.store.created == 0


def test_three_deliveries_produce_one_contact_and_one_write(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)

    results = [engine.ingest(_payload()) for _ in range(3)]

    assert [result.status for result in results] == [
        IngestStatus.APPLIED,
        IngestStatus.DUPLICATE,
        IngestStatus.DUPLICATE,
    ]
    assert state.store.created == 1
    assert state.store.updated == 0
    assert state.store.custom_field_writes == 5
    assert len(list(state.store.all_contacts())) == 1
    assert state.store.custom_field_writes == len(results[0].custom_fields)


def test_later_touch_preserves_credit_and_advances_last_touch(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)
    engine.ingest(_payload())

    second = engine.ingest(
        _payload(
            event_id="evt-2",
            utm_source="facebook",
            utm_medium="social",
            utm_campaign="retarget",
            occurred_at="2026-11-21T10:00:00Z",
        )
    )

    assert second.status is IngestStatus.APPLIED
    assert second.applied == ("last_touch_at",)
    assert set(second.preserved) >= {"utm_source", "utm_medium", "utm_campaign", "first_touch_at"}

    contact = list(state.store.all_contacts())[0].to_dict()
    values = _fields_by_key(contact)
    assert values["cf_attribution_source"] == "google"
    assert values["cf_attribution_campaign"] == "spring"
    assert values["cf_attribution_last_touch_at"] == "2026-11-21T10:00:00Z"
    assert values["cf_attribution_first_touch_at"] == "2026-11-20T10:00:00Z"
    assert contact["tags"] == ["attribution:google"]


def test_dry_run_returns_plan_without_writing(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)

    result = engine.ingest(_payload(), dry_run=True)

    assert result.status is IngestStatus.PLANNED
    assert result.wrote is False
    assert set(result.applied) == {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "first_touch_at",
        "last_touch_at",
    }
    assert state.store.created == 0
    assert state.store.custom_field_writes == 0
    assert engine.claims.held("loc_test:evt-1") is False


def test_failed_write_releases_claim_so_retry_is_the_same_operation(mock_client):
    state, client = mock_client
    state.queue_fault("/contacts/upsert", Fault(status=502, times=2))
    engine = AttributionEngine(client)

    with pytest.raises(Exception):
        engine.ingest(_payload())

    assert engine.claims.held("loc_test:evt-1") is False

    retry = engine.ingest(_payload())

    assert retry.status is IngestStatus.APPLIED
    assert state.store.created == 1
    assert len(list(state.store.all_contacts())) == 1


def test_redelivery_under_new_event_id_is_unchanged(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)
    engine.ingest(_payload())
    writes_before = state.store.custom_field_writes

    result = engine.ingest(_payload(event_id="evt-1-replay"))

    assert result.status is IngestStatus.UNCHANGED
    assert state.store.custom_field_writes == writes_before
    assert state.store.created == 1


def test_contact_without_attribution_signal_is_not_created(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)

    result = engine.ingest(
        {"event_id": "evt-organic", "email": "organic@example.com", "utm_source": "direct"}
    )

    assert result.status is IngestStatus.UNCHANGED
    assert state.store.created == 0


def test_invalid_payload_reports_every_problem(mock_client):
    _state, client = mock_client
    engine = AttributionEngine(client)

    with pytest.raises(InvalidAttributionEvent) as excinfo:
        engine.ingest({"utm_source": "google"})

    problems = " ".join(excinfo.value.problems)
    assert "event_id" in problems
    assert "email or phone" in problems


def test_discovery_refresh_picks_up_retyped_field(mock_client):
    state, client = mock_client
    engine = AttributionEngine(client)
    state.fields.retype("contact.attribution_first_touch_at", "TEXT")

    with pytest.raises(AttributionMappingError):
        engine.discover(refresh=True)
