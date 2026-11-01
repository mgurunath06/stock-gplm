# M2.3 historical OHLCV inventory

M2.3 inventories explicitly approved local Shoonya daily-publication directories
against the 2,715 EQ identities in the validated M2.2 master. It reads source
files, validates them with M1, and writes reports outside the raw tree. It never
contacts Shoonya, changes Oracle publications, fills candles, or derives listing
dates from observations or missing data.

## Approval and run binding

The caller must supply `--expected-eq-count 2715`. A mismatch stops before an
output directory is published. The inventory request contains only explicit named
horizons:

```json
{
  "schema_version": 1,
  "horizons": [
    {"name": "research-window", "start_date": "2022-01-01", "end_date": "2026-10-31"}
  ]
}
```

Every master, request, reference payload, reference manifest, raw segment, and
sidecar is checksum-bound. The canonical input list determines the run ID. Files
are streamed into a private staging directory, all source checksums are verified
again, and the complete directory is renamed into
`<output-root>/<run-id>/`. Existing run IDs are never overwritten. Report content
has stable ordering and contains no wall-clock timestamps, so identical bytes and
configuration produce identical output bytes.

Only immediate symbol directories and their immediate files are inspected.
Symlinks are reported and ignored. Inputs are size-bounded before hashing and
validation.

## CLI

```bash
.venv/bin/python -m stock_gplm.m2.inventory_cli \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --raw-root /approved/local/daily/shoonya/NSE \
  --request /approved/local/m23-request.json \
  --output-root reports/m23 \
  --expected-eq-count 2715 \
  --calendar-payload /local/reference/nse-calendar.csv \
  --calendar-manifest /local/reference/nse-calendar.csv.manifest.json \
  --listing-history-payload /local/reference/listing-history.json \
  --listing-history-manifest /local/reference/listing-history.json.manifest.json \
  --symbol-mapping-payload /local/reference/historical-symbol-mappings.json \
  --symbol-mapping-manifest /local/reference/historical-symbol-mappings.json.manifest.json \
  --corporate-actions-payload /local/reference/corporate-actions.json \
  --corporate-actions-manifest /local/reference/corporate-actions.json.manifest.json \
  --provider-unavailability-payload /local/reference/provider-unavailability.json \
  --provider-unavailability-manifest /local/reference/provider-unavailability.json.manifest.json
```

Every optional reference input is a payload/manifest pair. Omitting evidence
keeps the relevant classification unknown. Exit status is 0 only when publication,
candle-content, and historical-completeness gates all accept; 1 means a completed
run has an incomplete or quarantined gate; 3 means the run could not be published.
Training readiness remains `NOT_READY` regardless of the other gates.

## Report schemas

All JSONL records contain `schema_version: 1` and are emitted incrementally.

| File | Record or object contract |
| --- | --- |
| `run-manifest.json` | `stock-gplm.m23-inventory-run.v1`; run ID, expected count, sorted input hashes, deterministic/immutable flags, and output hashes |
| `inventory-summary.json` | `stock-gplm.m23-inventory-summary.v1`; universe reconciliation, zero-artifact count, record counts, and four independent gates |
| `segments.jsonl` | One discovered `.json.gz` per line: relative path, requested range, checksum, M1 publication gate, content gate, disposition, validated dates, and issues |
| `symbol-coverage.jsonl` | One current EQ symbol and requested horizon per line, including zero-artifact symbols; gate decisions, counts, and blockers |
| `coverage-intervals.jsonl` | Consecutive dates with identical publication, candle, membership, calendar, and historical states; intervals cover each requested symbol/horizon without overlap |
| `exceptions.jsonl` | Orphans, unsupported artifacts, unknown symbol directories, rejected publications, quarantined content, range overlaps, repeated dates, and contradictory OHLCV |

Publication states are `VERIFIED_PUBLISHED`, `MISSING_SEGMENT`,
`REJECTED_OR_UNPUBLISHED`, or `OVERLAPPING_VERIFIED_PUBLICATIONS`. Candle states
are `VALIDATED_OBSERVED`, `NO_VALIDATED_CANDLE`, `QUARANTINED`, `DUPLICATE`, or
`CONTRADICTORY`.

Historical states are `OBSERVED`, `PRE_LISTING`, `MARKET_CLOSED`,
`PROVIDER_UNAVAILABLE`, `MISSING_EXPECTED_SESSION`, or
`AUTHORITATIVE_REFERENCE_REQUIRED`. `PROVIDER_UNAVAILABLE` can only come from a
validated `stock-gplm.provider-unavailability.v1` payload. No day is classified
as a weekend, holiday, pre-listing period, delisted period, or outage from candle
absence.

## Completeness boundaries

The M2.2 master establishes the current reconciliation set and current Shoonya
identifiers only. Historical listing membership comes solely from the listing
history contract. Historical identity coverage comes from the symbol-mapping
contract. Calendar and corporate-action coverage remain separate requirements.
A valid publication can therefore pass publication and candle-content gates while
historical completeness remains incomplete.

The checked-in real master and raw RELIANCE publication were not processed by the
M2.3 implementation tests. Tests use reproducible synthetic publications and a
synthetic 2,715-row master.
