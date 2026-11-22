"""Incremental contact extractor tests (GHL-025).

Each test answers one question an operator would ask before trusting a nightly
sync: does a replay double-load, does a crash lose the window, does a partial page
walk silently advance the mark, does a re-poll invent new versions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ghl_client.pagination import Collection
from ghl_warehouse import (
    ExtractStats,
    InMemoryWarehouse,
    InMemoryWatermarkStore,
    IncompleteExtractError,
    IncrementalContactExtractor,
    normalise_contact,
)
from ghl_warehouse.watermark import Watermark

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def raw_contact(
    contact_id: str,
    updated: str,
    *,
    email: str = "a@example.com",
    tags: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "id": contact_id,
        "locationId": "loc-1",
        "dateAdded": "2026-11-01T09:00:00Z",
        "dateUpdated": updated,
        "email": email,
        "firstName": "Ada",
        "lastName": "Lovelace",
        "source": "web",
        "tags": tags or ["lead"],
    }
    payload.update(extra)
    return payload


class FakeSource:
    """Stands in for ``ContactsResource`` including its failure modes."""

    def __init__(self, items: list[dict[str, Any]], *, complete: bool = True, pages: int = 1) -> None:
        self.items = items
        self.complete = complete
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> Collection[dict[str, Any]]:
        self.calls.append(kwargs)
        return Collection(
            items=list(self.items),
            pages_fetched=self.pages,
            complete=self.complete,
            truncated_reason=None if self.complete else "page_failure",
        )


def build(
    items: list[dict[str, Any]],
    *,
    sink: InMemoryWarehouse | None = None,
    store: InMemoryWatermarkStore | None = None,
    mark: str | None = None,
    overlap_seconds: int = 300,
    complete: bool = True,
) -> tuple[IncrementalContactExtractor, InMemoryWarehouse, InMemoryWatermarkStore, FakeSource]:
    warehouse = sink or InMemoryWarehouse()
    marks = store or InMemoryWatermarkStore()
    if mark:
        marks.put(Watermark(entity="contacts", value=mark, tie_breaker="c1"))
    source = FakeSource(items, complete=complete)
    extractor = IncrementalContactExtractor(
        source, warehouse, marks, overlap_seconds=overlap_seconds
    )
    return extractor, warehouse, marks, source


class TestFirstRun:
    def test_loads_every_contact(self) -> None:
        items = [raw_contact("c1", "2026-11-20T10:00:00Z"), raw_contact("c2", "2026-11-21T10:00:00Z")]
        extractor, warehouse, marks, _ = build(items)

        stats = extractor.run(now=NOW)

        assert stats.new_versions == 2
        assert stats.rejected == 0
        assert warehouse.contact_count() == 2
        assert marks.get("contacts").value == "2026-11-21T10:00:00Z"

    def test_fact_row_per_version_with_measures(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)

        fact = next(iter(warehouse.fact_contact_version.values()))
        assert fact["attributes_changed"] == 5  # email, phone? no: tracked non-null fields
        assert fact["days_since_created"] == 19
        assert fact["observed_date_key"] == 20261122

    def test_dimension_row_is_current_and_open(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)

        row = warehouse.current_version(next(iter(warehouse.dim_contact))[0])
        assert row is not None
        assert row["is_current"] == 1
        assert row["valid_to"] is None
        assert row["version"] == 1
        assert row["created_date_key"] == 20261101

    def test_date_dimension_is_populated_for_created_and_observed(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)
        assert {20261101, 20261122} <= set(warehouse.dim_date)


class TestIdempotency:
    def test_replay_creates_no_new_versions(self) -> None:
        items = [raw_contact("c1", "2026-11-20T10:00:00Z")]
        extractor, warehouse, marks, _ = build(items)
        extractor.run(now=NOW)
        first = marks.get("contacts")

        replay_stats = extractor.run(now=NOW + timedelta(hours=1))

        assert replay_stats.new_versions == 0
        assert replay_stats.unchanged == 1
        assert warehouse.contact_count() == 1
        assert len(warehouse.dim_contact) == 1
        assert marks.get("contacts").value == first.value

    def test_identical_payload_with_new_timestamp_is_not_a_version(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)

        source = FakeSource([raw_contact("c1", "2026-11-21T10:00:00Z")])
        extractor._source = source  # same attributes, later poll
        stats = extractor.run(now=NOW)

        assert stats.unchanged == 1
        assert len(warehouse.dim_contact) == 1

    def test_tag_order_is_not_a_change(self) -> None:
        extractor, warehouse, _, _ = build(
            [raw_contact("c1", "2026-11-20T10:00:00Z", tags=["lead", "vip"])]
        )
        extractor.run(now=NOW)
        extractor._source = FakeSource(
            [raw_contact("c1", "2026-11-21T10:00:00Z", tags=["vip", "lead"])]
        )
        assert extractor.run(now=NOW).unchanged == 1
        assert len(warehouse.dim_contact) == 1


class TestVersionHistory:
    def test_attribute_change_closes_previous_version(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)

        extractor._source = FakeSource(
            [raw_contact("c1", "2026-11-21T10:00:00Z", email="new@example.com")]
        )
        stats = extractor.run(now=NOW)

        assert stats.new_versions == 1
        key = next(iter(warehouse.dim_contact))[0]
        versions = warehouse.versions(key)
        assert [row["version"] for row in versions] == [1, 2]
        assert versions[0]["is_current"] == 0
        assert versions[0]["valid_to"] == "2026-11-21T10:00:00Z"
        assert versions[1]["is_current"] == 1
        assert versions[1]["email"] == "new@example.com"

    def test_exactly_one_current_version(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)
        for updated, email in (
            ("2026-11-21T10:00:00Z", "b@example.com"),
            ("2026-11-21T11:00:00Z", "c@example.com"),
        ):
            extractor._source = FakeSource([raw_contact("c1", updated, email=email)])
            extractor.run(now=NOW)

        key = next(iter(warehouse.dim_contact))[0]
        rows = warehouse.versions(key)
        assert len(rows) == 3
        assert sum(1 for row in rows if row["is_current"]) == 1
        assert warehouse.fact_contact_version[(key, 2)]["attributes_changed"] == 1

    def test_older_delivery_does_not_rewrite_history(self) -> None:
        extractor, warehouse, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        extractor.run(now=NOW)

        extractor._source = FakeSource(
            [raw_contact("c1", "2026-11-20T09:59:00Z", email="late@example.com")]
        )
        stats = extractor.run(now=NOW)

        assert stats.stale_ignored == 1
        assert len(warehouse.dim_contact) == 1


class TestWindow:
    def test_record_outside_overlapped_window_is_not_loaded(self) -> None:
        extractor, warehouse, _, _ = build(
            [raw_contact("old", "2026-11-20T00:00:00Z")],
            mark="2026-11-22T10:00:00Z",
            overlap_seconds=300,
        )
        stats = extractor.run(now=NOW)

        assert stats.outside_window == 1
        assert stats.new_versions == 0
        assert warehouse.contact_count() == 0

    def test_record_inside_overlap_is_loaded(self) -> None:
        extractor, warehouse, _, _ = build(
            [raw_contact("recent", "2026-11-22T09:58:00Z")],
            mark="2026-11-22T10:00:00Z",
            overlap_seconds=300,
        )
        stats = extractor.run(now=NOW)

        assert stats.new_versions == 1
        assert warehouse.contact_count() == 1

    def test_watermark_advances_only_to_loaded_maximum(self) -> None:
        extractor, _, marks, _ = build(
            [
                raw_contact("c1", "2026-11-22T09:59:00Z"),
                raw_contact("old", "2026-11-01T00:00:00Z"),
            ],
            mark="2026-11-22T09:00:00Z",
        )
        extractor.run(now=NOW)
        assert marks.get("contacts").value == "2026-11-22T09:59:00Z"

    def test_watermark_untouched_when_nothing_in_window(self) -> None:
        extractor, _, marks, _ = build(
            [raw_contact("old", "2026-11-01T00:00:00Z")], mark="2026-11-22T10:00:00Z"
        )
        stats = extractor.run(now=NOW)
        assert stats.watermark_moved is False
        assert marks.get("contacts").value == "2026-11-22T10:00:00Z"


class TestFailureBehaviour:
    def test_incomplete_collection_refuses_to_load(self) -> None:
        extractor, warehouse, marks, _ = build(
            [raw_contact("c1", "2026-11-20T10:00:00Z")], complete=False
        )
        with pytest.raises(IncompleteExtractError):
            extractor.run(now=NOW)

        assert warehouse.contact_count() == 0
        assert marks.get("contacts") is None

    def test_sink_failure_leaves_watermark_untouched(self) -> None:
        class ExplodingSink(InMemoryWarehouse):
            def load_contact(self, record, *, observed_at):  # noqa: ANN001, ANN201
                raise RuntimeError("warehouse down")

        extractor, _, marks, _ = build(
            [raw_contact("c1", "2026-11-20T10:00:00Z")], sink=ExplodingSink()
        )
        with pytest.raises(RuntimeError):
            extractor.run(now=NOW)

        assert marks.get("contacts") is None

    def test_invalid_records_are_reported_not_fatal(self) -> None:
        items = [
            raw_contact("c1", "2026-11-20T10:00:00Z"),
            {"locationId": "loc-1"},  # no id, no dateUpdated
            {"id": "c3"},  # no dateUpdated
        ]
        extractor, warehouse, marks, _ = build(items)
        stats = extractor.run(now=NOW)

        assert stats.rejected == 2
        assert stats.new_versions == 1
        assert len(stats.rejections) == 2
        assert warehouse.contact_count() == 1
        assert marks.get("contacts").value == "2026-11-20T10:00:00Z"

    def test_rejection_samples_are_capped(self) -> None:
        items = [{"locationId": "loc-1"} for _ in range(10)]
        extractor, _, _, _ = build(items)
        stats = extractor.run(now=NOW)
        assert stats.rejected == 10
        assert len(stats.rejections) == extractor.max_rejection_samples


class TestDryRun:
    def test_dry_run_loads_nothing_and_holds_the_mark(self) -> None:
        extractor, warehouse, marks, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        stats = extractor.run(now=NOW, dry_run=True)

        assert stats.dry_run is True
        assert stats.new_versions == 1
        assert warehouse.loads == 0
        assert marks.get("contacts") is None
        assert stats.watermark_after == ""

    def test_dry_run_is_a_plan_not_a_write(self) -> None:
        extractor, warehouse, marks, _ = build(
            [raw_contact("c1", "2026-11-22T09:59:00Z")], mark="2026-11-22T09:00:00Z"
        )
        extractor.run(now=NOW, dry_run=True)
        assert warehouse.dim_contact == {}
        assert marks.get("contacts").value == "2026-11-22T09:00:00Z"


class TestReporting:
    def test_stats_serialise_for_logs(self) -> None:
        extractor, _, _, _ = build([raw_contact("c1", "2026-11-20T10:00:00Z")])
        payload = extractor.run(now=NOW).to_dict()
        assert payload["entity"] == "contacts"
        assert payload["new_versions"] == 1
        assert set(payload) >= {"pages", "scanned", "accepted", "rejected", "dry_run"}

    def test_page_count_is_reported(self) -> None:
        warehouse = InMemoryWarehouse()
        marks = InMemoryWatermarkStore()
        source = FakeSource([raw_contact("c1", "2026-11-20T10:00:00Z")], pages=4)
        stats = IncrementalContactExtractor(source, warehouse, marks).run(now=NOW)
        assert stats.pages == 4

    def test_pending_keys_are_deterministic(self) -> None:
        extractor, _, _, _ = build([])
        keys = extractor.pending_keys([raw_contact("c1", "2026-11-20T10:00:00Z")])
        again = extractor.pending_keys([raw_contact("c1", "2026-11-20T10:00:00Z")])
        assert keys == again and len(keys) == 1


class TestNormalisation:
    def test_missing_timestamp_is_refused(self) -> None:
        from ghl_warehouse import InvalidRecordError

        with pytest.raises(InvalidRecordError):
            normalise_contact({"id": "c1", "locationId": "loc-1"})

    def test_tags_are_normalised_case_insensitively(self) -> None:
        record = normalise_contact(raw_contact("c1", "2026-11-20T10:00:00Z", tags=["VIP", "vip "]))
        assert record.tags == ("vip",)

    def test_created_at_falls_back_to_updated_at(self) -> None:
        payload = raw_contact("c1", "2026-11-20T10:00:00Z")
        payload.pop("dateAdded")
        assert normalise_contact(payload).created_at == "2026-11-20T10:00:00Z"

    def test_stats_default_is_contacts_entity(self) -> None:
        assert ExtractStats().entity == "contacts"
