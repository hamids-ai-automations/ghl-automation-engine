"""Durable sink tests (GHL-027).

The point of these tests is not that SQLite stores rows — it does. The point is
that the file behaves like the reference sink: the same sequence of payloads must
produce the same versions, the same statuses and the same current row. Where the
in-memory sink and the SQLite sink can disagree, one of them is wrong about the
business rule, and the equivalence tests below are what surfaces it.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ghl_warehouse.extractor import IncrementalContactExtractor
from ghl_warehouse.opportunities import (
    IncrementalOpportunityExtractor,
    InMemoryOpportunityWarehouse,
    normalise_opportunity,
)
from ghl_warehouse.sink import InMemoryWarehouse, normalise_contact
from ghl_warehouse.sqlite import SqliteWarehouse
from ghl_warehouse.watermark import InMemoryWatermarkStore

OBSERVED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)


def _contact(
    contact_id: str = "c1",
    *,
    updated: str = "2026-01-05T00:00:00Z",
    email: str = "a@example.com",
    tags: tuple[str, ...] = ("beta", "alpha"),
    stage_id: str | None = None,
    pipeline_id: str | None = None,
):
    payload = {
        "id": contact_id,
        "locationId": "loc_test",
        "dateAdded": "2026-01-01T00:00:00Z",
        "dateUpdated": updated,
        "email": email,
        "firstName": "Ada",
        "tags": list(tags),
    }
    if stage_id:
        payload["stageId"] = stage_id
    if pipeline_id:
        payload["pipelineId"] = pipeline_id
    return normalise_contact(payload)


def _opportunity(
    opportunity_id: str = "o1",
    *,
    updated: str = "2026-01-06T00:00:00Z",
    value: str = "1250.50",
    status: str = "open",
    stage: str = "s1",
):
    return normalise_opportunity(
        {
            "id": opportunity_id,
            "locationId": "loc_test",
            "dateAdded": "2026-01-02T00:00:00Z",
            "dateUpdated": updated,
            "name": "Roof replacement",
            "status": status,
            "monetaryValue": value,
            "pipelineId": "p1",
            "pipelineStageId": stage,
            "contactId": "c1",
        }
    )


@pytest.fixture()
def scratch() -> Path:
    """Own temporary directory instead of ``tmp_path``.

    This machine's pytest cannot clean its own ``pytest-current`` symlink up
    (PermissionError at session finish), so these tests manage a directory they
    can remove themselves.
    """

    holder = tempfile.TemporaryDirectory(prefix="ghl-warehouse-")
    try:
        yield Path(holder.name)
    finally:
        holder.cleanup()


@pytest.fixture()
def warehouse(scratch: Path) -> SqliteWarehouse:
    store = SqliteWarehouse(scratch / "warehouse.sqlite")
    yield store
    store.close()


# -- schema -------------------------------------------------------------


def test_every_declared_table_is_created(warehouse: SqliteWarehouse) -> None:
    names = {
        row[0]
        for row in warehouse._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table in warehouse.schema.tables:
        assert table.name in names


def test_rows_carry_no_undeclared_columns(warehouse: SqliteWarehouse) -> None:
    """Inline warehouses build dicts by hand; this catches DDL/dict drift."""

    with pytest.raises(ValueError, match="undeclared columns"):
        warehouse._insert("dim_contact", {"contact_key": "k", "not_a_column": 1})


def test_inline_sink_rows_fit_the_declared_schema() -> None:
    """Both reference sinks must produce rows the SQL schema can hold."""

    from ghl_warehouse.schema import DIM_CONTACT, DIM_OPPORTUNITY

    memory = InMemoryWarehouse()
    memory.load_contact(_contact(), observed_at=OBSERVED_AT)
    row = next(iter(memory.dim_contact.values()))
    assert set(row) <= set(DIM_CONTACT.names)

    opportunities = InMemoryOpportunityWarehouse()
    opportunities.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)
    opp_row = next(iter(opportunities.dim_opportunity.values()))
    assert set(opp_row) <= set(DIM_OPPORTUNITY.names)


# -- contact load -------------------------------------------------------


def test_first_load_writes_dimension_and_fact(warehouse: SqliteWarehouse) -> None:
    result = warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)

    assert result.status == "new_version"
    assert result.version == 1
    assert warehouse.table_counts()["dim_contact"] == 1
    assert warehouse.table_counts()["fact_contact_version"] == 1
    fact = warehouse._conn.execute("SELECT * FROM fact_contact_version").fetchone()
    assert fact["observed_date_key"] == 20261120
    assert fact["days_since_created"] == 4


def test_replay_does_not_create_a_version(warehouse: SqliteWarehouse) -> None:
    record = _contact()
    warehouse.load_contact(record, observed_at=OBSERVED_AT)
    second = warehouse.load_contact(record, observed_at=OBSERVED_AT)

    assert second.status == "unchanged"
    assert second.version == 1
    assert warehouse.table_counts()["dim_contact"] == 1


def test_changed_attribute_opens_version_two(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    result = warehouse.load_contact(
        _contact(updated="2026-02-01T00:00:00Z", email="moved@example.com"),
        observed_at=OBSERVED_AT,
    )

    assert result.status == "new_version"
    assert result.version == 2
    assert result.attributes_changed == 1
    versions = warehouse.versions("dim_contact", _contact().key)
    assert [row["is_current"] for row in versions] == [0, 1]
    assert versions[0]["valid_to"] == "2026-02-01T00:00:00Z"
    assert versions[1]["valid_from"] == "2026-02-01T00:00:00Z"


def test_older_payload_is_ignored(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(
        _contact(updated="2026-03-01T00:00:00Z", email="b@x.co"),
        observed_at=OBSERVED_AT,
    )
    result = warehouse.load_contact(
        _contact(updated="2026-02-01T00:00:00Z", email="a@x.co"),
        observed_at=OBSERVED_AT,
    )

    assert result.status == "stale_ignored"
    assert result.version == 1
    assert warehouse.current_version("dim_contact", _contact().key)["email"] == "b@x.co"


def test_tag_and_stage_change_counts_as_a_version(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    result = warehouse.load_contact(
        _contact(
            updated="2026-02-01T00:00:00Z",
            tags=("alpha", "beta", "gamma"),
            stage_id="s2",
            pipeline_id="p1",
        ),
        observed_at=OBSERVED_AT,
    )

    assert result.version == 2
    # ``stage_id`` is tracked for opportunities but not for contacts, so only the
    # tag set counts as a changed attribute here. Asserting the number keeps the
    # difference visible instead of letting it drift unnoticed.
    assert result.attributes_changed == 1


def test_dim_date_spine_is_populated(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    keys = {row[0] for row in warehouse._conn.execute("SELECT date_key FROM dim_date")}
    assert keys == {20260101, 20251120} or keys == {20260101, 20261120}
    row = warehouse._conn.execute(
        "SELECT * FROM dim_date WHERE date_key = 20260101"
    ).fetchone()
    assert (row["year"], row["month"], row["day"]) == (2026, 1, 1)


# -- opportunity load ---------------------------------------------------


def test_opportunity_load_and_open_value(warehouse: SqliteWarehouse) -> None:
    result = warehouse.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)

    assert result.status == "new_version"
    assert warehouse.opportunity_count() == 1
    assert warehouse.open_value() == pytest.approx(1250.50)
    stage = warehouse._conn.execute("SELECT * FROM dim_pipeline_stage").fetchone()
    assert stage["pipeline_id"] == "p1"


def test_closed_opportunity_leaves_open_value(warehouse: SqliteWarehouse) -> None:
    warehouse.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)
    warehouse.load_opportunity(
        _opportunity(updated="2026-02-01T00:00:00Z", status="won"),
        observed_at=OBSERVED_AT,
    )

    assert warehouse.open_value() == 0.0
    versions = warehouse.versions("dim_opportunity", _opportunity().key)
    assert [row["status"] for row in versions] == ["open", "won"]
    fact = warehouse._conn.execute(
        "SELECT is_open FROM fact_opportunity_version ORDER BY version"
    ).fetchall()
    assert [row[0] for row in fact] == [1, 0]


def test_stage_move_creates_a_version(warehouse: SqliteWarehouse) -> None:
    warehouse.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)
    result = warehouse.load_opportunity(
        _opportunity(updated="2026-02-01T00:00:00Z", stage="s2"),
        observed_at=OBSERVED_AT,
    )

    assert result.version == 2
    assert (
        warehouse._conn.execute("SELECT COUNT(*) FROM dim_pipeline_stage").fetchone()[0]
        == 2
    )


# -- parity with the reference sink ------------------------------------


def test_contact_sequence_matches_inline_sink(warehouse: SqliteWarehouse) -> None:
    memory = InMemoryWarehouse()
    sequence = [
        _contact(),
        _contact(),
        _contact(updated="2026-02-01T00:00:00Z", email="two@example.com"),
        _contact(updated="2026-01-15T00:00:00Z", email="stale@example.com"),
        _contact(updated="2026-03-01T00:00:00Z", tags=("alpha",)),
    ]

    for record in sequence:
        sql_result = warehouse.load_contact(record, observed_at=OBSERVED_AT)
        mem_result = memory.load_contact(record, observed_at=OBSERVED_AT)
        assert (sql_result.status, sql_result.version) == (
            mem_result.status,
            mem_result.version,
        )
        assert sql_result.attributes_changed == mem_result.attributes_changed

    assert warehouse.contact_count() == memory.contact_count()
    key = sequence[0].key
    sql_rows = warehouse.versions("dim_contact", key)
    mem_rows = memory.versions(key)
    for sql_row, mem_row in zip(sql_rows, mem_rows, strict=True):
        for column in (
            "version",
            "row_hash",
            "email",
            "is_current",
            "valid_to",
            "tag_key",
        ):
            assert sql_row[column] == mem_row[column]


def test_opportunity_sequence_matches_inline_sink(warehouse: SqliteWarehouse) -> None:
    memory = InMemoryOpportunityWarehouse()
    sequence = [
        _opportunity(),
        _opportunity(),
        _opportunity(updated="2026-02-01T00:00:00Z", status="won"),
        _opportunity(updated="2026-01-20T00:00:00Z", value="9.00"),
    ]

    for record in sequence:
        sql_result = warehouse.load_opportunity(record, observed_at=OBSERVED_AT)
        mem_result = memory.load_opportunity(record, observed_at=OBSERVED_AT)
        assert (sql_result.status, sql_result.version) == (
            mem_result.status,
            mem_result.version,
        )

    assert warehouse.open_value() == memory.open_value()


# -- durability and reproducibility ------------------------------------


def test_data_survives_reopen(scratch: Path) -> None:
    path = scratch / "warehouse.sqlite"
    first = SqliteWarehouse(path)
    first.load_contact(_contact(), observed_at=OBSERVED_AT)
    manifest = first.manifest()
    first.close()

    second = SqliteWarehouse(path)
    try:
        assert second.contact_count() == 1
        assert second.manifest() == manifest
        replay = second.load_contact(_contact(), observed_at=OBSERVED_AT)
        assert replay.status == "unchanged"
    finally:
        second.close()


def test_manifest_is_reproducible_for_identical_data(scratch: Path) -> None:
    left = SqliteWarehouse(scratch / "left.sqlite")
    right = SqliteWarehouse(scratch / "right.sqlite")
    try:
        for store in (left, right):
            store.load_contact(_contact(), observed_at=OBSERVED_AT)
            store.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)
        assert left.manifest() == right.manifest()
    finally:
        left.close()
        right.close()


def test_manifest_changes_when_data_changes(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    before = warehouse.manifest()["digest"]
    warehouse.load_contact(
        _contact(updated="2026-02-01T00:00:00Z", email="x@example.com"),
        observed_at=OBSERVED_AT,
    )
    after = warehouse.manifest()

    assert after["digest"] != before
    assert after["tables"]["dim_contact"]["rows"] == 2
    json.dumps(after)  # manifest must be JSON-serialisable for a snapshot manifest


def test_two_current_rows_are_impossible(warehouse: SqliteWarehouse) -> None:
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    with pytest.raises(sqlite3.IntegrityError):
        warehouse._conn.execute(
            "INSERT INTO dim_contact (contact_key, contact_id, version, row_hash, "
            "created_date_key, source_updated_at, valid_from, is_current) "
            "VALUES (?, 'c1', 9, 'x', 20260101, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', 1)",
            (_contact().key,),
        )


# -- end to end through the extractors ---------------------------------


@dataclass
class _Page:
    """Minimal complete page, matching the ``Collection`` protocol the extractors read."""

    items: list[dict]
    pages_fetched: int = 1
    complete: bool = True
    truncated_reason: str | None = None

    def __len__(self) -> int:
        return len(self.items)


class _Source:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls = 0

    def search(self, **_: object) -> _Page:
        self.calls += 1
        return _Page(items=list(self.rows))


def test_contact_extractor_loads_into_sqlite(warehouse: SqliteWarehouse) -> None:
    rows = [
        {"id": "c1", "locationId": "loc_test", "dateUpdated": "2026-01-05T00:00:00Z"},
        {"id": "c2", "locationId": "loc_test", "dateUpdated": "2026-01-06T00:00:00Z"},
        {"id": "c3", "locationId": "loc_test"},
    ]
    extractor = IncrementalContactExtractor(
        _Source(rows), warehouse, InMemoryWatermarkStore()
    )

    result = extractor.run()

    assert result.accepted == 2
    assert result.rejected == 1
    assert result.pages == 1
    assert warehouse.contact_count() == 2


def test_opportunity_extractor_loads_into_sqlite(warehouse: SqliteWarehouse) -> None:
    rows = [
        {
            "id": "o1",
            "locationId": "loc_test",
            "dateUpdated": "2026-01-06T00:00:00Z",
            "monetaryValue": "1,250.50",
            "pipelineId": "p1",
            "pipelineStageId": "s1",
        },
        {
            "id": "o2",
            "locationId": "loc_test",
            "dateUpdated": "2026-01-07T00:00:00Z",
            "monetaryValue": 400,
            "status": "won",
        },
        {
            "id": "o3",
            "locationId": "loc_test",
            "dateUpdated": "2026-01-08T00:00:00Z",
            "status": "partially_won",
        },
    ]
    extractor = IncrementalOpportunityExtractor(
        _Source(rows), warehouse, InMemoryWatermarkStore()
    )

    result = extractor.run()

    assert result.accepted == 2
    assert result.rejected == 1
    assert warehouse.open_value() == pytest.approx(1250.50)
