# Reporting warehouse (GHL-025)

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
dim_pipeline_stage   (declared, not yet populated by this loader)
```

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

## Not built yet

- Opportunity, conversation, and attribution facts (contact dimension only today).
- A durable SQL sink. `InMemoryWarehouse` is a reference implementation proving the
  load rules; production storage and migrations are not in this repo yet.
- Server-side incremental filtering (see cost note above).
- Any performance or correctness claim against a live GoHighLevel sub-account. All
  evidence here is from the mock server and unit tests.
