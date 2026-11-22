---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: a60f8e46820ea466ef0868a20e79d739_59697ccce5f011f184bd52540072b1e8
    ReservedCode1: yyRTeocXxOJPfVOAncb7cgH1EwrwjA0bo9lxd4u6yYWlwgOYcf+PTAu6RSaj+eJkluqHzKuQ7adZ7NYxR74vSkIJq+GXsVNdpDDVq6ZabDmF52L/A1TTTRqnZi+IG3PjnZjhFDkFlAnKEiqCPU9XArb2LeXE+lmjxBrCTf5Rl3e1p/8Q0DXrndkmEaI=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: a60f8e46820ea466ef0868a20e79d739_59697ccce5f011f184bd52540072b1e8
    ReservedCode2: yyRTeocXxOJPfVOAncb7cgH1EwrwjA0bo9lxd4u6yYWlwgOYcf+PTAu6RSaj+eJkluqHzKuQ7adZ7NYxR74vSkIJq+GXsVNdpDDVq6ZabDmF52L/A1TTTRqnZi+IG3PjnZjhFDkFlAnKEiqCPU9XArb2LeXE+lmjxBrCTf5Rl3e1p/8Q0DXrndkmEaI=
---

# ADR-002: Webhook signature framing is configuration, not a guess

- **Status:** Accepted
- **Date:** 2026-11-22
- **Context:** GHL-012, webhook receiver

## Context

HighLevel signs webhook deliveries with an HMAC over the request body. What the docs and
the delivered payloads do not pin down, across marketplace apps and API versions, is:

- which header carries the signature (`X-GHL-Signature`, `X-WH-Signature`, others)
- whether the timestamp is a separate header or inside the body
- what exactly is signed: the body alone, `timestamp + "." + body`, or something else
- whether the digest is hex or base64
- whether the header value carries a `sha256=` prefix

Hard-coding one answer produces a receiver that looks correct and quietly rejects every
real delivery — or, worse, accepts unsigned ones while "waiting for the header". Guessing
in a security boundary is the specific failure this library exists to prevent.

## Decision

Signature framing is a :class:`SignatureConfig` value, not a constant:

```python
SignatureConfig(
    header="x-ghl-signature",
    timestamp_header="x-ghl-timestamp",
    algorithm="sha256",
    encoding="hex",
    prefix="",
    payload_template="{timestamp}.{body}",
)
```

Consequences of that choice:

1. **The default is documented as an assumption.** The module docstring says so and this
   ADR is the record. It is not presented as verified fact.
2. **Several configs can be live at once.** A deployment can accept two framings during
   rollout, observe which one matches, then narrow to it.
3. **The matched config is reported.** :class:`Intake` carries `matched_config`, so an
   operator can tell *which* framing actually verified instead of assuming.
4. **A timestamp header must be signed.** Config validation rejects a timestamp header
   whose value is not part of the signed payload (`payload_template` must contain
   `{timestamp}` when `timestamp_header` is set). An unsigned timestamp is attacker
   controlled and would let a replay extend its own window.
5. **The algorithm label is stripped but the digest is still checked.** `sha256=` and any
   configured prefix are removed before decoding; verification then compares digests with
   `hmac.compare_digest`. Stripping a label never weakens the comparison.

## How to settle the assumption against reality

Capture one real delivery (a HighLevel test webhook is enough) and try each config in
`ghl_webhooks.signature` against the captured headers and body. The one that verifies is
the truth for that app; delete the others. Until that has been done for a specific
account, treat the default as unverified.

## Rejected alternatives

| Alternative | Why not |
| --- | --- |
| Hard-code one header set | Silently rejects real traffic on a framing mismatch; the failure looks like "webhooks don't work" |
| Skip verification when the header is missing | Turns a missing configuration into an authentication bypass |
| Verify against the parsed JSON re-serialised | Byte differences in spacing or key order break the HMAC for honest senders; the raw body must be signed |
| Accept any framing that somehow passes | Configs are explicit and few; a permissive matcher invites downgrade to the weakest one |
| Use `==` for the digest comparison | Timing leak on a security check; `hmac.compare_digest` costs nothing here |

## Follow-up

- Confirm the framing against one live delivery per marketplace app, then reduce the
  configured set to one.
- If HighLevel later publishes an Ed25519 scheme, add it as another config rather than
  replacing HMAC: existing deployments should not need a flag day.
*（内容由AI生成，仅供参考）*
