"""ghl-attribution: lead intake that cannot lose or duplicate credit.

Public surface:
    AttributionEngine  -- discovery + idempotent ingest
    AttributionMapping -- attribution key -> GHL custom field map
    AttributionPolicy  -- what may be written, and what stays frozen
"""

from .engine import AttributionEngine, IngestResult, IngestStatus
from .events import AttributionEvent, InvalidAttributionEvent, Touch
from .ledger import ClaimStore, InMemoryClaimStore
from .mapping import (
    AttributionMapping,
    AttributionMappingError,
    CustomFieldDef,
    FieldDrift,
    FieldSpec,
    ResolvedMapping,
)
from .policy import AttributionPlan, AttributionPolicy

__all__ = [
    "AttributionEngine",
    "AttributionEvent",
    "AttributionMapping",
    "AttributionMappingError",
    "AttributionPlan",
    "AttributionPolicy",
    "ClaimStore",
    "CustomFieldDef",
    "FieldDrift",
    "FieldSpec",
    "InMemoryClaimStore",
    "IngestResult",
    "IngestStatus",
    "InvalidAttributionEvent",
    "ResolvedMapping",
    "Touch",
]
