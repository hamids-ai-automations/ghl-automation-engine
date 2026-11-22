"""GHL-022: escalation rules and the failure behaviour that goes with them."""

from __future__ import annotations

import pytest

from ghl_triage import taxonomy
from ghl_triage.escalation import (
    REASON_PRECEDENCE,
    EscalationPolicy,
    EscalationReason,
    decide,
    fail_closed,
    requests_human,
)
from ghl_triage.taxonomy import Entity, Intent

LEAD_TEXT = "Hi, do you do local SEO for a dental clinic? What is a ballpark?"


def test_happy_path_routes_without_escalating_but_never_auto_replies():
    plan = decide(
        Intent.LEAD_INQUIRY,
        0.91,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
    )

    assert plan.escalate is False
    assert plan.queue == taxonomy.QUEUE_LEADS
    assert plan.reasons == ()
    assert plan.auto_reply_allowed is False
    assert plan.draft_allowed is True


def test_low_confidence_escalates_to_human():
    plan = decide(
        Intent.LEAD_INQUIRY,
        0.54,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
    )

    assert plan.escalate is True
    assert plan.queue == taxonomy.QUEUE_HUMAN
    assert plan.reasons == (EscalationReason.LOW_CONFIDENCE,)
    assert plan.draft_allowed is False


def test_confidence_floor_is_inclusive():
    policy = EscalationPolicy(confidence_floor=0.55)

    at_floor = decide(
        Intent.LEAD_INQUIRY,
        0.55,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
        policy=policy,
    )
    below = decide(
        Intent.LEAD_INQUIRY,
        0.5499,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
        policy=policy,
    )

    assert at_floor.escalate is False
    assert below.escalate is True


def test_human_only_intent_escalates_even_at_full_confidence():
    plan = decide(
        Intent.BILLING,
        1.0,
        text="You charged me twice for invoice 1042.",
        present_entities=[Entity.INVOICE_OR_AMOUNT],
    )

    assert plan.escalate is True
    assert plan.queue == taxonomy.QUEUE_HUMAN
    assert plan.reasons == (EscalationReason.HUMAN_ONLY_INTENT,)


def test_explicit_request_for_a_person_escalates():
    plan = decide(
        Intent.CLIENT_SUPPORT,
        0.95,
        text="The campaign is still paused. I want to SPEAK   TO A HUMAN about it.",
        present_entities=[Entity.ORDER_OR_ACCOUNT],
    )

    assert plan.escalate is True
    assert EscalationReason.HUMAN_REQUESTED in plan.reasons


def test_human_request_detection_is_case_and_spacing_insensitive():
    assert requests_human("can I talk to a person please") is True
    assert requests_human("TALK TO A HUMAN") is True
    assert requests_human("talk   to   a   human") is True
    assert requests_human("the campaign performs well") is False


def test_missing_required_entity_escalates_instead_of_guessing():
    plan = decide(
        Intent.APPOINTMENT,
        0.93,
        text="Can we move it to later next week?",
        present_entities=[],
    )

    assert plan.escalate is True
    assert plan.reasons == (EscalationReason.MISSING_ENTITY,)

    satisfied = decide(
        Intent.APPOINTMENT,
        0.93,
        text="Can we move it to Thursday 3pm?",
        present_entities=[Entity.DATE_OR_TIME],
    )
    assert satisfied.escalate is False
    assert satisfied.queue == taxonomy.QUEUE_BOOKINGS


def test_empty_message_escalates():
    plan = decide(Intent.UNCLEAR, 0.9, text="   ")

    assert plan.escalate is True
    assert plan.reasons == (
        EscalationReason.EMPTY_MESSAGE,
        EscalationReason.HUMAN_ONLY_INTENT,
    )


def test_repeated_contact_escalates_at_the_threshold():
    policy = EscalationPolicy(max_messages_before_escalation=3)

    twice = decide(
        Intent.LEAD_INQUIRY,
        0.9,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
        message_count=2,
        policy=policy,
    )
    third = decide(
        Intent.LEAD_INQUIRY,
        0.9,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
        message_count=3,
        policy=policy,
    )

    assert twice.escalate is False
    assert third.escalate is True
    assert third.reasons == (EscalationReason.REPEATED_CONTACT,)


def test_already_escalated_thread_stays_with_a_human():
    plan = decide(
        Intent.LEAD_INQUIRY,
        0.99,
        text=LEAD_TEXT,
        present_entities=[Entity.CONTACT_NAME],
        already_escalated=True,
    )

    assert plan.escalate is True
    assert plan.reasons == (EscalationReason.ALREADY_ESCALATED,)


def test_reasons_are_reported_in_a_fixed_order():
    plan = decide(
        Intent.BILLING,
        0.10,
        text="invoice 9 twice? call me instead",
        present_entities=[],
        message_count=9,
        already_escalated=True,
    )

    assert plan.reasons == (
        EscalationReason.HUMAN_ONLY_INTENT,
        EscalationReason.HUMAN_REQUESTED,
        EscalationReason.MISSING_ENTITY,
        EscalationReason.LOW_CONFIDENCE,
        EscalationReason.REPEATED_CONTACT,
        EscalationReason.ALREADY_ESCALATED,
    )
    assert list(plan.reasons) == [r for r in REASON_PRECEDENCE if r in plan.reasons]


def test_fail_closed_keeps_failures_inside_the_taxonomy():
    plan = fail_closed()

    assert plan.intent is Intent.UNCLEAR
    assert plan.confidence == 0.0
    assert plan.queue == taxonomy.QUEUE_HUMAN
    assert plan.escalate is True
    assert plan.reasons == (EscalationReason.CLASSIFIER_ERROR,)
    assert plan.auto_reply_allowed is False


def test_fail_closed_refuses_reasons_it_cannot_justify():
    with pytest.raises(ValueError, match="classifier_error or empty_message"):
        fail_closed(EscalationReason.LOW_CONFIDENCE)


def test_plan_serialises_without_leaking_automation_opt_in():
    payload = decide(
        Intent.QUOTE_REQUEST,
        0.88,
        text="Need a quote for 40 landing pages by December.",
        present_entities=[Entity.SERVICE],
    ).to_dict()

    assert payload["intent"] == "quote_request"
    assert payload["queue"] == taxonomy.QUEUE_LEADS
    assert payload["auto_reply_allowed"] is False
    assert payload["draft_allowed"] is True
    assert payload["reasons"] == []


@pytest.mark.parametrize("intent", list(Intent))
def test_no_intent_can_ever_auto_reply(intent):
    plan = decide(
        intent,
        1.0,
        text=LEAD_TEXT,
        present_entities=list(Entity),
        message_count=1,
    )

    assert plan.auto_reply_allowed is False


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2])
def test_confidence_outside_the_unit_interval_is_rejected(confidence):
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        decide(Intent.LEAD_INQUIRY, confidence, text=LEAD_TEXT)


def test_policy_rejects_nonsense_thresholds():
    with pytest.raises(ValueError, match="confidence_floor"):
        EscalationPolicy(confidence_floor=1.5)
    with pytest.raises(ValueError, match="max_messages_before_escalation"):
        EscalationPolicy(max_messages_before_escalation=0)


def test_human_request_escalation_can_be_switched_off_explicitly():
    policy = EscalationPolicy(escalate_on_human_request=False)

    plan = decide(
        Intent.CLIENT_SUPPORT,
        0.95,
        text="Please speak to a human about the paused campaign.",
        present_entities=[Entity.ORDER_OR_ACCOUNT],
        policy=policy,
    )

    assert plan.escalate is False
