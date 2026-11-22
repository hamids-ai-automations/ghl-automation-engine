"""GHL-019: attribution field map plus custom-field discovery.

The map is data, not code. Attribution keys are the stable names our
pipeline speaks; GHL keys are the location-scoped custom fields that
must already exist in the target location.

Discovery does not guess. If a mapped field is missing, duplicated, or
has drifted to another ``dataType``, the engine refuses to start and
reports every problem at once instead of writing into the wrong field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "AttributionMapping",
    "AttributionMappingError",
    "CustomFieldDef",
    "FieldDrift",
    "FieldSpec",
    "ResolvedMapping",
]


@dataclass(frozen=True)
class FieldSpec:
    """One attribution key and the GHL custom field that backs it."""

    key: str
    ghl_key: str
    data_type: str
    required: bool = True

    def __str__(self) -> str:
        return f"{self.key} -> {self.ghl_key} ({self.data_type})"


@dataclass(frozen=True)
class FieldDrift:
    """A single mismatch between the map and the live location schema."""

    key: str
    ghl_key: str
    kind: str  # "missing" | "type" | "ambiguous"
    detail: str

    def __str__(self) -> str:
        return f"{self.key} [{self.kind}] {self.detail}"


class AttributionMappingError(Exception):
    """Raised when the live custom-field schema cannot serve the map."""

    def __init__(self, drift: Sequence[FieldDrift]) -> None:
        self.drift = list(drift)
        lines = "; ".join(str(item) for item in self.drift)
        super().__init__(f"custom field mapping drift ({len(self.drift)}): {lines}")


@dataclass(frozen=True)
class CustomFieldDef:
    id: str
    field_key: str
    data_type: str
    name: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CustomFieldDef":
        return cls(
            id=str(payload.get("id") or ""),
            field_key=str(payload.get("fieldKey") or payload.get("key") or ""),
            data_type=str(payload.get("dataType") or ""),
            name=str(payload.get("name") or ""),
        )


@dataclass(frozen=True)
class ResolvedMapping:
    """Map bound to concrete custom-field ids for one location."""

    location_id: str
    specs: Mapping[str, FieldSpec]
    field_ids: Mapping[str, str]

    def field_id(self, key: str) -> str:
        return self.field_ids[key]

    def ghl_key(self, key: str) -> str:
        return self.specs[key].ghl_key

    def keys(self) -> tuple[str, ...]:
        return tuple(self.specs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "locationId": self.location_id,
            "fields": {
                key: {
                    "ghlKey": spec.ghl_key,
                    "dataType": spec.data_type,
                    "id": self.field_ids.get(key),
                    "required": spec.required,
                }
                for key, spec in self.specs.items()
            },
        }


DEFAULT_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("utm_source", "contact.attribution_source", "TEXT"),
    FieldSpec("utm_medium", "contact.attribution_medium", "TEXT"),
    FieldSpec("utm_campaign", "contact.attribution_campaign", "TEXT"),
    FieldSpec("utm_content", "contact.attribution_content", "TEXT"),
    FieldSpec("landing_page", "contact.attribution_landing_page", "TEXT"),
    FieldSpec("first_touch_at", "contact.attribution_first_touch_at", "DATE"),
    FieldSpec("last_touch_at", "contact.attribution_last_touch_at", "DATE"),
    FieldSpec("gclid", "contact.gclid", "TEXT", required=False),
    FieldSpec("fbclid", "contact.fbclid", "TEXT", required=False),
)


@dataclass(frozen=True)
class AttributionMapping:
    """Declarative map from attribution keys to GHL custom fields."""

    specs: tuple[FieldSpec, ...] = DEFAULT_SPECS

    @classmethod
    def default(cls) -> "AttributionMapping":
        return cls(DEFAULT_SPECS)

    def spec(self, key: str) -> FieldSpec:
        for spec in self.specs:
            if spec.key == key:
                return spec
        raise KeyError(key)

    def resolve(
        self,
        definitions: Iterable[CustomFieldDef | Mapping[str, Any]],
        *,
        location_id: str = "",
    ) -> ResolvedMapping:
        """Bind the map to live field ids, raising on any drift."""

        parsed = [
            item if isinstance(item, CustomFieldDef) else CustomFieldDef.from_payload(item)
            for item in definitions
        ]

        index: dict[str, list[CustomFieldDef]] = {}
        for definition in parsed:
            if not definition.field_key:
                continue
            index.setdefault(definition.field_key.strip().lower(), []).append(definition)

        drift: list[FieldDrift] = []
        resolved_specs: dict[str, FieldSpec] = {}
        field_ids: dict[str, str] = {}

        for spec in self.specs:
            candidates = index.get(spec.ghl_key.strip().lower(), [])
            if not candidates:
                if spec.required:
                    drift.append(
                        FieldDrift(
                            key=spec.key,
                            ghl_key=spec.ghl_key,
                            kind="missing",
                            detail="custom field not found in location",
                        )
                    )
                continue
            if len(candidates) > 1:
                ids = ", ".join(item.id for item in candidates)
                drift.append(
                    FieldDrift(
                        key=spec.key,
                        ghl_key=spec.ghl_key,
                        kind="ambiguous",
                        detail=f"{len(candidates)} fields share this key ({ids})",
                    )
                )
                continue
            definition = candidates[0]
            actual = definition.data_type.strip().upper()
            if actual != spec.data_type.upper():
                drift.append(
                    FieldDrift(
                        key=spec.key,
                        ghl_key=spec.ghl_key,
                        kind="type",
                        detail=f"expected {spec.data_type}, found {actual or 'unknown'}",
                    )
                )
                continue
            if not definition.id:
                drift.append(
                    FieldDrift(
                        key=spec.key,
                        ghl_key=spec.ghl_key,
                        kind="missing",
                        detail="custom field has no id",
                    )
                )
                continue
            resolved_specs[spec.key] = spec
            field_ids[spec.key] = definition.id

        if drift:
            raise AttributionMappingError(drift)

        return ResolvedMapping(
            location_id=location_id,
            specs=resolved_specs,
            field_ids=field_ids,
        )
