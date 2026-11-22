"""Offline snapshot demo: two identical loads, byte-identical files, then one change.

Run: python examples/demo_snapshot.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ghl_warehouse.opportunities import normalise_opportunity
from ghl_warehouse.sink import normalise_contact
from ghl_warehouse.snapshot import (
    build_snapshot,
    diff_snapshots,
    read_snapshot,
    write_snapshot,
)
from ghl_warehouse.sqlite import SqliteWarehouse

OBSERVED_AT = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)


def contact(email: str, updated: str = "2026-01-05T00:00:00Z"):
    return normalise_contact(
        {
            "id": "c1",
            "locationId": "loc_demo",
            "dateAdded": "2026-01-01T00:00:00Z",
            "dateUpdated": updated,
            "email": email,
            "firstName": "Ada",
            "tags": ["alpha", "beta"],
        }
    )


def opportunity(value: str = "1250.50"):
    return normalise_opportunity(
        {
            "id": "o1",
            "locationId": "loc_demo",
            "dateAdded": "2026-01-02T00:00:00Z",
            "dateUpdated": "2026-01-06T00:00:00Z",
            "name": "Roof replacement",
            "status": "open",
            "monetaryValue": value,
            "pipelineId": "p1",
            "pipelineStageId": "s1",
            "contactId": "c1",
        }
    )


def seed(path: Path) -> SqliteWarehouse:
    warehouse = SqliteWarehouse(path)
    warehouse.load_contact(contact("ada@example.com"), observed_at=OBSERVED_AT)
    warehouse.load_opportunity(opportunity(), observed_at=OBSERVED_AT)
    return warehouse


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ghl-snapshot-demo-") as folder:
        base = Path(folder)
        left = seed(base / "left.sqlite")
        right = seed(base / "right.sqlite")
        try:
            left_file = write_snapshot(left, base / "left.json")
            write_snapshot(right, base / "right.json")
        finally:
            left.close()
            right.close()

        identical = (base / "left.json").read_bytes() == (
            base / "right.json"
        ).read_bytes()
        print(f"same payloads -> same file: {identical}")
        print(
            f"schema v{left_file['schema_version']}, digest {left_file['digest'][:12]}"
        )

        changed = seed(base / "changed.sqlite")
        try:
            changed.load_contact(
                contact("ada+new@example.com", updated="2026-02-01T00:00:00Z"),
                observed_at=OBSERVED_AT,
            )
            after = build_snapshot(changed)
        finally:
            changed.close()

        diff = diff_snapshots(read_snapshot(base / "left.json"), after)
        print(f"after one contact change, identical: {diff['identical']}")
        for name, table in diff["tables"].items():
            if table["status"] != "unchanged":
                print(
                    f"  {table['status']:<8} {name}: "
                    f"{table['rows_before']} -> {table['rows_after']} rows"
                )

        if not identical:
            print("FAIL: identical payloads produced different snapshot files")
            return 1
        if diff["identical"]:
            print("FAIL: a contact change did not appear in the diff")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
