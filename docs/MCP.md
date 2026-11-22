# The MCP surface

Five tools, one interceptor, and a rule that decides the design: **a tool call must never
be able to look like it worked when it did not.**

The crowded part of this ecosystem is tool count — there is a published GoHighLevel MCP
server with 927 tools. Tool count is not the bottleneck. Silent wrongness is: an agent
tells a user "I've updated the contact", the call returned 200, and the field it wrote was
the wrong one, or the write never happened because the token was expired. Everything below
exists to make those outcomes impossible or visible.

## The five tools

| Tool | Scope | Location-bound | dry_run | Notes |
| --- | --- | --- | --- | --- |
| `get_contact` | read | yes | — | Allowlisted fields only (see [PII](#pii-and-projection)). |
| `search_contacts` | read | yes | — | Follows every page cursor; returns `partial: true` if it stopped early. |
| `upsert_contact` | write | yes | yes | Approvable, single-use token, idempotency key supported. |
| `rate_limit_status` | read | no | — | Reports the client's local sliding-window budget. |
| `server_governance` | read | no | — | This server's own policy, plus the blocked-tool list. |

`server_governance` is the introspection tool an agent should call first: it returns the
active mode, whether approval is required, the PII allowlist, the remaining session budget,
and every tool that exists in the roadmap but is **not** implemented, with the reason.

## Governance: four controls, one job each

All four run inside `McpServer.call_tool` — the only path from JSON to a handler. There is
no second entry point for handlers to be reached, so there is nothing to bypass.

### 1. Mode — read-only by default

`GHL_MCP_MODE` is `read-only` unless the operator sets `read-write`. An unrecognised value
raises at startup instead of falling back to read-only: failing safe silently would hide the
typo, and the operator would spend an hour debugging refused writes.

In read-only mode a mutating call returns `read_only`, not a 200 with an empty body.

### 2. Approval tokens — bound to the arguments, not to the tool

A write needs a token issued for **that tool and those exact arguments**, single-use and
short-lived. Hashing the arguments into the token is the whole point: the human approved
*this change*, so the token cannot be spent on a different one. A token for
`upsert_contact {email: a@b.com}` does not authorise `upsert_contact {email: c@d.com}`.

```bash
# operator issues (TTL 300s by default)
GHL_MCP_APPROVAL_SECRET=... ghl-mcp approve \
  --tool upsert_contact \
  --args '{"email":"ada@example.com","first_name":"Ada"}'

# the agent then calls with the token inside the arguments
```

Refusal reasons are distinct, because "it failed" is not actionable:
`approval_required`, `approval_invalid` (wrong tool or different arguments),
`approval_expired`, `approval_reused`.

On reuse: a spent nonce is remembered until its own expiry, so a replay inside the window is
`approval_reused` rather than a second write.

"Approval" here is HMAC over a canonical JSON body — no cryptography dependency, and no
shared secret means no tokens are accepted at all (`ApprovalAuthority` refuses an empty
secret rather than validating every forged token).

### 3. dry_run — the write that is not a write

`upsert_contact` accepts `dry_run: true`. The handler builds the payload it *would* send,
returns it with `dry_run: true`, and never touches the transport. The response is
deliberately shaped like a success minus the side effect, so an agent can show a user
exactly what it is about to change.

A dry run needs no approval and is not a write for budget purposes — it is the call an agent
should reach for first, because it answers "what would change" without an operator in the
loop. It is still a call: the session ceiling counts it, so a dry-run loop still terminates.

### 4. Location guard — one sub-account, structurally

The server is bound to a single location from `GHL_LOCATION_ID`. Location is **not** a tool
argument: there is no `locationId` in any input schema, so cross-account access is not a
policy that can be argued with, it is a shape that does not exist. Passing `locationId`
anyway is `invalid_input` (the schema rejects unknown properties). A client with no bound
location refuses location-scoped calls as `cross_location`; local status tools still work.

### Plus: session budget

`GHL_MCP_MAX_CALLS` (default 200) and `GHL_MCP_MAX_WRITES` (default 20) stop a runaway loop
at a ceiling instead of letting the platform's rate limiter or the invoice do it.

## Honest failures

Every failure carries a machine-readable reason and a message a human can act on:

| reason | meaning |
| --- | --- |
| `invalid_input` | Schema violation; `details.problems` lists each problem by field. |
| `read_only_mode` | Mutating call while the server is read-only. |
| `approval_required` / `approval_invalid` / `approval_expired` / `approval_reused` | See above. |
| `cross_location` | Tool needs a bound sub-account and the client has none. |
| `session_budget_exhausted` | Session ceiling reached. |
| `partial_result` | A paginated read stopped early; collected rows ride along, flagged incomplete. |
| `upstream_failure` | The platform answered with an error; error type and `applied: false` included. |
| `unknown_tool` | No such tool (absence is an error, never an empty success). |
| `tool_error` | Base refusal for anything else; still `isError`, never silent. |

The failure shape is `{isError: true, structuredContent: {error: {reason, message, details?}}}`.
An agent that ignores `isError` still cannot mistake a refusal for data, because there is no
data field to read. A 404 upstream stays `upstream_failure` with `applied: false`; a fetch
that returns an empty body is also a failure, not an empty contact.

`search_contacts` reports completeness explicitly: `complete: true|false` plus a
`truncated_reason`. When the transport fails mid-pagination the call itself is
`partial_result` and carries the rows collected so far. Partial data is returned with the
flag rather than thrown away — and rather than passed off as exhaustive.

## PII and projection

Read tools project through an allowlist of contact fields (`DEFAULT_CONTACT_ALLOWLIST`) and
drop everything else. Free-text fields are truncated. This is a privacy measure and a
context-window measure at once: 400 custom fields per contact is not something to hand to a
model by default.

`server_governance` reports the exact allowlist in force, so "which fields can a model see"
is answerable without reading the source.

## Audit log

`GHL_MCP_AUDIT_PATH` writes one JSON object per line:

```json
{"ts": 1763800000.1, "call_id": "8f1c2a9b4d0e", "tool": "upsert_contact", "scope": "write", "mode": "read-write", "decision": "ok", "args_hash": "9f2c…", "result_hash": "1a3b…", "duration_ms": 12}
```

`args_hash` is the same canonical digest the approval token binds to, and arguments
themselves are **not** logged — the audit proves what was called without becoming a second
copy of the CRM. Refusals are logged too, with the reason in `decision`.

## Blocked tools, stated out loud

The roadmap's larger tool list is not implemented, and the gaps are declared rather than
faked. `server_governance` returns entries such as:

- `remove_tags` — no tag-removal route; removal must not be emulated by rewriting the tag
  list, which would silently drop concurrent additions.
- `update_contact` — no `PUT /contacts/{id}` route in the mock and no method on the resource.
- conversations / calendars / payments / workflows — out of scope until the client has
  resources for those domains.

A tool that returns a plausible empty result is worse than a tool that is absent. Absent,
the agent asks; plausible, it reports.

## Running it

```bash
pip install -e .
export GHL_PIT_TOKEN=...          # Private Integration token
export GHL_LOCATION_ID=...        # the bound sub-account
export GHL_MCP_MODE=read-write    # default: read-only
export GHL_MCP_APPROVAL_SECRET=... # required for writes
ghl-mcp                            # JSON-RPC on stdio, one line per message
```

Without `GHL_PIT_TOKEN` the server exits 2 and says so. It does not start and answer 401s,
because an agent will narrate a 401 as "no contacts found".

Against the mock, with no credentials:

```bash
ghl-mock --port 8080 &   # default port is 8080
GHL_API_BASE_URL=http://127.0.0.1:8080 GHL_PIT_TOKEN=test-token \
GHL_LOCATION_ID=loc_test GHL_MCP_MODE=read-write ghl-mcp
```

`python -m ghl_mcp` is equivalent to `ghl-mcp`.

## Known limits

- **Process-local state.** Used nonces and budget counters live in memory. A multi-process
  deployment needs a shared store — same follow-up as the webhook replay guard (ADR-002).
- **stdio only.** No HTTP transport, deliberately: a transport is attack surface and this is
  a local-trust story.
- **Five tools.** Scope is honest rather than wide; see the blocked list.
- **Approval tokens are issued by an operator**, not by the agent. An agent that can mint its
  own approvals has no approvals.
