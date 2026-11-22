# Reporting warehouse (GHL-025, GHL-026)

A star schema and an incremental contact loader. The point is not "we have a
warehouse" — it is that a nightly sync is **safe to re-run**: no duplicate
versions, no lost window when a run dies, no invented history when a poll returns
the same data again.

## Schema

```
dim_date ──┐
           ├── fact_contact_version   (grain: one contact version)
dim_contact┘         │
                     └── contact_key, version
dim_pipeline_stage ─┐
                    ├── fact_opportunity_version (grain: one opportunity version)
dim_opportunity  ───┘
```

`fact_contact_version` is loaded by the contact extractor, `fact_opportunity_version`
by the opportunity extractor. Both facts share `dim_date` and the same watermark
discipline; they are separate loads because the two endpoints page, filter and
throttle independently.

`ghl_warehouse.schema` validates itself at import: duplicate names, nullable
primary keys, facts without measures, facts that cannot reach `dim_date`, and
foreign keys pointing at no dimension key all raise `SchemaError`. A schema that
cannot be joined fails the test run, not a report three weeks later.

Keys are deterministic — `contact_key = sha256(locationId, contactId)[:24]` — so
reloading the same contact into an empty warehouse produces the same rows.

### Type-2 dimension rule

One row per **observed version** of a contact:

| column | meaning |
| --- | --- |
| `version` | 1, 2, 3... per contact |
| `row_hash` | hash of tracked attributes only |
| `valid_from` / `valid_to` | observed update timestamps; the current row has `valid_to = NULL` |
| `is_current` | exactly one `1` per contact |

A payload that hashes to the current `row_hash` is **not** a version. `dateUpdated`
and poll counters are excluded from the hash on purpose: including them would
record the sync schedule rather than change, and every nightly run would invent a
version for every contact.

A payload whose `dateUpdated` is older than the loaded version is ignored
(`stale_ignored`) rather than appended — out-of-order delivery must not rewrite the
timeline backwards.

## Incremental load

Order is the design:

1. read the watermark;
2. walk **every** page, refusing a partial result (`IncompleteExtractError`);
3. load rows into the sink;
4. then advance the watermark, and only as far as what was actually loaded.

Step 4 after step 3 means a crash mid-load is safe: the next run re-reads the same
window and the hash check turns the replay into a no-op. Watermarks are monotonic —
a replayed or out-of-order batch cannot rewind the mark.

Reads are **overlapped**: the next scan starts `overlap_seconds` (default 300)
before the mark, because commits do not arrive in timestamp order. The overlap is
applied when computing the window, never written into the stored mark.

### Measured cost, stated plainly

The source is walked **in full** and filtered client-side against the scan window.
Server-side filtering by `dateUpdated` has not been verified against a live
sub-account, so it is not used. `ExtractStats.pages` / `.scanned` exist so the cost
is measureable before anyone calls it cheap.

## Usage

```python
from ghl_warehouse import (
    InMemoryWarehouse, JsonFileWatermarkStore, IncrementalContactExtractor,
)

warehouse = InMemoryWarehouse()
marks = JsonFileWatermarkStore("state/watermarks.json")
extractor = IncrementalContactExtractor(contacts_resource, warehouse, marks)

plan = extractor.run(dry_run=True)      # no writes, no watermark movement
result = extractor.run()                # load, then advance
print(result.to_dict())                 # scanned / new_versions / unchanged / pages
```

`dry_run=True` reports what would be loaded and leaves both the warehouse and the
watermark untouched.

## Failure behaviour

| condition | behaviour |
| --- | --- |
| incomplete page walk | `IncompleteExtractError`, nothing loaded, mark untouched |
| sink raises mid-load | exception propagates, mark untouched, next run replays the window |
| record without `id` or `dateUpdated` | counted as rejected with the reason; the rest of the batch loads |
| payload identical to current version | counted as `unchanged`, no new version |
| payload older than current version | counted as `stale_ignored` |

## Opportunity grain (GHL-026)

`ghl_warehouse.opportunities` adds a second fact at deal grain. It reuses the
type-2 rule rather than re-implementing it, which is the whole argument for keeping
one loader contract:

| column | meaning |
| --- | --- |
| `opportunity_key` | `sha256(locationId, opportunityId)[:24]` |
| `version` | 1, 2, 3... per deal |
| `row_hash` | tracked attributes: name, status, stage, value, owner, contact |
| `valid_from` / `valid_to` | observed `dateUpdated`; current row has `valid_to = NULL` |
| `is_open` | `1` for `open`, `0` for `won` / `lost` / `abandoned` |
| `stage_key` | joins `dim_pipeline_stage(pipelineId, pipelineStageId)` |

A **stage move is a new version**, and that is the point: `stage_key` changing is
the signal a pipeline funnel report is built from. The previous row is closed at the
new observed timestamp, so "what was open value on any past date" is answerable
without snapshotting the whole table.

Two rules exist because the alternative is a quietly wrong report:

- an **unknown status** is rejected (`InvalidRecordError`), not bucketed into
  "other". A status the code has never seen means the provider changed or the
  payload is wrong, and a funnel that silently grows an "other" bar is worse than a
  failed load.
- a **non-numeric `monetaryValue`** is rejected rather than coerced to `0`. Only
  genuinely absent values are `0.0`. A deal worth "about 5k" is a data problem for
  the operator, not a measurement.

`InMemoryOpportunityWarehouse.open_value()` sums current open versions only, which
is the one number a revenue conversation usually starts from.

```python
from ghl_warehouse import (
    InMemoryOpportunityWarehouse, IncrementalOpportunityExtractor,
)

warehouse = InMemoryOpportunityWarehouse()
extractor = IncrementalOpportunityExtractor(opportunities_resource, warehouse, marks)
stats = extractor.run()
print(stats.new_versions, warehouse.open_value())
```

The mock server exposes `POST /__admin/opportunities` plus
`POST /opportunities/search` and `GET /opportunities/{id}` so the HTTP path is
tested end to end, not faked at the resource boundary.

## Durable sink

`InMemoryWarehouse` and `InMemoryOpportunityWarehouse` are reference implementations: they prove the load rules and nothing else. `SqliteWarehouse` keeps the same contract in a file, so the same payload sequence produces the same versions, statuses and current row. Parity is a test, not a claim: `tests/test_warehouse_sqlite.py` replays one sequence through both sinks and compares statuses, versions, hashes and `valid_to`.

What the file adds beyond storage:

* one transaction per record, because a crash between closing the old version and inserting the new one is worse than a refetch
* a partial unique index on `(key) WHERE is_current = 1`, because the DDL cannot say "at most one current row"
* `manifest()`, which hashes each table's ordered contents: same source data in, same digest out. It is a reproducibility check, never a report.

## Not built yet

- Conversation and attribution facts (contact and opportunity grains today).
- A durable SQL sink. `InMemoryWarehouse` is a reference implementation proving the
  load rules; production storage and migrations are not in this repo yet.
- Server-side incremental filtering (see cost note above).
- Opportunity value currency. `monetaryValue` is stored as a bare number; multi-currency
  sub-accounts would need a currency key before any total means money.
- Any performance or correctness claim against a live GoHighLevel sub-account. All
  evidence here is from the mock server and unit tests.
