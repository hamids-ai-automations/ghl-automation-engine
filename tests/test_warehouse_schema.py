"""Schema contract tests (GHL-025).

The schema is validated at import time, so a contradiction ships as a failed test
run rather than a report that joins on a column nobody defined.
"""

from __future__ import annotations

import pytest

from ghl_warehouse.schema import (
    DIM_CONTACT,
    DIM_DATE,
    FACT_CONTACT_VERSION,
    STAR_SCHEMA,
    Column,
    SchemaError,
    StarSchema,
    Table,
    contact_key,
    date_key,
    validate_schema,
)


class TestDefaults:
    def test_default_schema_validates(self) -> None:
        validate_schema()

    def test_ddl_covers_every_table(self) -> None:
        ddl = STAR_SCHEMA.to_ddl()
        for table in STAR_SCHEMA.tables:
            assert f"CREATE TABLE IF NOT EXISTS {table.name}" in ddl

    def test_ddl_declares_primary_keys(self) -> None:
        ddl = STAR_SCHEMA.to_ddl()
        assert "PRIMARY KEY (date_key)" in ddl
        assert "PRIMARY KEY (contact_key, version)" in ddl

    def test_fact_grain_is_contact_version(self) -> None:
        assert FACT_CONTACT_VERSION.primary_key == ("contact_key", "version")
        assert FACT_CONTACT_VERSION.kind == "fact"

    def test_contact_dimension_tracks_scd2_columns(self) -> None:
        for name in ("valid_from", "valid_to", "is_current", "version", "row_hash"):
            assert DIM_CONTACT.column(name)

    def test_every_fact_has_a_measure_and_a_date(self) -> None:
        for fact in STAR_SCHEMA.facts:
            roles = {column.role for column in fact.columns}
            assert "measure" in roles
            assert "date_key" in roles

    def test_table_lookup_raises_for_unknown(self) -> None:
        with pytest.raises(SchemaError):
            STAR_SCHEMA.table("dim_missing")


class TestValidationRejects:
    def _schema(self, *tables: Table) -> StarSchema:
        dimensions = tuple(table for table in tables if table.kind == "dimension")
        facts = tuple(table for table in tables if table.kind == "fact")
        return StarSchema(version="test", dimensions=dimensions, facts=facts)

    def test_duplicate_table_name(self) -> None:
        schema = self._schema(DIM_DATE, DIM_DATE)
        with pytest.raises(SchemaError, match="duplicate table"):
            validate_schema(schema)

    def test_duplicate_column_name(self) -> None:
        broken = Table(
            name="dim_x",
            columns=(
                Column("x", "TEXT", nullable=False, role="key"),
                Column("x", "TEXT"),
            ),
            primary_key=("x",),
            kind="dimension",
        )
        with pytest.raises(SchemaError, match="duplicate column"):
            validate_schema(self._schema(broken))

    def test_nullable_primary_key(self) -> None:
        broken = Table(
            name="dim_x",
            columns=(Column("x", "TEXT", nullable=True, role="key"),),
            primary_key=("x",),
            kind="dimension",
        )
        with pytest.raises(SchemaError, match="nullable"):
            validate_schema(self._schema(broken))

    def test_fact_without_measures(self) -> None:
        broken = Table(
            name="fact_x",
            columns=(
                Column("x_key", "TEXT", nullable=False, role="foreign_key"),
                Column("date_key", "INTEGER", nullable=False, role="date_key"),
            ),
            primary_key=("x_key",),
            kind="fact",
        )
        dim = Table(
            name="dim_x",
            columns=(Column("x_key", "TEXT", nullable=False, role="key"),),
            primary_key=("x_key",),
            kind="dimension",
        )
        with pytest.raises(SchemaError, match="no measures"):
            validate_schema(self._schema(dim, broken))

    def test_fact_without_date_key(self) -> None:
        broken = Table(
            name="fact_x",
            columns=(
                Column("x_key", "TEXT", nullable=False, role="foreign_key"),
                Column("n", "INTEGER", nullable=False, role="measure"),
            ),
            primary_key=("x_key",),
            kind="fact",
        )
        dim = Table(
            name="dim_x",
            columns=(Column("x_key", "TEXT", nullable=False, role="key"),),
            primary_key=("x_key",),
            kind="dimension",
        )
        with pytest.raises(SchemaError, match="dim_date"):
            validate_schema(self._schema(dim, broken))

    def test_foreign_key_with_no_dimension_key(self) -> None:
        broken = Table(
            name="fact_x",
            columns=(
                Column("ghost_key", "TEXT", nullable=False, role="foreign_key"),
                Column("date_key", "INTEGER", nullable=False, role="date_key"),
                Column("n", "INTEGER", nullable=False, role="measure"),
            ),
            primary_key=("ghost_key", "date_key"),
            kind="fact",
        )
        with pytest.raises(SchemaError, match="no dimension"):
            validate_schema(self._schema(DIM_DATE, broken))


class TestKeys:
    def test_contact_key_is_deterministic(self) -> None:
        assert contact_key("loc", "c1") == contact_key("loc", "c1")

    def test_contact_key_scopes_by_location(self) -> None:
        assert contact_key("loc-a", "c1") != contact_key("loc-b", "c1")

    def test_contact_key_requires_id(self) -> None:
        with pytest.raises(SchemaError):
            contact_key("loc", "  ")

    def test_date_key_round_trip(self) -> None:
        assert date_key("2026-11-22") == 20261122
        assert date_key("2026-11-22T10:00:00Z") == 20261122

    @pytest.mark.parametrize("bad", ["", "2026/11/22", "2026-13-01", "2026-11-99", "abc"])
    def test_date_key_rejects_garbage(self, bad: str) -> None:
        with pytest.raises(SchemaError):
            date_key(bad)
