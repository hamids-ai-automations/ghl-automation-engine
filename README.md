---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a60f8e46820ea466ef0868a20e79d739_64158b3be5e411f184bd52540072b1e8
    ReservedCode1: ADkDg75IRrLxsmT7VnbMUurc8BBlgxLfJaAPJHOg7LWEW+ueKuioY8QFZATuWTNEoJRqsGNUWG3hepfwmbLmL0XuCNH64gdvihEnI1zsBxixRDIeuyfWuZ+sGc9Or4GFJdx0SR/5NygP6xaEPWOnfr1LmPFUBGE20oVc7hSW4hLlshTWaPLYF31SDDQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a60f8e46820ea466ef0868a20e79d739_64158b3be5e411f184bd52540072b1e8
    ReservedCode2: ADkDg75IRrLxsmT7VnbMUurc8BBlgxLfJaAPJHOg7LWEW+ueKuioY8QFZATuWTNEoJRqsGNUWG3hepfwmbLmL0XuCNH64gdvihEnI1zsBxixRDIeuyfWuZ+sGc9Or4GFJdx0SR/5NygP6xaEPWOnfr1LmPFUBGE20oVc7hSW4hLlshTWaPLYF31SDDQ=
---

# ghl-automation-engine

HighLevel (GoHighLevel) integration engine for teams that put automations in
production and cannot afford them to be quietly wrong.

The GoHighLevel ecosystem already has a 900-tool MCP server. Tool *count* is not
the bottleneck. Reliability is. This project is deliberately narrow: it makes the
failure modes visible instead of silent.

## The four failures this exists to prevent

| Failure | What it looks like in production | What this library does |
| --- | --- | --- |
| Silent pagination truncation | Page 2 of 5 fails, the caller returns page 1, a downstream workflow treats 100 contacts as "all contacts" and archives the rest | Cursor walking is exhaustive by contract: you get a `Collection` with an honest `complete` flag, or a `GhlPartialResultError` carrying what was collected |
| Refresh-token stampede | 12 concurrent workers see an expired token, all refresh, the provider rotates the refresh token, 11 calls return `invalid_grant`, the integration hard-fails | Single-flight refresh under a lock with a double-checked expiry, and the rotated token is persisted before the lock is released |
| PII in logs | A debug line logs the contact upsert body and every email address lands in the log aggregator | Recursive redaction of credentials, emails, and phone numbers before any log record is emitted |
| Untestable integrations | Every test needs a live sub-account and a private token, so there are no tests | An offline mock server with fault injection; the whole suite runs on a clean machine in seconds |
| Webhook replays processed twice | A captured delivery is resent, or a platform retry arrives after a partial write, and the contact is created or the invoice is paid twice | Signature + timestamp window + an atomic seen-set; a duplicate returns `200` with `process=false`, so the sender stops retrying and your code does not run again |

## Install

```bash
git clone <this repo>
cd ghl-automation-engine
python -m pytest
```

No dependencies. The runtime uses only the standard library, so `pytest` is the
only thing you need, and CI needs no Docker and no network.

## Quickstart

```python
from ghl_client import ContactsResource, GhlClient, PrivateIntegrationToken

client = GhlClient(
    PrivateIntegrationToken(token="pit-..."),
    location_id="loc_...",
)

contacts = ContactsResource(client)
result = contacts.search(query="@example.com")

if not result.complete:
    raise SystemExit(f"refusing to act on {len(result)} of an unknown total")

for contact in result.items:
    ...
```

Idempotent writes:

```python
# Replaying this call does not create a duplicate contact.
contacts.upsert({"email": "jane@example.com"}, idempotency_key="sync-run-2026-11-21")
```

## Webhooks

```python
from ghl_webhooks import Receiver

receiver = Receiver(["whsec_..."])

def handle_delivery(headers, raw_body):
    intake = receiver.handle(headers, raw_body)
    if intake.should_process:
        dispatch(intake.event)      # a real, first-time, verified event
    return intake.http_status, {"status": intake.status.value}
```

Three things this does that a naive handler does not:

* **Order matters.** Nothing is parsed until the signature verifies, and no event id is
  claimed until every other check has passed — so an attacker cannot burn event ids to
  block genuine deliveries.
* **Duplicates are visible.** A repeat delivery answers `200` with `process=false`. The
  sender stops retrying; your application does not re-run. "Received" and "acted on" are
  different questions and both get an answer.
* **Unknown event types are kept.** An unrecognised type is delivered with
  `event.is_known == False` and its raw body intact. Silently discarding it is how an
  integration appears healthy while ingesting nothing.

Signature framing (header names, hex vs base64, what exactly is signed) is configuration
rather than a guess, because getting it wrong silently rejects real traffic. See
`docs/adr/ADR-002-webhook-signature-framing.md` for the default assumption and how to
confirm it against one captured delivery.

Run the intake demo — no network, no credentials:

```bash
python examples/demo_webhooks.py
```

## Governed MCP server

```bash
export GHL_PIT_TOKEN=pit-...      export GHL_LOCATION_ID=loc_...
export GHL_MCP_MODE=read-write    # default is read-only
export GHL_MCP_APPROVAL_SECRET=... # required before any write will run
ghl-mcp                            # JSON-RPC over stdio
```

Five tools: `get_contact`, `search_contacts`, `upsert_contact`, `rate_limit_status`,
`server_governance`. Scope is narrow on purpose — a tool that returns a plausible empty
result is worse than a tool that is absent, so the unimplemented ones are listed with
reasons instead of being faked.

The part that matters is the interceptor every call passes through, in this order:

1. **Mode.** Read-only unless the operator turned writes on. A refusal is `read_only_mode`,
   never a `200` with an empty body.
2. **Schema.** Unknown properties are rejected, so there is no `locationId` to argue about —
   cross-account access is not a policy, it is a shape that does not exist.
3. **Location.** The server is bound to one sub-account; an unbound client gets
   `cross_location` rather than a default location.
4. **Approval.** A write needs a single-use, short-lived token issued for *that tool and
   those exact arguments*. A token for one contact cannot be spent on another.
5. **Budget.** Session ceilings stop a loop before the platform rate limiter or the invoice
   does.

```bash
# the human approves one specific change
ghl-mcp approve --tool upsert_contact \
  --args '{"email":"ada@example.com","first_name":"Ada"}'
```

`upsert_contact` also takes `dry_run: true`, which returns the payload it *would* send and
touches nothing. `server_governance` reports the active policy, the PII allowlist, and the
blocked-tool list. Without `GHL_PIT_TOKEN` the server exits `2` and says why, instead of
starting up to answer 401s that an agent would narrate as "no contacts found".

Full detail: [`docs/MCP.md`](docs/MCP.md).

## Offline development

```bash
python -m ghl_mock --port 8080
python examples/demo_offline.py     # full round trip against the mock
```

The mock can be told to fail on purpose, which is the point:

```bash
curl -X POST localhost:8080/__admin/inject \
  -H 'content-type: application/json' \
  -d '{"path": "/contacts/search", "status": 429, "times": 1, "retry_after": "2"}'
```

## Architecture

```
src/ghl_client/
  errors.py       typed hierarchy; retryable is a property of the error, not the caller
  ratelimit.py    sliding-window budget, 90 req / 10 s (platform allows 100)
  retry.py        Retry-After (seconds and HTTP-date), capped at 120 s, full jitter
  transport.py    the only module that touches a socket; injectable
  auth.py         private tokens + OAuth with single-flight refresh and persistence
  pagination.py   exhaustive cursor walking with loop detection
  client.py       versioning, budgeting, redaction, request ids
  resources/      contacts (search, idempotent upsert)
src/ghl_mock/
  store.py        in-memory contacts with email/phone dedupe and cursor paging
  app.py          stdlib HTTP server + fault injection admin endpoints
src/ghl_mcp/
  server.py       JSON-RPC over stdio; call_tool is the only path to a handler
  governance.py   mode, approval tokens bound to arguments, session budget, location guard
  tools.py        five tools, each declaring scope, risk, endpoint and dry-run support
  registry.py     tool specs; carries requires_location per tool
  schema.py       strict JSON Schema subset: unknown keywords raise instead of being ignored
  projection.py   PII allowlist and field truncation for anything a model will read
  audit.py        one JSON line per call: digests, decision, duration — never the arguments
src/ghl_webhooks/
  signature.py    HMAC verification; header/encoding/prefix/signed-payload are configuration
  replay.py       timestamp window + atomic seen-set behind a one-method store protocol
  events.py       event parsing that preserves unknown types instead of dropping them
  receiver.py     ordered intake: size -> signature -> timestamp -> parse -> claim
src/ghl_attribution/
  mapping.py      required field keys, live id binding, drift as a startup error
  events.py       webhook body -> touch; "no campaign signal" is a first-class answer
  policy.py       which fields may be written: blanks + activity time, credit preserved
  ledger.py       event claim store (in-process default, protocol for a durable one)
  engine.py       discovery -> claim -> compare -> upsert; dry run returns the delta
```

Design decisions worth defending:

* **Mock before client.** The contract is written down as executable mock behaviour
  first, so the client is developed against a specification rather than against
  whatever the live API happened to return at 3 a.m.
* **Errors carry context.** Status, method, path, request id, and location id are
  attached at the point of failure, so a support ticket does not need a repro.
* **No dependency on the platform's SDK.** The SDK pulls its own HTTP stack and
  its own opinions about retries; both would fight with the retry and budget logic
  above.
* **A dropped connection is a dataset, not a surprise.** `urllib` wraps neither
  `ECONNRESET` nor Windows' `WSAECONNABORTED`, so the transport types them itself: every
  socket-level failure becomes a retryable `GhlTransportError` instead of a bare `OSError`
  that a calling workflow has no reason to handle.

## Attribution engine

The server is only half of it: leads arrive from forms, quizzes, and landing
pages, and the reason to send them to GoHighLevel is attribution. `ghl_attribution`
does that without the two failure modes that make attribution worthless — a lead
re-credited on every later visit, and one lead in the CRM three times over.

```python
from ghl_attribution import AttributionEngine

engine = AttributionEngine(client)                 # resolves custom field ids at startup
result = engine.ingest({                            # webhook body, verbatim
    "event_id": "evt-9f2",
    "email": "lead@example.com",
    "utm_source": "google",
    "utm_medium": "cpc",
    "utm_campaign": "spring",
    "occurred_at": "2026-11-20T10:00:00Z",
})
result.status        # IngestStatus.APPLIED
result.applied       # ("utm_source", "utm_medium", "utm_campaign", "first_touch_at", "last_touch_at")
```

* **Credit is append-only.** A later touch from another source advances
  `last_touch_at` and nothing else. `utm_source=direct` from a returning lead
  cannot erase the campaign that paid for them.
* **Two idempotency layers.** The event claim stops a redelivered webhook, the
  client's upsert ledger stops a duplicate contact after a failed retry, and the
  claim is released on failure so a retry is the same operation.
* **Refuses to start on schema drift.** Missing, retyped, or duplicated custom
  fields raise `AttributionMappingError` with every problem listed — before the
  first write, not after the reports break.
* `dry_run=True` returns the exact field payload without writing.

Full detail, including the required field keys: [docs/ATTRIBUTION.md](docs/ATTRIBUTION.md).

A worked case runs offline — seven relay deliveries (two of them redeliveries), a
later touch, a direct revisit, a dry run, and a mid-flight schema drift:

```bash
python examples/case_lead_attribution.py
```

It ends with two contacts, the paid-search credit intact, and the drift refused
before the first write. Write-up: [docs/CASE-STUDY-ATTRIBUTION.md](docs/CASE-STUDY-ATTRIBUTION.md)
— figures that need a real deployment are marked `TBD`, not guessed.

## Conversation triage

`ghl_triage` starts where a messaging bot usually goes wrong: it decides **what
kind of message this is and whether a human must take it**, before anything is
drafted. Eight intents, and every one of them states what it is *not*.

```python
from ghl_triage import Intent, Entity, decide

plan = decide(
    Intent.APPOINTMENT,
    0.93,
    text="Can we move it to later next week?",
    present_entities=[],          # no date was given
)
plan.escalate        # True
plan.queue           # 'human_review'
plan.reasons         # (EscalationReason.MISSING_ENTITY,)
plan.auto_reply_allowed   # False — always, even when the message is clean
```

Escalation reasons, in fixed precedence: `classifier_error`, `empty_message`,
`human_only_intent` (billing / opt-out / unclear), `human_requested`,
`missing_entity`, `low_confidence` (below `0.55`), `repeated_contact` (3+ in a
thread), `already_escalated`. Classification that fails at all goes through
`fail_closed()`, which can only report `classifier_error` or `empty_message` —
a failure is never dressed up as a confident routing decision.

Taxonomy, the full escalation table and the failure contract:
[docs/TRIAGE.md](docs/TRIAGE.md).

## Reporting warehouse

`ghl_warehouse` answers the question a sync usually gets wrong: **what happens when
the same data arrives twice?** Nothing — a payload whose tracked attributes hash to
the current version is not a new version, so the dimension records change instead of
polling frequency.

```python
from ghl_warehouse import (
    InMemoryWarehouse, JsonFileWatermarkStore, IncrementalContactExtractor,
)

extractor = IncrementalContactExtractor(
    contacts_resource, InMemoryWarehouse(), JsonFileWatermarkStore("state/watermarks.json")
)

plan = extractor.run(dry_run=True)   # no writes, mark untouched
stats = extractor.run()              # load, then advance
stats.new_versions, stats.unchanged, stats.pages
```

The same contract carries the **opportunity grain**: one type-2 fact per deal, where a
stage move is a new version and the previous row is closed at the observed timestamp.
`InMemoryOpportunityWarehouse.open_value()` sums current open deals only, so a funnel
number has one definition. Unknown statuses and non-numeric `monetaryValue` are
rejected rather than coerced — a load that fails loudly beats a funnel that grows an
"other" bar.

The load order is the design: read the mark, walk every page (a partial walk raises
`IncompleteExtractError` and loads nothing), load rows, and only then advance the
mark — as far as what was *loaded*, never what was *seen*. Watermarks are monotonic
and reads are overlapped by 300s, because commits do not arrive in timestamp order.

Schema, load rules, failure table and the client-side cost disclaimer:
[docs/REPORTING.md](docs/REPORTING.md).

## Status

Built and tested:

* typed errors, sliding-window rate budget, retry with `Retry-After`
* OAuth single-flight refresh with file persistence
* exhaustive cursor pagination with partial-result signalling
* contacts search and idempotent upsert
* offline mock server with fault injection (429 / 5xx / auth rejection)
* webhook intake: HMAC verification, timestamp window, replay seen-set, unknown-type preservation
* governed MCP server: read-only default, argument-bound single-use approvals, `dry_run`,
  location guard, session budget, digest-only audit log, blocked tools declared not faked
* attribution engine: append-only first-touch credit, event claim + upsert ledger,
  custom-field discovery with drift refusal, dry-run plans
* attribution case study: reproducible demo + page, invariants pinned by tests
* conversation triage: 8-intent taxonomy with stated exclusions, escalation policy
  with recorded reasons and fixed precedence, fail-closed classification,
  drafts-only by construction (`auto_reply_allowed` is never true)
* reporting warehouse: star schema validated at import, type-2 contact dimension
  keyed on a payload hash, monotonic watermarks advanced only after a load,
  interrupted and replayed runs proven idempotent by tests
* opportunity fact at deal grain: stage moves versioned, stale payloads ignored,
  open value defined once, mock endpoints for `/opportunities` covered end to end
* durable warehouse sink: `SqliteWarehouse` keeps the reference sink's contract in a file, one transaction per record, one current row per key enforced by index, and a manifest whose digest is reproducible for identical data
* snapshot CLI: `ghl-snapshot write` records the warehouse manifest to a file, `ghl-snapshot diff` exits 1 on any difference, and identical data produces byte-identical snapshots
* 377 tests, no network, no credentials
* `python examples/demo_snapshot.py` shows identical loads producing byte-identical snapshots

Not built yet, on purpose — in this order:

1. live smoke tests behind an opt-in environment flag
2. durable replay store adapter (Redis/Postgres) beyond the in-process default
3. multi-touch weighting (first touch is the only model implemented today)
4. the triage classifier and its labelled evaluation set (GHL-023 / GHL-024) —
   until that harness reports accuracy, cost and latency, no accuracy number is
   quoted for triage anywhere
5. conversation / attribution facts and a durable SQL sink for the warehouse
   (GHL-027); contacts and opportunities are the two grains today, and the
   in-memory sinks are reference implementations, not storage

## License

MIT. See [LICENSE](LICENSE).
*（内容由AI生成，仅供参考）*
