"""Opportunity grain tests (GHL-026).

The contact tests ask whether a sync double-loads. These ask the harder question:
does the warehouse still say the right thing about *money* after a deal moves
stage, after a value string arrives as text, after the provider invents a status
nobody coded for. Each test name is the question, and the assertion is the answer
an operator could put in front of a client.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ghl_client.auth import PrivateIntegrationToken
from ghl_client.client import GhlClient
from ghl_client.pagination import Collection
from ghl_client.ratelimit import SlidingWindowLimiter
from ghl_client.resources.opportunities import OpportunitiesResource
from ghl_client.retry import RetryPolicy
from ghl_client.transport import UrllibTransport
from ghl_mock import DEFAULT_TOKEN
from ghl_warehouse import (
    IncompleteExtractError,
    IncrementalOpportunityExtractor,
    InMemoryOpportunityWarehouse,
    InMemoryWatermarkStore,
    InvalidRecordError,
    normalise_opportunity,
    opportunity_row_hash,
)
from ghl_warehouse.opportunities import (
    InMemoryOpportunityWarehouse as WarehouseFromModule,
)
from ghl_warehouse.schema import opportunity_key

NOW = "2026-11-22T12:00:00Z"


def raw_opportunity(
    opportunity_id: str,
    updated: str,
    *,
    status: str = "open",
    stage: str = "stage-new",
    value: object = 1500,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": opportunity_id,
        "locationId": "loc-1",
        "name": f"Deal {opportunity_id}",
        "status": status,
        "pipelineId": "pipe-1",
        "pipelineStageId": stage,
        "pipelineStageName": stage,
        "assignedTo": "user-1",
        "contactId": "contact-1",
        "monetaryValue": value,
        "dateAdded": "2026-11-01T09:00:00Z",
        "dateUpdated": updated,
    }
    payload.update(extra)
    return payload


class FakeSource:
    """Stands in for ``OpportunitiesResource``, failure modes included."""

    def __init__(
        self, items: list[dict[str, object]], *, complete: bool = True, pages: int = 1
    ) -> None:
        self.items = items
        self.complete = complete
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> Collection[dict[str, object]]:
        self.calls.append(kwargs)
        return Collection(
            items=list(self.items),
            pages_fetched=self.pages,
            complete=self.complete,
            truncated_reason=None if self.complete else "page_failure",
        )


def extractor(
    items: list[dict[str, object]],
    *,
    sink: InMemoryOpportunityWarehouse | None = None,
    watermarks: InMemoryWatermarkStore | None = None,
    complete: bool = True,
) -> tuple[IncrementalOpportunityExtractor, InMemoryOpportunityWarehouse, InMemoryWatermarkStore]:
    warehouse = sink or InMemoryOpportunityWarehouse()
    marks = watermarks or InMemoryWatermarkStore()
    return (
        IncrementalOpportunityExtractor(FakeSource(items, complete=complete), warehouse, marks),
        warehouse,
        marks,
    )


# -- normalisation -----------------------------------------------------------


def test_payload_maps_onto_the_dimension():
    record = normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", value=2500))

    assert record.opportunity_id == "o1"
    assert record.status == "open"
    assert record.is_open == 1
    assert record.monetary_value == 2500.0
    assert record.contact_key is not None
    assert record.key == opportunity_key("loc-1", "o1")


def test_unknown_status_is_rejected_rather_than_bucketed():
    with pytest.raises(InvalidRecordError, match="unknown status"):
        normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", status="negotiating"))


def test_status_is_normalised_to_lowercase():
    record = normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", status="WON"))

    assert record.status == "won"
    assert record.is_open == 0


def test_payload_without_id_is_rejected():
    with pytest.raises(InvalidRecordError, match="without an id"):
        normalise_opportunity({"dateUpdated": "2026-11-20T10:00:00Z"})


def test_payload_without_update_timestamp_is_rejected():
    with pytest.raises(InvalidRecordError, match="no dateUpdated"):
        normalise_opportunity({"id": "o1", "locationId": "loc-1"})


def test_non_numeric_value_is_rejected_instead_of_read_as_zero():
    with pytest.raises(InvalidRecordError, match="not a number"):
        normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", value="about 1k"))


def test_numeric_value_arriving_as_text_is_parsed():
    record = normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", value="1,250.50"))

    assert record.monetary_value == 1250.5


def test_absent_value_is_zero_and_not_an_error():
    record = normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", value=None))

    assert record.monetary_value == 0.0


# -- row hash ----------------------------------------------------------------


def test_hash_ignores_the_provider_timestamp():
    first = opportunity_row_hash(normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z")))
    second = opportunity_row_hash(normalise_opportunity(raw_opportunity("o1", "2026-11-20T15:00:00Z")))

    assert first == second


def test_hash_changes_when_the_stage_moves():
    first = opportunity_row_hash(normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z")))
    moved = opportunity_row_hash(
        normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", stage="stage-won"))
    )

    assert first != moved


def test_hash_changes_when_the_value_changes():
    first = opportunity_row_hash(normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z")))
    repriced = opportunity_row_hash(
        normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z", value=9000))
    )

    assert first != repriced


# -- sink --------------------------------------------------------------------


def test_first_load_creates_version_one():
    warehouse = InMemoryOpportunityWarehouse()

    result = warehouse.load_opportunity(
        normalise_opportunity(raw_opportunity("o1", "2026-11-20T10:00:00Z")),
        observed_at=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert result.version == 1
    assert result.status == "new_version"
    assert warehouse.opportunity_count() == 1


def test_replay_of_the_same_payload_is_unchanged():
    extractor_a, warehouse, marks = extractor([raw_opportunity("o1", "2026-11-20T10:00:00Z")])
    extractor_a.run()

    extractor_b, _, _ = extractor(
        [raw_opportunity("o1", "2026-11-20T10:00:00Z")], sink=warehouse, watermarks=marks
    )
    stats = extractor_b.run()

    assert stats.new_versions == 0
    assert len(warehouse.versions(opportunity_key("loc-1", "o1"))) == 1


def test_stage_move_closes_the_previous_version():
    first, warehouse, marks = extractor([raw_opportunity("o1", "2026-11-20T10:00:00Z")])
    first.run()

    second, _, _ = extractor(
        [raw_opportunity("o1", "2026-11-22T09:00:00Z", stage="stage-won", status="won")],
        sink=warehouse,
        watermarks=marks,
    )
    stats = second.run()

    versions = warehouse.versions(opportunity_key("loc-1", "o1"))
    assert stats.new_versions == 1
    assert len(versions) == 2
    assert versions[0]["is_current"] == 0
    assert versions[0]["valid_to"] == "2026-11-22T09:00:00Z"
    assert versions[1]["is_current"] == 1
    assert versions[1]["status"] == "won"


def test_older_payload_does_not_overwrite_a_newer_one():
    first, warehouse, marks = extractor([raw_opportunity("o1", "2026-11-22T09:00:00Z")])
    first.run()

    second, _, _ = extractor(
        [raw_opportunity("o1", "2026-11-01T09:00:00Z", stage="stage-old")],
        sink=warehouse,
        watermarks=marks,
    )
    stats = second.run()

    assert stats.new_versions == 0
    assert stats.outside_window == 1
    assert stats.watermark_moved is False
    assert len(warehouse.versions(opportunity_key("loc-1", "o1"))) == 1
    current = warehouse.current_version(opportunity_key("loc-1", "o1"))
    assert current is not None and current["stage_key"] != opportunity_key("pipe-1", "stage-old")


def test_open_value_counts_only_current_open_deals():
    warehouse = InMemoryOpportunityWarehouse()
    extractor_a, _, _ = extractor(
        [
            raw_opportunity("o1", "2026-11-20T10:00:00Z", value=1000),
            raw_opportunity("o2", "2026-11-20T10:00:00Z", value=2000, status="won"),
            raw_opportunity("o3", "2026-11-20T10:00:00Z", value=500),
        ],
        sink=warehouse,
    )
    extractor_a.run()

    assert warehouse.open_value() == 1500.0


def test_pipeline_stage_dimension_is_shared_between_deals_in_one_stage():
    extractor_a, warehouse, _ = extractor(
        [
            raw_opportunity("o1", "2026-11-20T10:00:00Z", stage="stage-new"),
            raw_opportunity("o2", "2026-11-20T10:00:00Z", stage="stage-new"),
        ]
    )
    extractor_a.run()

    assert len(warehouse.dim_pipeline_stage) == 1


# -- extractor ---------------------------------------------------------------


def test_full_run_loads_every_opportunity():
    extractor_a, warehouse, marks = extractor(
        [
            raw_opportunity("o1", "2026-11-20T10:00:00Z"),
            raw_opportunity("o2", "2026-11-20T11:00:00Z"),
        ]
    )

    stats = extractor_a.run()

    assert stats.new_versions == 2
    assert warehouse.opportunity_count() == 2
    assert stats.watermark_moved is True
    assert marks.get("opportunities") is not None


def test_partial_collection_raises_and_leaves_the_mark_untouched():
    extractor_a, warehouse, marks = extractor(
        [raw_opportunity("o1", "2026-11-20T10:00:00Z")], complete=False
    )

    with pytest.raises(IncompleteExtractError):
        extractor_a.run()

    assert marks.get("opportunities") is None
    assert warehouse.opportunity_count() == 0


def test_dry_run_loads_nothing_and_does_not_move_the_mark():
    extractor_a, warehouse, marks = extractor([raw_opportunity("o1", "2026-11-20T10:00:00Z")])

    stats = extractor_a.run(dry_run=True)

    assert stats.dry_run is True
    assert stats.new_versions == 1
    assert warehouse.loads == 0
    assert marks.get("opportunities") is None


def test_invalid_rows_are_counted_and_the_rest_still_load():
    extractor_a, warehouse, _ = extractor(
        [
            raw_opportunity("o1", "2026-11-20T10:00:00Z"),
            raw_opportunity("o2", "2026-11-20T10:00:00Z", status="unknown-status"),
        ]
    )

    stats = extractor_a.run()

    assert stats.scanned == 2
    assert stats.rejected == 1
    assert stats.new_versions == 1
    assert warehouse.opportunity_count() == 1
    assert stats.rejections


def test_replay_never_moves_the_watermark_backwards():
    extractor_a, warehouse, marks = extractor([raw_opportunity("o1", "2026-11-22T09:00:00Z")])
    extractor_a.run()
    before = marks.get("opportunities").value

    extractor_b, _, _ = extractor(
        [raw_opportunity("o1", "2026-11-20T10:00:00Z")], sink=warehouse, watermarks=marks
    )
    stats = extractor_b.run()

    assert stats.watermark_moved is False
    assert stats.watermark_after == before


def test_stats_carry_the_entity_name_so_two_grains_do_not_collide():
    extractor_a, _, _ = extractor([raw_opportunity("o1", "2026-11-20T10:00:00Z")])

    assert extractor_a.run().entity == "opportunities"


# -- end to end over HTTP ----------------------------------------------------


def client_for(base_url: str) -> GhlClient:
    return GhlClient(
        PrivateIntegrationToken(token=DEFAULT_TOKEN),
        base_url=base_url,
        transport=UrllibTransport(),
        limiter=SlidingWindowLimiter(capacity=10_000, window=1.0),
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=False),
        location_id="loc_test",
    )


def test_warehouse_reconciles_opportunities_fetched_over_http(mock_server):
    _state, base_url = mock_server
    client = client_for(base_url)
    seeded = [
        raw_opportunity("opp_a", "2026-11-20T10:00:00Z", value=3200, locationId="loc_test"),
        raw_opportunity("opp_b", "2026-11-20T11:00:00Z", value=800, locationId="loc_test"),
        raw_opportunity("opp_c", "2026-11-21T11:00:00Z", value=4500, status="lost", locationId="loc_test"),
    ]
    client.post("/__admin/opportunities", json_body={"opportunities": seeded})

    resource = OpportunitiesResource(client, page_size=2)
    warehouse = InMemoryOpportunityWarehouse()
    marks = InMemoryWatermarkStore()
    stats = IncrementalOpportunityExtractor(resource, warehouse, marks).run()

    assert stats.new_versions == 3
    assert warehouse.opportunity_count() == 3
    assert warehouse.open_value() == 4000.0
    assert marks.get("opportunities") is not None


def test_http_replay_adds_no_versions(mock_server):
    _state, base_url = mock_server
    client = client_for(base_url)
    client.post(
        "/__admin/opportunities",
        json_body={
            "opportunities": [
                raw_opportunity("opp_a", "2026-11-20T10:00:00Z", locationId="loc_test")
            ]
        },
    )
    resource = OpportunitiesResource(client, page_size=10)
    warehouse = InMemoryOpportunityWarehouse()
    marks = InMemoryWatermarkStore()

    IncrementalOpportunityExtractor(resource, warehouse, marks).run()
    second = IncrementalOpportunityExtractor(resource, warehouse, marks).run()

    assert second.new_versions == 0
    assert len(warehouse.versions(opportunity_key("loc_test", "opp_a"))) == 1


def test_mock_sink_and_module_sink_are_the_same_object():
    """Guards against the export drifting from the implementation."""

    assert InMemoryOpportunityWarehouse is WarehouseFromModule
