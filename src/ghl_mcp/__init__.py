"""``ghl_mcp`` — a HighLevel MCP server that fails closed.

The premise: a 900-tool MCP that will happily describe a contact it never fetched is
worse than a small server that refuses. So the surface is narrow, the default is
read-only, writes need an approval bound to their arguments, every mutation previews, and
a partial read is an error rather than a short list.

Public surface:

``create_server`` / ``build_client_from_env`` / ``main``
    Assemble and run the server; ``python -m ghl_mcp`` speaks MCP over stdio.
``McpServer`` / ``ToolRegistry`` / ``ToolSpec``
    The protocol handler and the tool surface.
``Mode`` / ``ApprovalAuthority`` / ``SessionBudget`` / ``LocationGuard``
    The four governance controls.
"""

from __future__ import annotations

from .audit import AuditLog
from .errors import (
    ApprovalExpired,
    ApprovalInvalid,
    ApprovalRequired,
    ApprovalReused,
    BudgetExhausted,
    CrossLocation,
    InvalidInput,
    McpToolError,
    PartialResult,
    ReadOnlyMode,
    ToolNotFound,
    UpstreamFailure,
)
from .governance import (
    ApprovalAuthority,
    Grant,
    LocationGuard,
    Mode,
    SessionBudget,
    args_digest,
)
from .projection import DEFAULT_CONTACT_ALLOWLIST, project, project_many
from .registry import BLOCKED, ToolRegistry, ToolSpec
from .schema import validate
from .server import McpServer, build_client_from_env, create_server, main
from .tools import build_registry

__all__ = [
    "ApprovalAuthority",
    "ApprovalExpired",
    "ApprovalInvalid",
    "ApprovalRequired",
    "ApprovalReused",
    "AuditLog",
    "BLOCKED",
    "BudgetExhausted",
    "CrossLocation",
    "DEFAULT_CONTACT_ALLOWLIST",
    "Grant",
    "InvalidInput",
    "LocationGuard",
    "McpServer",
    "McpToolError",
    "Mode",
    "PartialResult",
    "ReadOnlyMode",
    "SessionBudget",
    "ToolNotFound",
    "ToolRegistry",
    "ToolSpec",
    "UpstreamFailure",
    "args_digest",
    "build_client_from_env",
    "build_registry",
    "create_server",
    "main",
    "project",
    "project_many",
    "validate",
]
