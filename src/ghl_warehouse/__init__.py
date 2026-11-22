"""Reporting warehouse package (GHL-025 / GHL-026).

Public surface: the star schema contract, the incremental contact and opportunity
extractors, and the watermark store they advance. Sinks are pluggable on purpose —
the reference ``InMemoryWarehouse`` and ``InMemoryOpportunityWarehouse`` exist to
prove the load rules, not to be production storage.
"""

from .extractor import (
    Collection,
    ContactSource,
    ExtractStats,
    IncompleteExtractError,
    IncrementalContactExtractor,
)
from .opportunities import (
    KNOWN_STATUSES,
    OPEN_STATUSES,
    IncrementalOpportunityExtractor,
    InMemoryOpportunityWarehouse,
    OpportunityLoadResult,
    OpportunityRecord,
    OpportunitySink,
    OpportunitySource,
    normalise_opportunity,
    opportunity_row_hash,
)
from .schema import (
    DIM_CONTACT,
    DIM_DATE,
    DIM_OPPORTUNITY,
    DIM_PIPELINE_STAGE,
    FACT_CONTACT_VERSION,
    FACT_OPPORTUNITY_VERSION,
    STAR_SCHEMA,
    Column,
    SchemaError,
    StarSchema,
    Table,
    contact_key,
    date_key,
    opportunity_key,
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
    "DIM_OPPORTUNITY",
    "DIM_PIPELINE_STAGE",
    "FACT_CONTACT_VERSION",
    "FACT_OPPORTUNITY_VERSION",
    "KNOWN_STATUSES",
    "OPEN_STATUSES",
    "STAR_SCHEMA",
    "Collection",
    "Column",
    "ContactRecord",
    "ContactSink",
    "ContactSource",
    "ExtractStats",
    "InMemoryOpportunityWarehouse",
    "InMemoryWarehouse",
    "InMemoryWatermarkStore",
    "IncompleteExtractError",
    "IncrementalContactExtractor",
    "IncrementalOpportunityExtractor",
    "InvalidRecordError",
    "JsonFileWatermarkStore",
    "LoadResult",
    "OpportunityLoadResult",
    "OpportunityRecord",
    "OpportunitySink",
    "OpportunitySource",
    "SchemaError",
    "StarSchema",
    "Table",
    "Watermark",
    "WatermarkStore",
    "advance",
    "contact_key",
    "date_key",
    "normalise_contact",
    "normalise_opportunity",
    "opportunity_key",
    "opportunity_row_hash",
    "row_hash",
    "validate_schema",
]
