"""A JSON Schema subset validator, hand-written because the runtime has no dependencies.

Why not ``jsonschema``: ADR-001 committed this repository to the standard library. A
tool server that drags a validation library into every deployment for six keywords is
a worse trade than 150 lines with tests.

Only the keywords this server's schemas actually use are implemented, and an unknown
keyword raises instead of being ignored — a schema that claims a constraint the
validator does not enforce is the same class of bug as a paginated read that claims
completeness.

Supported: ``type``, ``enum``, ``required``, ``properties``, ``additionalProperties``
(``false`` or a schema), ``minimum``, ``maximum``, ``minLength``, ``maxLength``,
``items``, ``minItems``, ``maxItems``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

_SUPPORTED = frozenset(
    {
        "type",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "items",
        "minItems",
        "maxItems",
        "default",
        "description",
        "title",
    }
)


def _is_type(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported schema type: {expected!r}")


def _validate(instance: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> None:
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise ValueError(f"schema at {path} uses unsupported keyword(s): {sorted(unknown)}")

    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(t, instance) for t in allowed):
            errors.append(f"{path}: expected {expected}, got {type(instance).__name__}")
            return

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: must be at most {schema['maxLength']} characters")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: must contain at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: must contain at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, value in enumerate(instance):
                _validate(value, item_schema, f"{path}[{index}]", errors)

    if isinstance(instance, Mapping):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties") or {}
        for name, value in instance.items():
            if name in properties:
                _validate(value, properties[name], f"{path}.{name}", errors)
                continue
            extra = schema.get("additionalProperties", True)
            if extra is False:
                errors.append(
                    f"{path}: unexpected property {name!r} "
                    f"(known: {sorted(properties)})"
                )
            elif isinstance(extra, Mapping):
                _validate(value, extra, f"{path}.{name}", errors)


def validate(instance: Any, schema: Mapping[str, Any]) -> list[str]:
    """Return a list of human-readable violations. Empty means valid."""

    errors: list[str] = []
    _validate(instance, schema, "$", errors)
    return errors


def describe(schema: Mapping[str, Any]) -> Iterable[str]:
    """Yield one line per top-level property; used by the server's self-description."""

    for name, spec in (schema.get("properties") or {}).items():
        kind = spec.get("type", "any")
        note = spec.get("description", "")
        yield f"{name}: {kind}" + (f" — {note}" if note else "")
