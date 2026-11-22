"""PII projection: contact records are narrowed to an allowlist before a model sees them.

The threat is mundane and specific: a 40-field contact payload goes into a prompt, the
prompt goes to a provider, and nobody intended to send ``customFields``, internal notes,
consent history or the attribution blob that happens to sit alongside the record. The
allowlist is the control; anything not named is dropped, and the caller is told how many
fields were dropped so an empty-looking record cannot be mistaken for a sparse one.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

DEFAULT_CONTACT_ALLOWLIST: tuple[str, ...] = (
    "id",
    "firstName",
    "lastName",
    "email",
    "phone",
    "tags",
    "dateAdded",
    "dateUpdated",
    "locationId",
)

ALWAYS_HIDDEN: frozenset[str] = frozenset(
    {"customFields", "attributions", "lastAttributionSource", "consentHistory"}
)


def project(item: Mapping[str, Any], allowlist: Sequence[str]) -> dict[str, Any]:
    allowed = set(allowlist)
    return {key: value for key, value in item.items() if key in allowed}


def omitted(item: Mapping[str, Any], allowlist: Sequence[str]) -> list[str]:
    allowed = set(allowlist)
    return sorted(key for key in item if key not in allowed)


def project_many(
    items: Iterable[Mapping[str, Any]], allowlist: Sequence[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Project a batch, returning the rows plus the union of dropped field names."""

    rows: list[dict[str, Any]] = []
    dropped: set[str] = set()
    for item in items:
        rows.append(project(item, allowlist))
        dropped.update(omitted(item, allowlist))
    return rows, sorted(dropped)


def redaction_note(dropped: Sequence[str]) -> str:
    if not dropped:
        return "no fields were withheld"
    return f"{len(dropped)} field(s) withheld by the PII allowlist: {', '.join(dropped)}"
