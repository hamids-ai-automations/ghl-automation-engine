"""GHL-021: the case demo has to keep telling the truth.

The case study page quotes the numbers in ``examples/case_lead_attribution.py``.
If a change to the engine alters them, this test fails before the page can go
out of date.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture(scope="module")
def evidence() -> dict:
    spec = importlib.util.spec_from_file_location(
        "case_lead_attribution", EXAMPLES / "case_lead_attribution.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.run_case(verbose=False)


def test_one_contact_per_lead(evidence: dict) -> None:
    assert evidence["counters"]["contacts_created"] == 2
    assert [row["email"] for row in evidence["contacts"]] == [
        "ada@example.com",
        "grace@example.com",
    ]


def test_redeliveries_are_duplicates_not_writes(evidence: dict) -> None:
    duplicates = [row for row in evidence["deliveries"] if row["status"] == "duplicate"]
    assert len(duplicates) == 2
    assert all(row["applied"] == [] for row in duplicates)


def test_first_touch_credit_survives_later_touch(evidence: dict) -> None:
    ada = evidence["contacts"][0]
    assert ada["fields"]["utm_source"] == "google"
    assert ada["fields"]["utm_medium"] == "cpc"
    assert ada["fields"]["utm_campaign"] == "spring-launch"
    assert ada["fields"]["first_touch_at"] == "2026-11-16T09:15:00Z"
    assert ada["fields"]["last_touch_at"] == "2026-11-18T18:40:00Z"
    assert ada["tags"] == ["attribution:google"]


def test_direct_revisit_changes_nothing(evidence: dict) -> None:
    direct = next(row for row in evidence["deliveries"] if row["event_id"] == "evt-2003")
    assert direct["status"] == "unchanged"


def test_dry_run_plans_without_writing(evidence: dict) -> None:
    planned = next(row for row in evidence["deliveries"] if row["dry_run"])
    assert planned["status"] == "planned"
    assert planned["contact_id"] is None
    assert "linus@example.com" not in [row["email"] for row in evidence["contacts"]]


def test_drift_refuses_startup_then_recovers(evidence: dict) -> None:
    drift = evidence["drift"]
    assert drift["refused"] is True
    assert "first_touch_at" in drift["error"]
    assert drift["recovers_after_fix"] is True


def test_case_demo_exit_code():
    """The demo is also the acceptance command, so it must exit 0."""

    spec = importlib.util.spec_from_file_location(
        "case_lead_attribution_main", EXAMPLES / "case_lead_attribution.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.main() == 0
