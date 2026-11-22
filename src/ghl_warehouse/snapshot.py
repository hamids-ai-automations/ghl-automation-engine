"""Snapshot files: the warehouse manifest written to disk, and the diff between two of them (GHL-028).

Three decisions worth stating:

* A snapshot holds no wall clock. Two warehouses loaded from the same payload
  sequence must produce byte-identical files, otherwise the digest proves nothing
  and every diff is noise.
* ``diff`` is the only reader of the digest. Nothing here decides whether a
  difference is acceptable: the CLI exits 1 when snapshots differ, and the caller
  decides what that means.
* The file format carries its own version, separate from the star schema version.
  A schema bump is a fact about the data; a format bump is a fact about this file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .sqlite import SqliteWarehouse

__all__ = [
    "SNAPSHOT_FORMAT",
    "SnapshotError",
    "build_snapshot",
    "diff_snapshots",
    "read_snapshot",
    "write_snapshot",
]

SNAPSHOT_FORMAT = 1

PATH_TYPE = str | Path


class SnapshotError(ValueError):
    """A snapshot file is unreadable, or was written by a newer format version."""


def build_snapshot(warehouse: SqliteWarehouse) -> dict[str, Any]:
    """Manifest of the warehouse, tagged with the snapshot format version."""
    manifest = warehouse.manifest()
    return {
        "snapshot_format": SNAPSHOT_FORMAT,
        "schema_version": manifest["schema_version"],
        "tables": manifest["tables"],
        "digest": manifest["digest"],
    }


def write_snapshot(warehouse: SqliteWarehouse, path: PATH_TYPE) -> dict[str, Any]:
    """Write the snapshot atomically, so a crash never leaves half a file.

    The temporary name is fixed rather than random: a leftover file from a killed
    run is overwritten on the next attempt instead of accumulating.
    """
    snapshot = build_snapshot(warehouse)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".tmp")
    scratch.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(scratch, target)
    return snapshot


def read_snapshot(path: PATH_TYPE) -> dict[str, Any]:
    """Load and validate a snapshot file."""
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotError(f"snapshot not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot is not valid JSON: {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SnapshotError(f"snapshot root must be an object: {source}")

    version = raw.get("snapshot_format")
    if not isinstance(version, int):
        raise SnapshotError(f"snapshot has no integer snapshot_format: {source}")
    if version > SNAPSHOT_FORMAT:
        raise SnapshotError(
            f"snapshot format {version} is newer than this reader "
            f"({SNAPSHOT_FORMAT}): {source}"
        )
    if not isinstance(raw.get("schema_version"), str):
        raise SnapshotError(f"snapshot has no schema_version: {source}")
    if not isinstance(raw.get("digest"), str):
        raise SnapshotError(f"snapshot has no digest: {source}")
    tables = raw.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise SnapshotError(f"snapshot has no tables: {source}")
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise SnapshotError(f"table entry is not an object: {name}")
        if not isinstance(table.get("rows"), int) or not isinstance(
            table.get("sha256"), str
        ):
            raise SnapshotError(f"table entry is missing rows or sha256: {name}")
    return raw


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Table-by-table comparison of two snapshots.

    A table is ``changed`` when its content digest moved, even if the row count did
    not: a value rewrite inside a version row is exactly the change a row count
    would hide.
    """
    old_tables = old["tables"]
    new_tables = new["tables"]
    tables: dict[str, dict[str, Any]] = {}
    for name in sorted(set(old_tables) | set(new_tables)):
        before = old_tables.get(name)
        after = new_tables.get(name)
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif before["sha256"] != after["sha256"]:
            status = "changed"
        else:
            status = "unchanged"
        tables[name] = {
            "status": status,
            "rows_before": None if before is None else before["rows"],
            "rows_after": None if after is None else after["rows"],
        }

    schema_moved = old["schema_version"] != new["schema_version"]
    changed = schema_moved or any(t["status"] != "unchanged" for t in tables.values())
    return {
        "identical": not changed,
        "digest_before": old["digest"],
        "digest_after": new["digest"],
        "schema_version_before": old["schema_version"],
        "schema_version_after": new["schema_version"],
        "schema_version_changed": schema_moved,
        "tables": tables,
    }


def _render_diff(diff: dict[str, Any]) -> str:
    lines = [
        f"schema {diff['schema_version_before']} -> {diff['schema_version_after']}"
        + (" (changed)" if diff["schema_version_changed"] else ""),
        f"digest {diff['digest_before'][:12]} -> {diff['digest_after'][:12]}",
    ]
    for name, table in diff["tables"].items():
        before = "-" if table["rows_before"] is None else str(table["rows_before"])
        after = "-" if table["rows_after"] is None else str(table["rows_after"])
        lines.append(f"{table['status']:<9} {name}: {before} -> {after}")
    return "\n".join(lines)


def _cmd_write(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"error: warehouse not found: {args.db}", file=sys.stderr)
        return 2
    with SqliteWarehouse(args.db) as warehouse:
        snapshot = write_snapshot(warehouse, args.out)
    print(
        f"wrote {args.out}: schema v{snapshot['schema_version']}, "
        f"{len(snapshot['tables'])} tables, digest {snapshot['digest'][:12]}"
    )
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    try:
        old = read_snapshot(args.before)
        new = read_snapshot(args.after)
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    diff = diff_snapshots(old, new)
    print(_render_diff(diff))
    return 0 if diff["identical"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ghl-snapshot",
        description="Write a warehouse snapshot, or diff two of them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="write a snapshot of a warehouse file")
    write.add_argument("--db", required=True, help="path to the SQLite warehouse")
    write.add_argument("--out", required=True, help="path of the snapshot to write")
    write.set_defaults(func=_cmd_write)

    diff = sub.add_parser("diff", help="compare two snapshots")
    diff.add_argument("before", help="baseline snapshot")
    diff.add_argument("after", help="snapshot to compare against the baseline")
    diff.set_defaults(func=_cmd_diff)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
