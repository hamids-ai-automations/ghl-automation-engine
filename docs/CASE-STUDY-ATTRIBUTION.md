# Case study — lead attribution that survives a broken relay

**Status:** skeleton, reproducible demo complete. Client-specific figures are
placeholders on purpose (see [Metrics](#metrics)).

**Project:** P4 / Project B — Lead attribution → GoHighLevel
**Tasks:** GHL-019 (mapping + upsert semantics), GHL-020 (idempotent intake),
GHL-021 (this demo and case page)
**Code:** [`src/ghl_attribution`](../src/ghl_attribution),
[`examples/case_lead_attribution.py`](../examples/case_lead_attribution.py)
**References:** [ATTRIBUTION.md](ATTRIBUTION.md) for the field contract and
outcome table.

---

## The problem

A lead-intake relay sits between a website (or Meta/Google lead form) and a
GoHighLevel location. Relays retry. Webhooks get redelivered. Analytics traffic
also arrives on the same pipe — direct visits, bots, internal QA clicks.

The usual integration writes "last click" attribution straight onto the contact
every time it sees a touch. Three things go wrong:

1. **Duplicate contacts.** A retried delivery creates a second contact instead
   of a second field write, so the CRM slowly doubles.
2. **Credit gets stolen by noise.** The lead converts from paid search, then
   reads a newsletter, then revisits directly — and the source flips to
   `direct`. The channel that actually paid for the lead disappears.
3. **Silent breakage on schema drift.** Someone renames or retypes a custom
   field. The integration keeps running and writes timestamps into a text
   field, and nobody notices until a report looks wrong.

## The approach

One intake path, three rules:

| Rule | Implementation | Where |
| --- | --- | --- |
| Credit is written **once** | Existing non-empty attribution is read back and preserved; a later touch only advances `last_touch_at` | `policy.py` |
| One event = at most one write | Event claim keyed by `location:event_id` taken before the write; redelivery returns `DUPLICATE` | `ledger.py`, `engine.py` |
| The write itself is retry-safe | `POST /contacts/upsert` is keyed by email/phone through the client's idempotency ledger | `client.py`, `resources/contacts.py` |
| Schema is verified **before** the first write | Field map is bound to live custom-field ids at startup; missing/retyped/duplicated fields raise `AttributionMappingError` | `mapping.py`, `custom_fields.py` |
| Noise is not recorded | A touch with no campaign signal (`direct`, empty, bots) returns `UNCHANGED` | `events.py`, `engine.py` |
| You can look before you leap | `dry_run=True` returns the exact field delta with zero API writes | `engine.py` |

## The demo (runnable)

```bash
python examples/case_lead_attribution.py
```

It replays one week of relay traffic against the offline mock and prints what
happened. Captured output (fixture data, mock location `loc_case`):

```
## What arrived
| # | delivery | event | status | wrote | fields touched |
| - | --- | --- | --- | --- | --- |
| 1 | first touch (paid search) | evt-2001 | applied | yes | utm_source, utm_medium, utm_campaign, utm_content, landing_page, gclid, first_touch_at, last_touch_at |
| 2 | same event, redelivered | evt-2001 | duplicate | no | — |
| 3 | same event, redelivered again | evt-2001 | duplicate | no | — |
| 4 | later touch (newsletter) | evt-2002 | applied | yes | last_touch_at |
| 5 | direct revisit | evt-2003 | unchanged | no | — |
| 6 | second lead (paid social) | evt-2004 | applied | yes | utm_source, utm_medium, utm_campaign, landing_page, fbclid, first_touch_at, last_touch_at |
| 7 | dry run (new lead) | evt-2005 | planned | no | utm_source, utm_medium, utm_campaign, landing_page, first_touch_at, last_touch_at |

## What the location ended up with
contacts created: 2 · contacts updated: 1 · custom-field writes: 16
ada@example.com  tags=['attribution:google']  first_touch_at: 2026-11-16T09:15:00Z  gclid: Cj0KCQ-paid-click  landing_page: /pricing  last_touch_at: 2026-11-18T18:40:00Z  utm_campaign: spring-launch  utm_content: headline-b  utm_medium: cpc  utm_source: google
grace@example.com  tags=['attribution:meta']  fbclid: IwAR-facebook-click  first_touch_at: 2026-11-19T11:02:00Z  landing_page: /demo  last_touch_at: 2026-11-19T11:02:00Z  utm_campaign: retargeting-q4  utm_medium: paid_social  utm_source: meta

## Schema drift
retype contact.attribution_first_touch_at -> TEXT
engine refused to start: custom field mapping drift (1): first_touch_at [type] expected DATE, found TEXT
recovers after fix: True

## Outcome counts
  applied: 3
  duplicate: 2
  planned: 1
  unchanged: 1
```

Seven deliveries in, five outcomes, two contacts. The lead that arrived from
paid search still reads `utm_source: google` after a newsletter touch and a
direct revisit. The dry run planned six fields and wrote none.

## Before / after

Same seven deliveries, two intake designs:

| Behaviour | Last-click, write-every-touch | First-touch, claim-then-write (this repo) |
| --- | --- | --- |
| Contacts after 7 deliveries | 7 (one per delivery) | 2 (one per lead) |
| `evt-2001` redelivered twice | 2 extra contacts | `DUPLICATE`, no write |
| `utm_source` after newsletter + direct revisit | `direct` | `google` |
| Direct revisit | overwrites source | `UNCHANGED`, nothing written |
| Field retyped by an admin | writes keep flowing into a broken field | engine refuses to start, names the field |
| Preview a write before it happens | not possible | `dry_run=True` returns the delta |

The engine can be told to overwrite instead
(`AttributionPolicy(overwrite_existing=True)`), and then it reports which fields
it replaced in `result.overwritten` — the point is that overriding is a decision
someone makes, not a default.

## Metrics

### The one number that is measured, not modelled

> **16 custom-field writes produced 2 contacts from 7 relay deliveries.**
> Method: `python examples/case_lead_attribution.py` replays the fixture week
> against `ghl_mock`; the counters come from `ContactStore`
> (`created`, `updated`, `custom_field_writes`), and the same invariants are
> asserted by `tests/test_case_attribution.py`. Scope: a mock location, fixture
> data. It is reproducible, not a client result.

> Fill the rest in from a real deployment. No numbers are estimated here, and none
> should be added that cannot be traced to a run, a log line, or an export.

| Metric | Baseline (last-click) | With this engine | How to measure |
| --- | --- | --- | --- |
| Contacts per lead after retries | `TBD` | `TBD` | count of `/contacts` in the location ÷ distinct lead emails |
| Share of leads whose recorded source matches first touch | `TBD` | `TBD` | sample N contacts against relay log by `event_id` |
| Duplicate deliveries per 1,000 events | `TBD` | `TBD` (expect `DUPLICATE` outcome) | count of `IngestResult.status == duplicate` in logs |
| Writes per converted lead | `TBD` | `TBD` | `custom_field_writes` delta per contact |
| Drift incidents caught before a bad write | `0` by construction | `TBD` | count of `AttributionMappingError` at startup |

Sections to write once a deployment exists: client context, campaign mix,
volume, and what the team changed after seeing the first-touch report. Keep
them factual or keep them out.

## Limits (stated up front)

- **First touch only.** Multi-touch weighting is not implemented; a later touch
  is recorded as activity, not as credit. If a customer needs linear or
  position-based models, this is a modelling change, not a config flag.
- **One location per engine instance.** Cross-location rollups are out of scope.
- **Schema is authoritative.** The engine refuses to run on drift rather than
  migrating fields; field creation stays a human decision.
- **Mock-verified, not production-verified.** The behaviour above is proven
  against `ghl_mock` and the test suite; a live Private Integration token and
  location are needed before any of it can be called a production deployment.
- **No attribution for anonymous traffic.** A touch without an email or phone
  cannot be matched to a contact and is skipped.

## Reproduce

```bash
python -m pytest tests/test_case_attribution.py -q   # case invariants
python examples/case_lead_attribution.py             # the transcript above
python -m pytest -q                                  # full suite
```

The CI workflow runs both the suite and every example on Ubuntu and Windows
across Python 3.10–3.12, so this page cannot silently drift from the code.
