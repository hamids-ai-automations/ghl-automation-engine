"""Tool errors: every refusal carries a reason an agent can act on.

The rule this package enforces is narrow and absolute: **a tool result never looks
like success when the work did not happen.** A denial, a validation failure and a
partial read all return ``isError: true`` with a machine-readable ``reason``, so an
agent cannot mistake "I was not allowed to do that" for "there was nothing to do".
That distinction is the entire point of this server; see
``docs/adr/ADR-003-governance-first-mcp.md``.
"""

from __future__ import annotations

from typing import Any


class McpToolError(Exception):
    """Base class. Subclasses fix a stable ``reason`` string."""

    reason = "tool_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ToolNotFound(McpToolError):
    """The model asked for a tool this server does not expose."""

    reason = "unknown_tool"


class InvalidInput(McpToolError):
    """Arguments do not match the tool's schema. The messages are written for the model."""

    reason = "invalid_input"


class ReadOnlyMode(McpToolError):
    """A mutating tool was called while the server is in read-only mode."""

    reason = "read_only_mode"


class ApprovalRequired(McpToolError):
    """A mutating tool was called without an approval token."""

    reason = "approval_required"


class ApprovalInvalid(McpToolError):
    """The token does not verify, or was issued for something else."""

    reason = "approval_invalid"


class ApprovalExpired(McpToolError):
    """The token was valid but is past its expiry."""

    reason = "approval_expired"


class ApprovalReused(McpToolError):
    """The token was already spent. One approval covers one call."""

    reason = "approval_reused"


class CrossLocation(McpToolError):
    """Arguments name a sub-account this server is not bound to."""

    reason = "cross_location"


class BudgetExhausted(McpToolError):
    """The session hit its call or write ceiling."""

    reason = "session_budget_exhausted"


class UpstreamFailure(McpToolError):
    """The platform call failed. Nothing was applied."""

    reason = "upstream_failure"


class PartialResult(McpToolError):
    """A read stopped early. The collected rows are attached, and the call is an error."""

    reason = "partial_result"
