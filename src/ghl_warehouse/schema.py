"""Star schema for the reporting warehouse (GHL-025).

Shape, in one line: dimensions hold current state and its history, facts hold
observable events with the measures computed from them. No fact may reference a
dimension that is not declared here — ``validate_schema`` fails at import time if
it does, which is cheaper than discovering a dangling join three reports later.

Keys are deterministic hashes of the source identifiers, not sequence numbers, so
reloading the same contact into an empty warehouse produces the same rows. That is
what makes the incremental load idempotent rather than merely repeatable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Literal

__all__ = [
    "Column",
    "DIM_CONTACT",
    "DIM_DATE",
    "DIM_PIPELINE_STAGE",
    "FACT_CONTACT_VERSION",
    "STAR_SCHEMA",
    "SchemaError",
    "StarSchema",
    "Table",
    "column_names",
    "contact_key",
    "date_key",
    "stage_key",
    "validate_schema",
]


class SchemaError(Exception):
    """The schema contradicts itself. Never rescued at runtime."""


#: SQL types are kept portable: SQLite is used in tests, Postgres in deployment.
SQLType = Literal["TEXT", "INTEGER", "REAL"]


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: SQLType
    nullable: bool = True
    role: Literal["key", "foreign_key", "attribute", "measure", "date_key"] = "attribute"
    note: str = ""

    def to_ddl(self) -> str:
        parts = [self.name, self.sql_type]
        if not self.nullable:
            parts.append("NOT NULL")
        return " ".join(parts)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    kind: Literal["dimension", "fact"]
    description: str = ""

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        raise SchemaError(f"{self.name} has no column {name!r}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def to_ddl(self) -> str:
        body = ",\n  ".join(column.to_ddl() for column in self.columns)
        keys = ", ".join(self.primary_key)
        return (
            f"CREATE TABLE IF NOT EXISTS {self.name} (\n  {body},\n"
            f"  PRIMARY KEY ({keys})\n);"
        )


DIM_DATE = Table(
    name="dim_date",
    description="Calendar spine. Every fact joins to it; no report invents its own dates.",
    columns=(
        Column("date_key", "INTEGER", nullable=False, role="key"),
        Column("calendar_date", "TEXT", nullable=False),
        Column("year", "INTEGER", nullable=False),
        Column("month", "INTEGER", nullable=False),
        Column("day", "INTEGER", nullable=False),
        Column("iso_week", "INTEGER", nullable=False),
        Column("weekday", "INTEGER", nullable=False),
    ),
    primary_key=("date_key",),
    kind="dimension",
)


DIM_CONTACT = Table(
    name="dim_contact",
    description=(
        "Type-2 contact dimension: one row per observed version, previous versions "
        "closed with valid_to. is_current marks exactly one row per contact."
    ),
    columns=(
        Column("contact_key", "TEXT", nullable=False, role="key"),
        Column("contact_id", "TEXT", nullable=False),
        Column("location_id", "TEXT"),
        Column("version", "INTEGER", nullable=False, role="attribute"),
        Column("row_hash", "TEXT", nullable=False, role="attribute",
               note="hash of the normalised payload; identical hash means no new version"),
        Column("email", "TEXT"),
        Column("phone", "TEXT"),
        Column("first_name", "TEXT"),
        Column("last_name", "TEXT"),
        Column("source", "TEXT"),
        Column("stage_key", "TEXT", role="foreign_key"),
        Column("created_date_key", "INTEGER", nullable=False, role="date_key"),
        Column("source_updated_at", "TEXT", nullable=False),
        Column("valid_from", "TEXT", nullable=False),
        Column("valid_to", "TEXT"),
        Column("is_current", "INTEGER", nullable=False),
    ),
    primary_key=("contact_key", "version"),
    kind="dimension",
)


DIM_PIPELINE_STAGE = Table(
    name="dim_pipeline_stage",
    description=(
        "Pipeline stages seen in the sub-account. Declared as part of the contract; "
        "populated once opportunity extraction lands (it is not filled by the "
        "contact extractor, and no report may assume it is)."
    ),
    columns=(
        Column("stage_key", "TEXT", nullable=False, role="key"),
        Column("stage_id", "TEXT", nullable=False),
        Column("pipeline_id", "TEXT", nullable=False),
        Column("stage_name", "TEXT"),
        Column("position", "INTEGER"),
    ),
    primary_key=("stage_key",),
    kind="dimension",
)


FACT_CONTACT_VERSION = Table(
    name="fact_contact_version",
    description=(
        "Grain: one row per contact version. Measures describe the change that "
        "created the version, so 'how much churn is there really' is a query, not "
        "an opinion."
    ),
    columns=(
        Column("contact_key", "TEXT", nullable=False, role="foreign_key"),
        Column("version", "INTEGER", nullable=False, role="foreign_key"),
        Column("observed_date_key", "INTEGER", nullable=False, role="date_key"),
        Column("attributes_changed", "INTEGER", nullable=False, role="measure"),
        Column("days_since_created", "INTEGER", nullable=False, role="measure"),
        Column("first_seen_at", "TEXT", nullable=False),
    ),
    primary_key=("contact_key", "version"),
    kind="fact",
)


@dataclass(frozen=True)
class StarSchema:
    version: str
    dimensions: tuple[Table, ...]
    facts: tuple[Table, ...]

    @property
    def tables(self) -> tuple[Table, ...]:
        return self.dimensions + self.facts

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise SchemaError(f"no table named {name!r}")

    def to_ddl(self) -> str:
        header = f"-- ghl warehouse star schema v{self.version}\n"
        return header + "\n\n".join(table.to_ddl() for table in self.tables) + "\n"


STAR_SCHEMA = StarSchema(
    version="1",
    dimensions=(DIM_DATE, DIM_CONTACT, DIM_PIPELINE_STAGE),
    facts=(FACT_CONTACT_VERSION,),
)


def validate_schema(schema: StarSchema = STAR_SCHEMA) -> None:
    """Refuse a schema that cannot be joined.

    Checks: unique table and column names, non-nullable keys, every foreign key
    resolves to a declared dimension key, every fact carries at least one measure
    and reaches ``dim_date``.
    """

    seen_tables: set[str] = set()
    for table in schema.tables:
        if table.name in seen_tables:
            raise SchemaError(f"duplicate table {table.name!r}")
        seen_tables.add(table.name)

        seen_columns: set[str] = set()
        for column in table.columns:
            if column.name in seen_columns:
                raise SchemaError(f"duplicate column {table.name}.{column.name}")
            seen_columns.add(column.name)

        for key in table.primary_key:
            column = table.column(key)
            if column.nullable:
                raise SchemaError(f"primary key {table.name}.{key} is nullable")

        if table.kind == "dimension":
            continue

        measures = [column for column in table.columns if column.role == "measure"]
        if not measures:
            raise SchemaError(f"fact {table.name} has no measures")
        if not any(column.role == "date_key" for column in table.columns):
            raise SchemaError(f"fact {table.name} does not reach dim_date")
        for column in table.columns:
            if column.role == "foreign_key":
                _require_dimension_key(schema, table, column)


def _require_dimension_key(schema: StarSchema, table: Table, column: Column) -> None:
    for dimension in schema.dimensions:
        if column.name in dimension.primary_key:
            return
    raise SchemaError(
        f"{table.name}.{column.name} declares a foreign key but no dimension "
        "exposes a primary key with that name"
    )


def _hash_key(*parts: str, prefix: str) -> str:
    blob = "\x1f".join(part.strip() for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(blob).hexdigest()[:24]}"


def contact_key(location_id: str, contact_id: str) -> str:
    """Deterministic surrogate key. Same contact, same key, every reload."""

    if not contact_id.strip():
        raise SchemaError("contact_id is required for a surrogate key")
    return _hash_key(location_id or "", contact_id, prefix="ct")


def stage_key(pipeline_id: str, stage_id: str) -> str:
    if not stage_id.strip():
        raise SchemaError("stage_id is required for a surrogate key")
    return _hash_key(pipeline_id or "", stage_id, prefix="st")


def date_key(iso_date: str) -> int:
    """``YYYY-MM-DD`` -> ``YYYYMMDD``. Rejects anything that is not a date."""

    text = iso_date.strip()[:10]
    parts = text.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise SchemaError(f"not an ISO date: {iso_date!r}")
    year, month, day = (int(part) for part in parts)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise SchemaError(f"not a valid date: {iso_date!r}")
    return year * 10000 + month * 100 + day


validate_schema()


def column_names(table: Table) -> Iterable[str]:
    """Convenience for loaders that build INSERT statements from column order."""

    return table.names
