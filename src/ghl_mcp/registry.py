"""Tool registry: what this server exposes, and what it deliberately does not.

The registry is the single source of truth for the surface. ``BLOCKED`` exists because
the roadmap asks for 25 tools and the client can currently back five of them; the gap is
published here rather than filled with handlers that would invent an endpoint. A tool
that returns a plausible shape for an API call that never happened is worse than a
missing tool: the missing tool produces an error, the invented one produces damage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .errors import ToolNotFound
from .governance import READ, WRITE

Handler = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    scope: str
    risk: str
    endpoint: str
    handler: Handler
    supports_dry_run: bool = False
    requires_location: bool = True

    def public(self) -> dict[str, Any]:
        """The MCP ``tools/list`` entry. Note the governance metadata is travel-safe."""

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "annotations": {
                "readOnlyHint": self.scope == READ,
                "destructiveHint": False,
                "idempotentHint": self.scope == READ or self.supports_dry_run,
                "risk": self.risk,
                "scope": self.scope,
            },
        }


@dataclass
class ToolRegistry:
    _specs: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.scope not in (READ, WRITE):
            raise ValueError(f"tool {spec.name!r} has invalid scope {spec.scope!r}")
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise ToolNotFound(
                f"unknown tool {name!r}",
                available=sorted(self._specs),
            ) from None

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def names(self) -> list[str]:
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


#: Roadmap tools that are named but not implementable yet, with the reason. Kept in code
#: so the server can report its own gaps over the wire instead of a doc drifting away.
BLOCKED: tuple[dict[str, str], ...] = (
    {
        "name": "update_contact",
        "blocked_by": "no PUT /contacts/{id} route in the mock and no method on ContactsResource",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "list_contact_notes",
        "blocked_by": "no /contacts/{id}/notes endpoint in the mock",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "list_contact_tasks",
        "blocked_by": "no /contacts/{id}/tasks endpoint in the mock",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "add_note",
        "blocked_by": "no /contacts/{id}/notes POST route in the mock",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "add_tags",
        "blocked_by": "tag mutation is not a mock route; upsert already carries tags, so a "
        "separate tool would be a second path to the same field",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "remove_tags",
        "blocked_by": "no tag-removal route; removal must not be emulated by rewriting the "
        "tag list, which would silently drop concurrent additions",
        "unblocks_in": "P2 mock extension",
    },
    {
        "name": "update_opportunity / conversations / calendars / payments / workflows",
        "blocked_by": "out of scope until the client has resources for those domains; "
        "the roadmap's 14 unnamed read tools fall here rather than being invented",
        "unblocks_in": "P4-P6",
    },
)
