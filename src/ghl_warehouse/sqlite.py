"""Durable SQLite sink for the reporting warehouse (GHL-027).

The in-memory sinks prove the load rules; this one keeps them after the process
exits. It is deliberately the same contract — ``load_contact`` /
``load_opportunity`` in, ``LoadResult`` / ``OpportunityLoadResult`` out — because a
sink that behaves differently from the reference implementation turns every test
written against the reference into a lie about production.

Two decisions worth stating before the code:

* one transaction per record load. A crash between "close the old version" and
  "insert the new one" would leave a contact with two current rows or none, and
  both are worse than losing a batch that the watermark replay will refetch.
* ``manifest()`` is a reproducibility check, not a report. It hashes the ordered
  contents of every table, so two loads of the same source data produce the same
  manifest and any difference is a real difference rather than a timestamp.

Timestamps stay TEXT in ISO form. SQLite has no date type worth trusting, and
keeping the provider's own ordering key as text means the comparison that decides
stale-versus-new is the same comparison the in-memory sink makes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self

from .opportunities import (
    OPEN_STATUSES,
    OpportunityLoadResult,
    OpportunityRecord,
    opportunity_row_hash,
)
from .schema import STAR_SCHEMA, StarSchema, Table, date_key, stage_key
from .sink import (
    TRACKED_ATTRIBUTES as CONTACT_TRACKED,
)
from .sink import (
    ContactRecord,
    LoadResult,
    row_hash,
)
from .watermark import parse_timestamp

__all__ = ["SqliteWarehouse"]

PATH_TYPE = str | Path


def _days_between(start: str, end: str) -> int:
    delta = parse_timestamp(end) - parse_timestamp(start)
    return max(int(delta.total_seconds() // 86400), 0)


class SqliteWarehouse:
    """Type-2 warehouse in a single SQLite file.

    Also satisfies ``ContactSink`` and ``OpportunitySink``, so either extractor can
    be pointed at it without adapters.
    """

    def __init__(self, path: PATH_TYPE, *, schema: StarSchema = STAR_SCHEMA) -> None:
        self.path = Path(path)
        self.schema = schema
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(schema.to_ddl())
        # The DDL cannot express "at most one current row per key"; a partial unique
        # index can, and it turns a silent double-current bug into a failed insert.
        for table, key in (
            ("dim_contact", "contact_key"),
            ("dim_opportunity", "opportunity_key"),
        ):
            if any(item.name == table for item in schema.tables):
                self._conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_current "
                    f"ON {table} ({key}) WHERE is_current = 1"
                )
        self._conn.commit()

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- schema helpers --------------------------------------------------
    def _table(self, name: str) -> Table:
        return self.schema.table(name)

    def _insert(self, name: str, row: dict[str, Any]) -> None:
        """Insert only columns the schema declares.

        An unexpected key is a bug in the caller, not something to store: writing
        it would make the file disagree with its own DDL.
        """

        table = self._table(name)
        declared = set(table.names)
        unknown = set(row) - declared
        if unknown:
            raise ValueError(
                f"{name} row carries undeclared columns: {sorted(unknown)}"
            )
        columns = [column for column in table.names if column in row]
        placeholders = ", ".join("?" for _ in columns)
        statement = f"INSERT OR IGNORE INTO {name} ({', '.join(columns)}) VALUES ({placeholders})"
        self._conn.execute(statement, [row[column] for column in columns])

    def _ensure_date(self, key: int, iso_date: str) -> None:
        self._insert(
            "dim_date",
            {
                "date_key": key,
                "calendar_date": iso_date,
                "year": key // 10000,
                "month": (key // 100) % 100,
                "day": key % 100,
                "iso_week": 0,
                "weekday": 0,
            },
        )

    def _current_row(
        self, table: str, key_column: str, key: str
    ) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ? AND is_current = 1", (key,)
        )
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def _next_version(self, table: str, key_column: str, key: str) -> int:
        cursor = self._conn.execute(
            f"SELECT COALESCE(MAX(version), 0) FROM {table} WHERE {key_column} = ?",
            (key,),
        )
        return int(cursor.fetchone()[0]) + 1

    # -- reads -----------------------------------------------------------
    def current_version(self, table: str, key: str) -> dict[str, Any] | None:
        key_column = "contact_key" if table == "dim_contact" else "opportunity_key"
        return self._current_row(table, key_column, key)

    def versions(self, table: str, key: str) -> tuple[dict[str, Any], ...]:
        key_column = "contact_key" if table == "dim_contact" else "opportunity_key"
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ? ORDER BY version ASC", (key,)
        )
        return tuple(dict(row) for row in cursor.fetchall())

    def contact_count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(DISTINCT contact_key) FROM dim_contact"
            ).fetchone()[0]
        )

    def opportunity_count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(DISTINCT opportunity_key) FROM dim_opportunity"
            ).fetchone()[0]
        )

    def open_value(self) -> float:
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        cursor = self._conn.execute(
            f"SELECT COALESCE(SUM(monetary_value), 0) FROM dim_opportunity "
            f"WHERE is_current = 1 AND status IN ({placeholders})",
            sorted(OPEN_STATUSES),
        )
        return float(cursor.fetchone()[0])

    def table_counts(self) -> dict[str, int]:
        return {
            table.name: int(
                self._conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
            )
            for table in self.schema.tables
        }

    def manifest(self) -> dict[str, Any]:
        """Ordered per-table digest. Same data in, same manifest out."""

        tables: dict[str, dict[str, Any]] = {}
        for table in self.schema.tables:
            order = ", ".join(table.primary_key)
            cursor = self._conn.execute(f"SELECT * FROM {table.name} ORDER BY {order}")
            rows = [list(row) for row in cursor.fetchall()]
            blob = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
            tables[table.name] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            }
        overall = hashlib.sha256(
            json.dumps(tables, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": self.schema.version,
            "tables": tables,
            "digest": overall,
        }

    # -- contact load ----------------------------------------------------
    def load_contact(
        self, record: ContactRecord, *, observed_at: datetime
    ) -> LoadResult:
        key = record.key
        digest = row_hash(record)
        current = self._current_row("dim_contact", "contact_key", key)

        if current is not None:
            if record.updated_at < current["source_updated_at"]:
                return LoadResult(key, int(current["version"]), "stale_ignored")
            if digest == current["row_hash"]:
                return LoadResult(key, int(current["version"]), "unchanged")
            version = int(current["version"]) + 1
            changed = sum(
                1
                for name in CONTACT_TRACKED
                if current.get(name) != getattr(record, name)
            )
        else:
            version = self._next_version("dim_contact", "contact_key", key)
            changed = sum(
                1 for name in CONTACT_TRACKED if getattr(record, name) is not None
            )

        created_key = record.created_date_key
        observed_date = observed_at.astimezone(timezone.utc).date().isoformat()
        observed_key = date_key(observed_date)

        with self._conn:
            self._ensure_date(created_key, record.created_at[:10])
            self._ensure_date(observed_key, observed_date)
            if current is not None:
                self._conn.execute(
                    "UPDATE dim_contact SET valid_to = ?, is_current = 0 "
                    "WHERE contact_key = ? AND version = ?",
                    (record.updated_at, key, int(current["version"])),
                )
            self._insert(
                "dim_contact",
                {
                    "contact_key": key,
                    "contact_id": record.contact_id,
                    "location_id": record.location_id,
                    "version": version,
                    "row_hash": digest,
                    "email": record.email,
                    "phone": record.phone,
                    "first_name": record.first_name,
                    "last_name": record.last_name,
                    "source": record.source,
                    "tag_key": record.tag_key,
                    "stage_key": None,
                    "created_date_key": created_key,
                    "source_updated_at": record.updated_at,
                    "valid_from": record.updated_at,
                    "valid_to": None,
                    "is_current": 1,
                },
            )
            self._insert(
                "fact_contact_version",
                {
                    "contact_key": key,
                    "version": version,
                    "observed_date_key": observed_key,
                    "attributes_changed": changed,
                    "days_since_created": _days_between(
                        record.created_at, record.updated_at
                    ),
                    "first_seen_at": observed_at.astimezone(timezone.utc).isoformat(),
                },
            )

        return LoadResult(key, version, "new_version", attributes_changed=changed)

    # -- opportunity load ------------------------------------------------
    def _ensure_stage(self, record: OpportunityRecord) -> str | None:
        if not (record.pipeline_id and record.stage_id):
            return None
        key = stage_key(record.pipeline_id, record.stage_id)
        self._insert(
            "dim_pipeline_stage",
            {
                "stage_key": key,
                "stage_id": record.stage_id,
                "pipeline_id": record.pipeline_id,
                "stage_name": record.stage_name,
                "position": None,
            },
        )
        return key

    def load_opportunity(
        self, record: OpportunityRecord, *, observed_at: datetime
    ) -> OpportunityLoadResult:
        key = record.key
        digest = opportunity_row_hash(record)
        current = self._current_row("dim_opportunity", "opportunity_key", key)

        if current is not None:
            if record.updated_at < current["source_updated_at"]:
                return OpportunityLoadResult(
                    key, int(current["version"]), "stale_ignored"
                )
            if digest == current["row_hash"]:
                return OpportunityLoadResult(key, int(current["version"]), "unchanged")
            version = int(current["version"]) + 1
            changed = sum(
                1
                for name in (
                    "name",
                    "status",
                    "stage_id",
                    "pipeline_id",
                    "assigned_to",
                    "monetary_value",
                    "contact_id",
                )
                if current.get(name) != getattr(record, name)
            )
        else:
            version = self._next_version("dim_opportunity", "opportunity_key", key)
            changed = sum(
                1
                for name in (
                    "name",
                    "status",
                    "stage_id",
                    "pipeline_id",
                    "assigned_to",
                    "monetary_value",
                    "contact_id",
                )
                if getattr(record, name) not in (None, "", 0.0)
            )

        created_key = record.created_date_key
        observed_date = observed_at.astimezone(timezone.utc).date().isoformat()
        observed_key = date_key(observed_date)

        with self._conn:
            self._ensure_date(created_key, record.created_at[:10])
            self._ensure_date(observed_key, observed_date)
            stage = self._ensure_stage(record)
            if current is not None:
                self._conn.execute(
                    "UPDATE dim_opportunity SET valid_to = ?, is_current = 0 "
                    "WHERE opportunity_key = ? AND version = ?",
                    (record.updated_at, key, int(current["version"])),
                )
            self._insert(
                "dim_opportunity",
                {
                    "opportunity_key": key,
                    "opportunity_id": record.opportunity_id,
                    "location_id": record.location_id,
                    "version": version,
                    "row_hash": digest,
                    "contact_key": record.contact_key,
                    "name": record.name,
                    "status": record.status,
                    "stage_key": stage,
                    "assigned_to": record.assigned_to,
                    "source": None,
                    "monetary_value": record.monetary_value,
                    "created_date_key": created_key,
                    "source_updated_at": record.updated_at,
                    "valid_from": record.updated_at,
                    "valid_to": None,
                    "is_current": 1,
                },
            )
            self._insert(
                "fact_opportunity_version",
                {
                    "opportunity_key": key,
                    "version": version,
                    "observed_date_key": observed_key,
                    "monetary_value": record.monetary_value,
                    "attributes_changed": changed,
                    "days_since_created": _days_between(
                        record.created_at, record.updated_at
                    ),
                    "is_open": record.is_open,
                    "first_seen_at": observed_at.astimezone(timezone.utc).isoformat(),
                },
            )

        return OpportunityLoadResult(
            key, version, "new_version", attributes_changed=changed
        )
