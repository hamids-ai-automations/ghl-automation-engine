"""The tool surface itself.

Five tools, not twenty-five. Each one is backed by a route that exists in the mock and a
method that exists on ``ContactsResource``; the rest of the roadmap's surface is listed in
``registry.BLOCKED`` with the reason it is not here. See
``docs/mcp-tool-surface.md`` for the per-tool table and the honest count.

Design notes that matter more than the code:

* ``upsert_contact`` is the only write, and it is idempotent by construction — the client
  ledger keys on email/phone, so a replayed call returns the original contact instead of
  creating a twin. A write tool whose retry creates duplicates is an incident generator.
* ``dry_run`` on the write returns the diff it *would* apply and mutates nothing. It is
  allowed in read-only mode on purpose: it makes no mutation, and an agent that can preview
  is an agent that does not need write access to answer a question.
* Reads report ``complete``. A search that stopped at a limit says so; it does not return a
  short list and let the model conclude the contact does not exist.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ghl_client import (
    ContactsResource,
    GhlClient,
    GhlError,
    GhlPartialResultError,
)

from .errors import PartialResult, UpstreamFailure
from .governance import READ, WRITE
from .projection import (
    DEFAULT_CONTACT_ALLOWLIST,
    omitted,
    project,
    project_many,
    redaction_note,
)
from .registry import BLOCKED, ToolRegistry, ToolSpec

_SEARCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Free-text match on name, email or phone. Omit to page all contacts.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "Maximum contacts to return. A limited read reports complete=false.",
        },
    },
    "additionalProperties": False,
}

_GET_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "contact_id": {"type": "string", "description": "HighLevel contact id."}
    },
    "required": ["contact_id"],
    "additionalProperties": False,
}

_UPSERT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "description": "Contact email. Email or phone is required."},
        "phone": {"type": "string", "description": "Contact phone in E.164. Email or phone is required."},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 50,
            "description": "Tags to attach. Existing tags are preserved by the platform.",
        },
        "idempotency_key": {
            "type": "string",
            "description": "Explicit replay key. Defaults to a digest of email/phone.",
        },
        "dry_run": {
            "type": "boolean",
            "description": "Preview only. Returns the create/update decision and the fields "
            "that would change; writes nothing and needs no approval.",
        },
    },
    "additionalProperties": False,
}

_EMPTY_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _payload_value(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        contact = payload.get("contact")
        if isinstance(contact, Mapping):
            return contact
        return payload
    return {}


def _limiter_value(limiter: Any, name: str) -> Any:
    """The limiter exposes these as properties; accept a method too, but never invent a value."""

    value = getattr(limiter, name, None)
    return value() if callable(value) else value


def _wrap_upstream(exc: GhlError, action: str) -> UpstreamFailure:
    return UpstreamFailure(
        f"{action} failed: {exc}",
        error_type=type(exc).__name__,
        applied=False,
    )


def build_registry(
    client: GhlClient,
    *,
    contacts: ContactsResource | None = None,
    allowlist: Sequence[str] = DEFAULT_CONTACT_ALLOWLIST,
) -> ToolRegistry:
    resource = contacts or ContactsResource(client)
    registry = ToolRegistry()

    def search_contacts(arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            collection = resource.search(
                query=arguments.get("query"),
                limit=arguments.get("limit"),
            )
        except GhlPartialResultError as exc:
            rows, dropped = project_many(exc.collected, allowlist)
            raise PartialResult(
                f"search stopped after {len(exc.collected)} contact(s); the result set is "
                "incomplete and must not be treated as exhaustive",
                collected=rows,
                errors=[str(err) for err in exc.errors],
                withheld_fields=dropped,
            ) from exc
        except GhlError as exc:
            raise _wrap_upstream(exc, "contact search") from exc

        rows, dropped = project_many(collection.items, allowlist)
        return {
            "contacts": rows,
            "returned": len(rows),
            "complete": collection.complete,
            "pages_fetched": collection.pages_fetched,
            "truncated_reason": collection.truncated_reason,
            "redaction": redaction_note(dropped),
        }

    def get_contact(arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = resource.get(str(arguments["contact_id"]))
        except GhlError as exc:
            raise _wrap_upstream(exc, "contact fetch") from exc
        contact = _payload_value(payload)
        if not contact:
            raise UpstreamFailure(
                "contact fetch returned no record",
                contact_id=arguments["contact_id"],
                applied=False,
            )
        return {
            "contact": project(contact, allowlist),
            "redaction": redaction_note(omitted(contact, allowlist)),
        }

    def upsert_contact(arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not arguments.get("email") and not arguments.get("phone"):
            raise UpstreamFailure(
                "upsert needs an email or a phone: without one the platform cannot "
                "deduplicate, and the call would create a duplicate contact",
                applied=False,
            )

        body: dict[str, Any] = {}
        for source, target in (
            ("email", "email"),
            ("phone", "phone"),
            ("first_name", "firstName"),
            ("last_name", "lastName"),
            ("tags", "tags"),
        ):
            if arguments.get(source) is not None:
                body[target] = arguments[source]

        if arguments.get("dry_run"):
            existing = None
            lookup = (
                resource.search(query=body.get("email") or body.get("phone"), limit=1)
                if (body.get("email") or body.get("phone"))
                else None
            )
            if lookup is not None and lookup.items:
                existing = _payload_value(lookup.items[0])
            changes: dict[str, Any] = {}
            if existing:
                for key, value in body.items():
                    if existing.get(key) != value:
                        changes[key] = {"from": existing.get(key), "to": value}
                action = "update" if changes else "no-op"
            else:
                changes = {key: {"from": None, "to": value} for key, value in body.items()}
                action = "create"
            return {
                "dry_run": True,
                "mutated": False,
                "action": action,
                "would_apply": changes,
                "existing": project(existing, allowlist) if existing else None,
            }

        try:
            result = resource.upsert(body, idempotency_key=arguments.get("idempotency_key"))
        except GhlError as exc:
            raise _wrap_upstream(exc, "contact upsert") from exc

        contact = _payload_value(result)
        return {
            "dry_run": False,
            "mutated": True,
            "created": bool(result.get("new")) if isinstance(result, Mapping) else None,
            "contact": project(contact, allowlist),
            "redaction": redaction_note(omitted(contact, allowlist)),
        }

    def rate_limit_status(_: Mapping[str, Any]) -> dict[str, Any]:
        limiter = getattr(client, "limiter", None)
        status: dict[str, Any] = {"source": "client limiter (local, no network call)"}
        if limiter is not None:
            status["capacity"] = _limiter_value(limiter, "capacity")
            status["remaining"] = _limiter_value(limiter, "remaining")
            status["used"] = _limiter_value(limiter, "used")
        return status

    def server_governance(_: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "tools_implemented": registry.names(),
            "tools_blocked": [
                {"name": entry["name"], "blocked_by": entry["blocked_by"]} for entry in BLOCKED
            ],
            "write_path": {
                "requires": ["mode=read-write", "per-call approval token"],
                "dry_run_available": True,
                "dry_run_mutates": False,
            },
            "pii_allowlist": list(allowlist),
            "bound_location": getattr(client, "location_id", None),
            "guarantees": {
                "partial_reads_are_errors": True,
                "write_without_approval_fails_closed": True,
                "cross_location_is_an_error": True,
                "audit_log_holds_digests_only": True,
            },
        }

    registry.register(
        ToolSpec(
            name="search_contacts",
            description=(
                "Search contacts in the bound sub-account. Paginates every cursor and reports "
                "complete=false when a limit stopped the walk. Returns allowlisted fields only."
            ),
            input_schema=_SEARCH_SCHEMA,
            scope=READ,
            risk="low",
            endpoint="POST /contacts/search",
            handler=search_contacts,
        )
    )
    registry.register(
        ToolSpec(
            name="get_contact",
            description="Fetch one contact by id. Returns allowlisted fields only.",
            input_schema=_GET_SCHEMA,
            scope=READ,
            risk="low",
            endpoint="GET /contacts/{id}",
            handler=get_contact,
        )
    )
    registry.register(
        ToolSpec(
            name="upsert_contact",
            description=(
                "Create or update a contact, deduplicated on email/phone. Idempotent on replay. "
                "Pass dry_run=true to preview the change without writing."
            ),
            input_schema=_UPSERT_SCHEMA,
            scope=WRITE,
            risk="high",
            endpoint="POST /contacts/upsert",
            handler=upsert_contact,
            supports_dry_run=True,
        )
    )
    registry.register(
        ToolSpec(
            name="rate_limit_status",
            description="Remaining request budget in the client's local sliding window. No network call.",
            input_schema=_EMPTY_SCHEMA,
            scope=READ,
            risk="low",
            endpoint="(local)",
            handler=rate_limit_status,
            requires_location=False,
        )
    )
    registry.register(
        ToolSpec(
            name="server_governance",
            description=(
                "This server's own policy: mode, approval requirement, PII allowlist, and the "
                "tools that are deliberately not implemented."
            ),
            input_schema=_EMPTY_SCHEMA,
            scope=READ,
            risk="low",
            endpoint="(local)",
            handler=server_governance,
            requires_location=False,
        )
    )
    return registry
