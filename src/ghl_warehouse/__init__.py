"""Reporting warehouse package (GHL-025).

Public surface: the star schema contract, the incremental contact extractor, and
the watermark store it advances. The sink is pluggable on purpose — the reference
:class:`~ghl_warehouse.sink.InMemoryWarehouse` exists to prove the load rules, not
to be production storage.
"""

from .extractor import (
    Collection,
    ContactSource,
    ExtractStats,
    IncompleteExtractError,
    IncrementalContactExtractor,
)
from .schema import (
    DIM_CONTACT,
    DIM_DATE,
    DIM_PIPELINE_STAGE,
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
from .sink import (
    ContactRecord,
    ContactSink,
    InMemoryWarehouse,
    InvalidRecordError,
    LoadResult,
    normalise_contact,
    row_hash,
)
from .watermark import (
    DEFAULT_OVERLAP_SECONDS,
    InMemoryWatermarkStore,
    JsonFileWatermarkStore,
    Watermark,
    WatermarkStore,
    advance,
)

__all__ = [
    "DEFAULT_OVERLAP_SECONDS",
    "DIM_CONTACT",
    "DIM_DATE",
    "DIM_PIPELINE_STAGE",
    "FACT_CONTACT_VERSION",
    "STAR_SCHEMA",
    "Collection",
    "Column",
    "ContactRecord",
    "ContactSink",
    "ContactSource",
    "ExtractStats",
    "InMemoryWarehouse",
    "InMemoryWatermarkStore",
    "IncompleteExtractError",
    "IncrementalContactExtractor",
    "InvalidRecordError",
    "JsonFileWatermarkStore",
    "LoadResult",
    "SchemaError",
    "StarSchema",
    "Table",
    "Watermark",
    "WatermarkStore",
    "advance",
    "contact_key",
    "date_key",
    "normalise_contact",
    "row_hash",
    "validate_schema",
]
