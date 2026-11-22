from ghl_attribution.events import AttributionEvent
from ghl_attribution.policy import AttributionPolicy


def _event(**kwargs):
    payload = {
        "event_id": "evt-1",
        "email": "lead@example.com",
        "utm_source": "google",
        "utm_medium": "cpc",
        "utm_campaign": "spring",
        "occurred_at": "2026-11-20T10:00:00Z",
    }
    payload.update(kwargs)
    return AttributionEvent.from_payload(payload)


def test_first_touch_writes_full_credit_set():
    plan = AttributionPolicy().compute({}, _event())

    assert set(plan.applied) == {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "first_touch_at",
        "last_touch_at",
    }
    assert plan.values["utm_source"] == "google"
    assert plan.preserved == ()


def test_earned_credit_is_never_rewritten():
    existing = {"utm_source": "google", "utm_medium": "cpc"}
    event = _event(event_id="evt-2", utm_source="facebook", utm_medium="social")

    plan = AttributionPolicy().compute(existing, event)

    assert "utm_source" not in plan.applied
    assert set(plan.preserved) >= {"utm_source", "utm_medium"}
    assert plan.overwritten == ()


def test_direct_traffic_cannot_overwrite_a_campaign():
    existing = {"utm_source": "google", "utm_campaign": "spring"}
    direct = _event(event_id="evt-3", utm_source=None, utm_medium=None, utm_campaign=None)

    plan = AttributionPolicy().compute(existing, direct)

    assert plan.values.get("utm_source") is None
    assert "utm_source" not in plan.applied


def test_last_touch_only_moves_forward():
    existing = {"last_touch_at": "2026-11-21T00:00:00Z"}
    older = _event(event_id="evt-4", occurred_at="2026-11-19T00:00:00Z")
    newer = _event(event_id="evt-5", occurred_at="2026-11-22T00:00:00Z")

    assert "last_touch_at" not in AttributionPolicy().compute(existing, older).applied
    assert "last_touch_at" in AttributionPolicy().compute(existing, newer).applied


def test_overwrite_is_opt_in():
    existing = {"utm_source": "google"}
    event = _event(event_id="evt-6", utm_source="facebook")

    plan = AttributionPolicy(overwrite_existing=True).compute(existing, event)

    assert "utm_source" in plan.applied
    assert plan.overwritten == ("utm_source",)


def test_unchanged_values_are_reported_as_preserved_not_written():
    existing = {"utm_source": "google", "last_touch_at": "2026-11-20T10:00:00Z"}
    plan = AttributionPolicy().compute(existing, _event())

    assert "utm_source" not in plan.applied
    assert "utm_source" in plan.preserved


def test_existing_fields_without_new_signal_are_skipped():
    existing = {"fbclid": "abc123"}
    plan = AttributionPolicy().compute(existing, _event())

    assert "fbclid" in plan.skipped
