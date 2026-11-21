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

## Status

Built and tested:

* typed errors, sliding-window rate budget, retry with `Retry-After`
* OAuth single-flight refresh with file persistence
* exhaustive cursor pagination with partial-result signalling
* contacts search and idempotent upsert
* offline mock server with fault injection (429 / 5xx / auth rejection)
* 45 tests, no network, no credentials

Not built yet, on purpose — in this order:

1. webhook receiver with signature verification and replay protection
2. MCP tool surface with a governance layer (scope, dry-run, audit log)
3. live smoke tests behind an opt-in environment flag

## License

MIT. See [LICENSE](LICENSE).
*（内容由AI生成，仅供参考）*
