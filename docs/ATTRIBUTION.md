# Attribution engine (Project B)

Turns anonymous touch data from a lead form, quiz, or landing page into
GoHighLevel contact attribution — without re-crediting a lead on every later
visit and without creating duplicate contacts when the same event is delivered
twice.

```python
from ghl_attribution import AttributionEngine

engine = AttributionEngine(client)          # discovers custom fields at startup
result = engine.ingest(event)               # event: dict or AttributionEvent
```

## What "attribution" means here

| Concept | Stored in | Rule |
| --- | --- | --- |
| Earned credit (source / medium / campaign / content / landing page) | custom fields on the contact | written once, never rewritten |
| `first_touch_at` | custom field | set with credit, immutable afterwards |
| `last_touch_at` | custom field | advances only forward in time |
| click ids (`gclid`, `fbclid`) | custom field | same immutability as credit |
| `attribution:<source>` tag | contact tag | applied only by the event that earns credit |

Later touches from a different source update *activity*, never *credit*. This is
the whole point: the common implementation re-credits on every touch, so one
`utm_source=direct` visit from a returning lead erases the paid campaign that
actually produced them. That failure is why the credit fields in this package
are treated as append-only.

Overwriting is possible but explicit: `AttributionPolicy(overwrite_existing=True)`.
Every overwritten field is listed in `result.overwritten` so the decision is
auditable rather than silent.

## Required custom fields

Discovery runs against `GET /locations/{locationId}/customFields` and resolves
field **ids** by key. Keep one field per signal:

| Key | Type | Required |
| --- | --- | --- |
| `contact.attribution_source` | TEXT | yes |
| `contact.attribution_medium` | TEXT | yes |
| `contact.attribution_campaign` | TEXT | yes |
| `contact.attribution_content` | TEXT | yes |
| `contact.attribution_landing_page` | TEXT | yes |
| `contact.attribution_first_touch_at` | DATE | yes |
| `contact.attribution_last_touch_at` | DATE | yes |
| `contact.gclid` | TEXT | no |
| `contact.fbclid` | TEXT | no |

If a required field is missing, retyped, or duplicated, `AttributionEngine(...)`
raises `AttributionMappingError` **before the first write**, and the error
carries every problem it found:

```python
for problem in exc.drift:
    print(problem.key, problem.kind, problem.detail)   # missing / type / ambiguous
```

A retyped `DATE` field is a live incident, not a warning: writing
`2026-11-20T10:00:00Z` into a `TEXT` field silently breaks every downstream
report. The engine refuses to start instead of guessing.

## Idempotency and retries

Two layers, both required:

1. **Event claim** — `(location, event_id)` is claimed before any write. A
   redelivered webhook returns `DUPLICATE` and performs no call. The claim is
   released on failure, so a retry after a 5xx is the *same* operation, not a
   second contact.
2. **Upsert ledger** — the client's idempotency ledger keys `POST /contacts/upsert`
   by email/phone, so a crash between write and response cannot mint a duplicate
   contact on replay.

Outcomes are explicit: `APPLIED`, `PLANNED` (dry run), `DUPLICATE`,
`UNCHANGED`. `UNCHANGED` covers the useful case where the values on the contact
already match — the engine writes nothing and says so.

## Dry run

```python
plan = engine.ingest(event, dry_run=True)
plan.status          # IngestStatus.PLANNED
plan.applied         # ("utm_source", ...) — exactly what would be written
plan.custom_fields   # ({"id": "cf_...", "value": "google"}, ...)
```

No claim is held and no request is made to the contacts endpoint, so the plan can
be shown to a client before the integration goes live.

## No-op traffic

A touch whose only signal is `utm_source=direct` (or empty) carries nothing to
attribute. It returns `UNCHANGED` and creates no contact. Bots, prefetches, and
direct visits are most of the volume; recording them is how an attribution table
fills with worthless rows.

## Mock server support

`ghl_mock` serves `GET /locations/{id}/customFields`, applies `customFields` on
upsert, and counts field-level writes in `state.store.custom_field_writes`.
Drift can be simulated directly on the store, which is what the tests use:

```python
state.fields.drop("contact.attribution_campaign")   # -> mapping error at startup
state.fields.retype("contact.attribution_first_touch_at", "TEXT")
```

## Limits

* Attribution is first-touch only. Linear, position-based, and multi-touch models
  need a separate touch history store — this package deliberately does not keep
  one.
* `last_touch_at` compares parsed timestamps; a touch with no `occurred_at`
  defaults to "now", so ordering is only as good as the sender's clock.
* Field discovery is cached per engine instance: call `engine.discover(refresh=True)`
  after changing field definitions in a running process.
