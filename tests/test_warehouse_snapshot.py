"""Snapshot tests (GHL-028).

Two properties matter here and nothing else does:

* the same data must produce the same file, byte for byte, or the digest is
  decoration;
* a rewrite that keeps the row count must still show up as a change, because a
  diff that only counts rows would call a corrected value "unchanged".
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ghl_warehouse.opportunities import normalise_opportunity
from ghl_warehouse.sink import normalise_contact
from ghl_warehouse.snapshot import (
    SNAPSHOT_FORMAT,
    SnapshotError,
    build_snapshot,
    diff_snapshots,
    main,
    read_snapshot,
    write_snapshot,
)
from ghl_warehouse.sqlite import SqliteWarehouse

OBSERVED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)


def _contact(
    contact_id: str = "c1",
    *,
    updated: str = "2026-01-05T00:00:00Z",
    email: str = "a@example.com",
    tags: tuple[str, ...] = ("alpha", "beta"),
):
    return normalise_contact(
        {
            "id": contact_id,
            "locationId": "loc_test",
            "dateAdded": "2026-01-01T00:00:00Z",
            "dateUpdated": updated,
            "email": email,
            "firstName": "Ada",
            "tags": list(tags),
        }
    )


def _opportunity(
    opportunity_id: str = "o1",
    *,
    updated: str = "2026-01-06T00:00:00Z",
    value: str = "1250.50",
):
    return normalise_opportunity(
        {
            "id": opportunity_id,
            "locationId": "loc_test",
            "dateAdded": "2026-01-02T00:00:00Z",
            "dateUpdated": updated,
            "name": "Roof replacement",
            "status": "open",
            "monetaryValue": value,
            "pipelineId": "p1",
            "pipelineStageId": "s1",
            "contactId": "c1",
        }
    )


@pytest.fixture()
def scratch() -> Path:
    """Own temporary directory; this machine's pytest cannot clean tmp_path."""

    holder = tempfile.TemporaryDirectory(prefix="ghl-snapshot-")
    try:
        yield Path(holder.name)
    finally:
        holder.cleanup()


def _seeded(path: Path) -> SqliteWarehouse:
    warehouse = SqliteWarehouse(path)
    warehouse.load_contact(_contact(), observed_at=OBSERVED_AT)
    warehouse.load_opportunity(_opportunity(), observed_at=OBSERVED_AT)
    return warehouse


# -- file shape ---------------------------------------------------------


def test_snapshot_carries_format_and_schema_versions(scratch: Path) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    try:
        snapshot = build_snapshot(warehouse)
        manifest = warehouse.manifest()
        schema_version = warehouse.schema.version
    finally:
        warehouse.close()

    assert snapshot["snapshot_format"] == SNAPSHOT_FORMAT
    assert snapshot["schema_version"] == schema_version
    assert snapshot["digest"] == manifest["digest"]


def test_written_file_round_trips_through_read_snapshot(scratch: Path) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    target = scratch / "snap" / "one.json"
    try:
        written = write_snapshot(warehouse, target)
    finally:
        warehouse.close()

    assert target.exists()
    assert not target.with_name(target.name + ".tmp").exists()
    loaded = read_snapshot(target)
    assert loaded == written


def test_same_data_produces_byte_identical_files(scratch: Path) -> None:
    left = _seeded(scratch / "left.sqlite")
    right = _seeded(scratch / "right.sqlite")
    try:
        write_snapshot(left, scratch / "left.json")
        write_snapshot(right, scratch / "right.json")
    finally:
        left.close()
        right.close()

    assert (scratch / "left.json").read_bytes() == (scratch / "right.json").read_bytes()


def test_snapshot_file_has_no_wall_clock(scratch: Path) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    try:
        write_snapshot(warehouse, scratch / "one.json")
    finally:
        warehouse.close()

    payload = json.loads((scratch / "one.json").read_text(encoding="utf-8"))
    assert set(payload) == {"snapshot_format", "schema_version", "tables", "digest"}


# -- reader validation --------------------------------------------------


def test_missing_file_is_a_snapshot_error(scratch: Path) -> None:
    with pytest.raises(SnapshotError, match="not found"):
        read_snapshot(scratch / "absent.json")


def test_invalid_json_is_a_snapshot_error(scratch: Path) -> None:
    target = scratch / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotError, match="not valid JSON"):
        read_snapshot(target)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([], "root must be an object"),
        ({"snapshot_format": "1"}, "integer snapshot_format"),
        ({"snapshot_format": SNAPSHOT_FORMAT + 1}, "newer than this reader"),
        (
            {"snapshot_format": SNAPSHOT_FORMAT, "schema_version": "3", "digest": "d"},
            "no tables",
        ),
        (
            {
                "snapshot_format": SNAPSHOT_FORMAT,
                "schema_version": 3,
                "digest": "d",
                "tables": {"t": {"rows": 1, "sha256": "x"}},
            },
            "no schema_version",
        ),
        (
            {
                "snapshot_format": SNAPSHOT_FORMAT,
                "schema_version": "3",
                "digest": "d",
                "tables": {"t": {"rows": 1}},
            },
            "missing rows or sha256",
        ),
    ],
)
def test_malformed_snapshots_are_rejected(
    scratch: Path, payload, expected: str
) -> None:
    target = scratch / "bad.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match=expected):
        read_snapshot(target)


def test_newer_format_is_refused_rather_than_misread(scratch: Path) -> None:
    target = scratch / "future.json"
    target.write_text(
        json.dumps({"snapshot_format": SNAPSHOT_FORMAT + 1, "schema_version": "3"}),
        encoding="utf-8",
    )
    with pytest.raises(SnapshotError):
        read_snapshot(target)


# -- diff ---------------------------------------------------------------


def test_identical_snapshots_report_no_change(scratch: Path) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    try:
        one = build_snapshot(warehouse)
        two = build_snapshot(warehouse)
    finally:
        warehouse.close()

    diff = diff_snapshots(one, two)
    assert diff["identical"] is True
    assert diff["schema_version_changed"] is False
    assert {t["status"] for t in diff["tables"].values()} == {"unchanged"}


def test_new_contact_version_shows_as_changed_table(scratch: Path) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    try:
        before = build_snapshot(warehouse)
        warehouse.load_contact(
            _contact(updated="2026-02-01T00:00:00Z", email="b@example.com"),
            observed_at=OBSERVED_AT,
        )
        after = build_snapshot(warehouse)
    finally:
        warehouse.close()

    diff = diff_snapshots(before, after)
    assert diff["identical"] is False
    # dim_contact carries the current email, so the dimension moves with it
    assert diff["tables"]["dim_contact"]["status"] == "changed"
    assert diff["tables"]["fact_contact_version"]["status"] == "changed"
    assert diff["tables"]["fact_contact_version"]["rows_before"] == 1
    assert diff["tables"]["fact_contact_version"]["rows_after"] == 2
    assert diff["tables"]["dim_opportunity"]["status"] == "unchanged"


def test_value_rewrite_is_changed_even_when_row_count_holds(scratch: Path) -> None:
    left = _seeded(scratch / "left.sqlite")
    right = _seeded(scratch / "right.sqlite")
    try:
        before = build_snapshot(left)
        right.load_opportunity(
            _opportunity(updated="2026-02-01T00:00:00Z", value="999.99"),
            observed_at=OBSERVED_AT,
        )
        after = build_snapshot(right)
    finally:
        left.close()
        right.close()

    diff = diff_snapshots(before, after)
    fact = diff["tables"]["fact_opportunity_version"]
    assert fact["status"] == "changed"
    assert fact["rows_before"] == 1
    assert fact["rows_after"] == 2


def test_tables_missing_on_either_side_are_added_or_removed() -> None:
    before = {
        "schema_version": "3",
        "digest": "a",
        "tables": {"gone": {"rows": 2, "sha256": "x"}},
    }
    after = {
        "schema_version": "3",
        "digest": "b",
        "tables": {"fresh": {"rows": 5, "sha256": "y"}},
    }

    diff = diff_snapshots(before, after)
    assert diff["tables"]["gone"]["status"] == "removed"
    assert diff["tables"]["gone"]["rows_after"] is None
    assert diff["tables"]["fresh"]["status"] == "added"
    assert diff["tables"]["fresh"]["rows_before"] is None
    assert diff["identical"] is False


def test_schema_bump_alone_counts_as_a_difference() -> None:
    before = {
        "schema_version": "2",
        "digest": "a",
        "tables": {"t": {"rows": 1, "sha256": "x"}},
    }
    after = {
        "schema_version": "3",
        "digest": "b",
        "tables": {"t": {"rows": 1, "sha256": "x"}},
    }

    diff = diff_snapshots(before, after)
    assert diff["schema_version_changed"] is True
    assert diff["identical"] is False
    assert diff["tables"]["t"]["status"] == "unchanged"


# -- cli ----------------------------------------------------------------


def test_cli_write_then_diff_reports_identical(
    scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warehouse = _seeded(scratch / "w.sqlite")
    warehouse.close()
    first = scratch / "first.json"
    second = scratch / "second.json"

    assert main(["write", "--db", str(scratch / "w.sqlite"), "--out", str(first)]) == 0
    assert main(["write", "--db", str(scratch / "w.sqlite"), "--out", str(second)]) == 0
    capsys.readouterr()

    assert main(["diff", str(first), str(second)]) == 0
    assert "unchanged" in capsys.readouterr().out


def test_cli_diff_exits_one_when_snapshots_differ(
    scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch_db = scratch / "w.sqlite"
    warehouse = _seeded(scratch_db)
    before = scratch / "before.json"
    write_snapshot(warehouse, before)
    warehouse.load_contact(
        _contact(updated="2026-03-01T00:00:00Z", email="c@example.com"),
        observed_at=OBSERVED_AT,
    )
    warehouse.close()
    after = scratch / "after.json"

    assert main(["write", "--db", str(scratch_db), "--out", str(after)]) == 0
    capsys.readouterr()

    assert main(["diff", str(before), str(after)]) == 1
    out = capsys.readouterr().out
    assert "changed" in out
    assert "fact_contact_version" in out


def test_cli_write_on_missing_warehouse_exits_two(
    scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "write",
            "--db",
            str(scratch / "absent.sqlite"),
            "--out",
            str(scratch / "x.json"),
        ]
    )
    assert code == 2
    assert "warehouse not found" in capsys.readouterr().err


def test_cli_diff_on_unreadable_snapshot_exits_two(
    scratch: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = scratch / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert main(["diff", str(broken), str(broken)]) == 2
    assert "root must be an object" in capsys.readouterr().err
