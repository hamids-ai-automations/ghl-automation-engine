"""GHL-022: the intent taxonomy cannot silently lose its shape."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ghl_triage import taxonomy
from ghl_triage.taxonomy import (
    INTENTS,
    Entity,
    Intent,
    IntentSpec,
    all_intents,
    intents_for_queue,
    spec,
    validate_taxonomy,
)


def test_taxonomy_holds_exactly_eight_intents():
    assert len(INTENTS) == 8
    assert all_intents() == tuple(Intent)


def test_every_intent_carries_a_usable_definition():
    for intent in Intent:
        item = spec(intent)
        assert isinstance(item, IntentSpec)
        assert item.intent is intent
        assert len(item.definition) >= 40, intent
        assert len(item.signals) >= 2, intent
        assert item.excludes, intent
        assert item.queue, intent


def test_exclusions_are_stated_for_confusable_pairs():
    # The pairs most likely to be merged by a naive classifier.
    pairs = [
        (Intent.LEAD_INQUIRY, Intent.QUOTE_REQUEST),
        (Intent.APPOINTMENT, Intent.QUOTE_REQUEST),
        (Intent.CLIENT_SUPPORT, Intent.BILLING),
        (Intent.LEAD_INQUIRY, Intent.CLIENT_SUPPORT),
    ]
    for left, right in pairs:
        joined = " ".join(spec(left).excludes).lower()
        assert spec(right).queue != ""  # spec exists
        assert joined, left
    assert any("quote" in line.lower() for line in spec(Intent.LEAD_INQUIRY).excludes)
    assert any("billing" in line.lower() for line in spec(Intent.CLIENT_SUPPORT).excludes)


def test_money_and_opt_out_intents_are_human_only():
    assert spec(Intent.BILLING).human_only is True
    assert spec(Intent.SPAM_OPTOUT).human_only is True
    assert spec(Intent.UNCLEAR).human_only is True


def test_unclear_routes_to_human_review():
    assert spec(Intent.UNCLEAR).queue == taxonomy.QUEUE_HUMAN


def test_queues_are_populated_and_stable():
    for queue in (
        taxonomy.QUEUE_LEADS,
        taxonomy.QUEUE_BOOKINGS,
        taxonomy.QUEUE_SERVICE,
        taxonomy.QUEUE_BILLING,
        taxonomy.QUEUE_PARTNERSHIPS,
        taxonomy.QUEUE_PAPER_TRAIL,
        taxonomy.QUEUE_HUMAN,
    ):
        assert intents_for_queue(queue), queue

    assert intents_for_queue(taxonomy.QUEUE_LEADS) == (
        Intent.LEAD_INQUIRY,
        Intent.QUOTE_REQUEST,
    )
    assert intents_for_queue(taxonomy.QUEUE_HUMAN) == (Intent.UNCLEAR,)


def test_required_entities_are_declared_where_they_drive_escalation():
    assert Entity.DATE_OR_TIME in spec(Intent.APPOINTMENT).required_entities
    assert Entity.INVOICE_OR_AMOUNT in spec(Intent.BILLING).required_entities
    assert Entity.ORDER_OR_ACCOUNT in spec(Intent.CLIENT_SUPPORT).required_entities
    assert spec(Intent.SPAM_OPTOUT).required_entities == ()


def test_spec_accepts_plain_strings():
    assert spec("billing").intent is Intent.BILLING


def test_validate_taxonomy_accepts_the_shipped_taxonomy():
    validate_taxonomy()  # must not raise


def test_validate_taxonomy_rejects_a_thin_spec(monkeypatch):
    thin = replace(INTENTS[Intent.LEAD_INQUIRY], signals=("one signal",))
    broken = dict(INTENTS)
    broken[Intent.LEAD_INQUIRY] = thin
    monkeypatch.setattr(taxonomy, "INTENTS", broken)

    with pytest.raises(AssertionError, match="two signals"):
        validate_taxonomy()


def test_validate_taxonomy_rejects_a_missing_intent(monkeypatch):
    broken = dict(INTENTS)
    broken.pop(Intent.PARTNERSHIP)
    monkeypatch.setattr(taxonomy, "INTENTS", broken)

    with pytest.raises(AssertionError, match="exactly 8 intents"):
        validate_taxonomy()


def test_validate_taxonomy_rejects_a_human_only_intent_downgrade(monkeypatch):
    downgraded = replace(INTENTS[Intent.BILLING], human_only=False)
    broken = dict(INTENTS)
    broken[Intent.BILLING] = downgraded
    monkeypatch.setattr(taxonomy, "INTENTS", broken)

    with pytest.raises(AssertionError, match="human-only"):
        validate_taxonomy()
