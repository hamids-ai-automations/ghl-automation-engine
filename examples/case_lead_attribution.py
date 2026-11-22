"""GHL-021: runnable attribution case against the offline mock.

This is the demo behind ``docs/CASE-STUDY-ATTRIBUTION.md``. It replays one
relay misbehaving the way real relays misbehave — three deliveries of the same
event, a later touch on the same lead, a direct revisit, a second lead from a
different channel, one dry run, and a schema drift — and prints what the engine
did with each.

Everything here is fixture data served by ``ghl_mock``; no customer data and no
measured production figures are involved.

Run: python examples/case_lead_attribution.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ghl_attribution import AttributionEngine, AttributionMappingError  # noqa: E402
from ghl_client.auth import PrivateIntegrationToken  # noqa: E402
from ghl_client.client import GhlClient  # noqa: E402
from ghl_client.transport import UrllibTransport  # noqa: E402
from ghl_mock import DEFAULT_TOKEN, MockState, serve_in_thread  # noqa: E402

LOCATION_ID = "loc_case"


# --- fixture: one week of a lead-intake relay -------------------------------

FIRST_DELIVERY = {
    "event_id": "evt-2001",
    "email": "ada@example.com",
    "first_name": "Ada",
    "utm_source": "google",
    "utm_medium": "cpc",
    "utm_campaign": "spring-launch",
    "utm_content": "headline-b",
    "landing_page": "/pricing",
    "gclid": "Cj0KCQ-paid-click",
    "occurred_at": "2026-11-16T09:15:00Z",
}

LATER_TOUCH = {
    "event_id": "evt-2002",
    "email": "ada@example.com",
    "utm_source": "newsletter",
    "utm_medium": "email",
    "utm_campaign": "november-digest",
    "landing_page": "/blog/attribution",
    "occurred_at": "2026-11-18T18:40:00Z",
}

DIRECT_REVISIT = {
    "event_id": "evt-2003",
    "email": "ada@example.com",
    "utm_source": "direct",
    "occurred_at": "2026-11-19T07:05:00Z",
}

SECOND_LEAD = {
    "event_id": "evt-2004",
    "email": "grace@example.com",
    "first_name": "Grace",
    "utm_source": "meta",
    "utm_medium": "paid_social",
    "utm_campaign": "retargeting-q4",
    "landing_page": "/demo",
    "fbclid": "IwAR-facebook-click",
    "occurred_at": "2026-11-19T11:02:00Z",
}

DRY_RUN_LEAD = {
    "event_id": "evt-2005",
    "email": "linus@example.com",
    "first_name": "Linus",
    "utm_source": "google",
    "utm_medium": "cpc",
    "utm_campaign": "spring-launch",
    "landing_page": "/pricing",
    "occurred_at": "2026-11-20T08:30:00Z",
}


# --- case -------------------------------------------------------------------


def _field_snapshot(state: MockState, field_ids: dict[str, str]) -> list[dict[str, Any]]:
    by_id = {field_id: key for key, field_id in field_ids.items()}
    rows: list[dict[str, Any]] = []
    for contact in state.store.all_contacts():
        raw = contact.to_dict()
        values = {
            by_id.get(str(item["id"]), str(item["id"])): item.get("value")
            for item in raw.get("customFields") or []
        }
        rows.append({"email": raw["email"], "tags": raw["tags"], "fields": values})
    return sorted(rows, key=lambda row: row["email"] or "")


def run_case(*, verbose: bool = True) -> dict[str, Any]:
    """Replay the case and return evidence. No asserts here: the test asserts."""

    state = MockState()
    server, base_url = serve_in_thread(state)
    try:
        client = GhlClient(
            PrivateIntegrationToken(DEFAULT_TOKEN),
            base_url=base_url,
            transport=UrllibTransport(),
            location_id=LOCATION_ID,
        )
        engine = AttributionEngine(client)
        field_ids = dict(engine.resolved.field_ids)

        deliveries: list[dict[str, Any]] = []

        def deliver(kind: str, payload: dict[str, Any], *, dry_run: bool = False) -> None:
            result = engine.ingest(payload, dry_run=dry_run)
            deliveries.append(
                {
                    "kind": kind,
                    "event_id": payload["event_id"],
                    "email": payload["email"],
                    "status": result.status.value,
                    "contact_id": result.contact_id,
                    "applied": list(result.applied),
                    "preserved": list(result.preserved),
                    "reason": result.reason,
                    "dry_run": dry_run,
                }
            )

        deliver("first touch (paid search)", FIRST_DELIVERY)
        deliver("same event, redelivered", FIRST_DELIVERY)
        deliver("same event, redelivered again", FIRST_DELIVERY)
        deliver("later touch (newsletter)", LATER_TOUCH)
        deliver("direct revisit", DIRECT_REVISIT)
        deliver("second lead (paid social)", SECOND_LEAD)
        deliver("dry run (new lead)", DRY_RUN_LEAD, dry_run=True)

        counters = {
            "contacts_created": state.store.created,
            "contacts_updated": state.store.updated,
            "custom_field_writes": state.store.custom_field_writes,
        }
        contacts_after = _field_snapshot(state, field_ids)

        # Drift: someone retypes the first-touch date to a text field in the
        # location schema. The engine must refuse to start rather than write
        # timestamps into a text column.
        drift: dict[str, Any] = {
            "field_key": "contact.attribution_first_touch_at",
            "retyped_to": "TEXT",
            "refused": False,
            "error": "",
            "problems": [],
        }
        state.fields.retype("contact.attribution_first_touch_at", "TEXT")
        try:
            engine.discover(refresh=True)
        except AttributionMappingError as exc:
            drift["refused"] = True
            drift["error"] = str(exc)
            drift["problems"] = [str(item) for item in getattr(exc, "problems", ())]
        finally:
            state.fields.retype("contact.attribution_first_touch_at", "DATE")

        # Recovery: once the schema is sane again the engine starts normally.
        drift["recovers_after_fix"] = bool(engine.discover(refresh=True).field_ids)

        evidence = {
            "location_id": LOCATION_ID,
            "schema_bound": field_ids,
            "deliveries": deliveries,
            "counters": counters,
            "contacts": contacts_after,
            "drift": drift,
            "status_counts": _count(deliveries),
        }
        if verbose:
            _print_report(evidence)
        return evidence
    finally:
        server.shutdown()
        server.server_close()


def _count(deliveries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in deliveries:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _print_report(evidence: dict[str, Any]) -> None:
    print("## What arrived\n")
    print("| # | delivery | event | status | wrote | fields touched |")
    print("| - | --- | --- | --- | --- | --- |")
    for index, row in enumerate(evidence["deliveries"], start=1):
        wrote = "no" if row["dry_run"] else ("yes" if row["status"] == "applied" else "no")
        touched = ", ".join(row["applied"]) or "—"
        print(f"| {index} | {row['kind']} | {row['event_id']} | {row['status']} | {wrote} | {touched} |")

    counters = evidence["counters"]
    print("\n## What the location ended up with\n")
    print(
        f"contacts created: {counters['contacts_created']} · "
        f"contacts updated: {counters['contacts_updated']} · "
        f"custom-field writes: {counters['custom_field_writes']}"
    )
    for contact in evidence["contacts"]:
        print(f"\n{contact['email']}  tags={contact['tags']}")
        for key in sorted(contact["fields"]):
            print(f"  {key}: {contact['fields'][key]}")

    drift = evidence["drift"]
    print("\n## Schema drift\n")
    print(f"retype {drift['field_key']} -> {drift['retyped_to']}")
    if drift["refused"]:
        print(f"engine refused to start: {drift['error']}")
        for problem in drift["problems"]:
            print(f"  - {problem}")
    else:
        print("engine started anyway — this is a bug")
    print(f"recovers after fix: {drift['recovers_after_fix']}")

    print("\n## Outcome counts\n")
    for status in sorted(evidence["status_counts"]):
        print(f"  {status}: {evidence['status_counts'][status]}")


def main() -> int:
    evidence = run_case()
    statuses = evidence["status_counts"]
    counters = evidence["counters"]
    checks = [
        (counters["contacts_created"] == 2, "two leads, two contacts (not five)"),
        (statuses.get("duplicate", 0) == 2, "two redeliveries rejected as duplicates"),
        (statuses.get("unchanged", 0) == 1, "direct revisit changed nothing"),
        (statuses.get("planned", 0) == 1, "dry run wrote nothing"),
        (evidence["drift"]["refused"], "drift refused before first write"),
    ]
    failed = [label for ok, label in checks if not ok]
    if failed:
        print("\nFAILED: " + "; ".join(failed))
        return 1
    print("\nok: 5/5 case invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
