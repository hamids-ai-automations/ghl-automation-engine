---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a60f8e46820ea466ef0868a20e79d739_68fd6d38e5e411f184bd52540072b1e8
    ReservedCode1: ontaP3HgIpIANWKjA22IqSKCZm9NOp0MNSKNJNEYtOOgwzfgKgfICDzSkMtNXiN2txwh/GrhccadS65ujZkCtbY4vPS1E6v9aROQrN/A5GqcYwaVQ9NhlYyq0PLh8LqefY+RAGLQiz52zQziAy0v+T5Z9ggS62bn0UNN6Pt3CG3JAgO/jcd1FUN3g2E=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a60f8e46820ea466ef0868a20e79d739_68fd6d38e5e411f184bd52540072b1e8
    ReservedCode2: ontaP3HgIpIANWKjA22IqSKCZm9NOp0MNSKNJNEYtOOgwzfgKgfICDzSkMtNXiN2txwh/GrhccadS65ujZkCtbY4vPS1E6v9aROQrN/A5GqcYwaVQ9NhlYyq0PLh8LqefY+RAGLQiz52zQziAy0v+T5Z9ggS62bn0UNN6Pt3CG3JAgO/jcd1FUN3g2E=
---

# ADR-001: Monorepo, mock-first, standard library only

- Status: accepted
- Date: 2026-11-21
- Deciders: maintainer

## Context

The HighLevel integration space already has a 927-tool MCP server with 128 stars.
A 928th tool is not the gap. The gap is that existing integrations fail silently:
truncated pagination reads pass as complete, chrome refresh stampedes revoke
tokens, and nobody can write tests because every test needs a live sub-account.

Three decisions had to be made before writing code.

## Decision 1: One repository, two packages

`ghl-client` (the client) and `ghl-mock` (the simulator) live in one repository
but are separate importable packages.

Rationale: they are versioned together on purpose. The mock encodes the contract
the client is tested against; separate repositories would let them drift and the
tests would then prove nothing. They remain separate packages so a consumer can
install the client without the mock.

## Decision 2: Mock first, client second

The mock server and its fault-injection endpoints are written before the client
methods that call them.

Rationale: the contract becomes executable. Every client behaviour that is claimed
in the README has a corresponding scenario the mock can reproduce. This is also
why the mock deliberately supports mid-pagination failure: that scenario is the
core product claim, so it must be reproducible in one line.

## Decision 3: Standard library only

No `requests`, no `httpx`, no vendor SDK. `urllib.request` for HTTP,
`http.server` for the mock, `pytest` for tests.

Rationale:

- The vendor SDK brings its own retry and auth behaviour that would conflict with
  the explicit budgeting and single-flight refresh implemented here.
- `git clone && python -m pytest` must work with zero installs, otherwise
  contributors and CI both need environment assembly before proving anything.
- The retry, rate-limit, and pagination logic is the product. Hiding it behind a
  library wrapper would make the interesting code invisible.

Trade-off accepted: no connection pooling and no HTTP/2 out of the box. If
throughput ever matters, a `Transport` implementation backed by `httpx` can be
injected without touching the layers above it.

## Consequences

- Test suite runs offline, in seconds, on Windows and Linux, with no credentials.
- CI needs no Docker and no secrets, so pull requests from forks can run the full
  suite.
- The client is the only thing that needs an HTTP implementation swap to scale,
  and that seam is a single class.

## Rejected alternatives

- **Extend the existing 927-tool MCP server.** Its contract is "many tools"; the
  work required is "few, governed, honest tools". Different product.
- **Wrap the official SDK and add retries.** Does not address pagination
  honesty, and inherits an opaque transport.
- **Test against a live sandbox sub-account.** Requires credentials in CI, makes
  the suite non-deterministic, and still cannot reproduce a mid-page 500 on demand.
*（内容由AI生成，仅供参考）*
