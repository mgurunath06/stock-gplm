# M2.1 reference-data integration

M2.1 imports explicitly supplied local reference files without changing them. It
does not download data, connect to Oracle, or turn a current instrument snapshot
into historical evidence. Every payload is bound to a sidecar manifest and its
compressed or uncompressed bytes are read only.

## Common manifest contract

Each payload needs a JSON manifest with exactly these fields:

```json
{
  "schema_version": 1,
  "contract": "stock-gplm.nse-calendar.v1",
  "payload_file": "nse-calendar.csv",
  "sha256": "<lowercase SHA-256 of the payload bytes>",
  "source": "<authoritative publisher name>",
  "source_reference": "<publication identifier or URL recorded at acquisition>",
  "publication_date": "YYYY-MM-DD",
  "acquired_at_utc": "YYYY-MM-DDTHH:MM:SSZ",
  "effective_start": "YYYY-MM-DD",
  "effective_end": "YYYY-MM-DD",
  "coverage_status": "UNKNOWN | PARTIAL | COMPLETE"
}
```

The importer rejects mismatched names or hashes, naive acquisition timestamps,
publication dates after acquisition, invalid ranges, duplicate JSON keys, and
non-finite JSON numbers. `COMPLETE` is a claim supplied by the source manifest,
not inferred by the software.

## Versioned payload contracts

### NSE calendar V1

Contract: `stock-gplm.nse-calendar.v1`.

The normalized CSV form has the exact header `date,status,label`. JSON may instead
be an object containing only a `records` list with those same fields. Supported
statuses are `TRADING_SESSION`, `MARKET_CLOSED`, `SPECIAL_SESSION`, and `UNKNOWN`.
The manifest range must lie within 2022-01-01 through 2026-12-31. Omitted dates
remain unknown. A `COMPLETE` range must explicitly contain every calendar date,
including closed dates; weekends and holidays are never inferred.

This is a normalized authoritative-data contract. If the supplied NSE publication
has a different source layout, it requires a source-specific, tested adapter; it
must not be silently reinterpreted.

### Shoonya instrument master V1

Contract: `stock-gplm.shoonya-instrument-master.v1`.

The importer accepts a plain delimited file or a ZIP containing exactly one file.
Comma, pipe, and tab delimiters are recognized. Required headers are `Exchange`,
`Token`, and `TradingSymbol`; `ISIN` is optional. Only rows whose `Exchange` is
exactly `NSE` enter the NSE inventory. Empty optional ISIN values remain `null`.

The import records symbols, tokens and supplied ISINs as of the master snapshot.
It deliberately leaves listing date, delisting date, historical membership and
alias history unknown. Current master membership is not historical universe
membership.

### Historical symbol mappings V1

Contract: `stock-gplm.historical-symbol-mappings.v1`.

The JSON payload is `{"records": [...]}`. Each record has exactly
`canonical_symbol`, `alias_symbol`, `valid_from`, `valid_to`, and nullable `isin`.
Ranges must not overlap for a canonical symbol. Mappings can enrich a symbol
already in the inventory but cannot create listing membership evidence.

### Listing history V1

Contract: `stock-gplm.listing-history.v1`.

The JSON payload is `{"records": [...]}`. Each record has exactly
`canonical_symbol`, `listing_date`, nullable `delisting_date`, and nullable
`membership_verified_through`. Dates must lie inside the manifest effective
range, symbols must be unique, and existing instrument-membership invariants are
reused. Records may enrich current-master identities; absent records stay
unknown. Candle presence and absence never create listing evidence.

### Provider unavailability V1

Contract: `stock-gplm.provider-unavailability.v1`.

The JSON payload is `{"outages": [...]}`. Every outage has exactly `start_date`,
`end_date`, and `reason`. Intervals must be non-overlapping and inside the
manifest range. Manifest provenance is attached to each interval. M2.3 emits
`PROVIDER_UNAVAILABLE` only from this imported contract and only when calendar
and listing evidence also establish an expected session.

### Corporate actions V1

Contract: `stock-gplm.corporate-actions.v1`.

The JSON payload contains a `catalogs` list. Each catalog declares `symbol`,
`coverage_status`, `coverage_start`, `coverage_end`, and `actions`. Each action
declares `action_type`, `effective_date`, `adjustment_status`, and `details`.
Manifest provenance is attached to every catalog and action. M2.1 stores these
facts but does not calculate adjusted prices. `POLICY_REQUIRED`, `UNADJUSTED`, or
otherwise unresolved statuses continue to block completeness where applicable.

## Coverage workflow

The coverage request has this form:

```json
{
  "schema_version": 1,
  "symbols": ["RELIANCE-EQ"],
  "horizons": [
    {"name": "acceptance-window", "start_date": "2026-10-01", "end_date": "2026-10-07"}
  ]
}
```

Once authoritative local payloads and manifests are supplied, run:

```bash
.venv/bin/python -m stock_gplm.m2.reference_cli \
  --calendar-payload /local/reference/nse-calendar.csv \
  --calendar-manifest /local/reference/nse-calendar.csv.manifest.json \
  --instrument-payload /local/reference/NSE_symbols.txt.zip \
  --instrument-manifest /local/reference/NSE_symbols.txt.zip.manifest.json \
  --symbol-mapping-payload /local/reference/historical-symbol-mappings.json \
  --symbol-mapping-manifest /local/reference/historical-symbol-mappings.json.manifest.json \
  --listing-history-payload /local/reference/listing-history.json \
  --listing-history-manifest /local/reference/listing-history.json.manifest.json \
  --corporate-actions-payload /local/reference/corporate-actions.json \
  --corporate-actions-manifest /local/reference/corporate-actions.json.manifest.json \
  --coverage-request /local/reference/coverage-request.json \
  --output-dir reports/reference
```

Optional mapping, listing-history, and action arguments must be supplied as
payload/manifest pairs.
The command refuses to overwrite outputs or place reports in a source directory.
It emits normalized inventory and calendar files, copied provenance manifests,
optional mappings/actions, and `reference-coverage.json`. Exit status is 0 only
when every requested symbol/horizon is `READY`, 1 for `INCOMPLETE`, and 3 for an
invalid contract or artifact error.

## Decision boundaries

Raw candle acceptance, historical completeness, and model-training readiness are
independent. A valid Shoonya segment can be `ACCEPT` while historical completeness
is `QUARANTINE`. Training readiness is always `NOT_READY` in M2.1 because a
point-in-time universe, chronological holdout, purging/embargo policy, leakage
audit, and training configuration have not been approved or evaluated.

## Current blockers and unverified assumptions

The real Shoonya NSE master is now present and has passed the separate M2.2
structural and identifier validation. It has no publication date, acquisition
timestamp, effective date, or ISIN column, so it cannot yet satisfy the temporal
M2.1 manifest or establish historical reference coverage. No authoritative NSE
calendar, historical mapping, or corporate-action payload is present. Consequently,
the real RELIANCE candle publication retains M1 `ACCEPT`, while historical
completeness remains `QUARANTINE`.

The following require sanitized or authoritative local samples before compatibility
can be claimed:

- whether future Shoonya snapshots preserve the now-observed archive layout,
  UTF-8 encoding and headers, and whether any separate source supplies ISIN or
  authoritative snapshot-effective timestamps;
- the exact NSE calendar publication layouts and whether a publication explicitly
  covers regular, closed, and special-session dates;
- authoritative point-in-time listing, delisting and historical membership data;
- authoritative historical-symbol mapping coverage and treatment of NSE series;
- corporate-action types, effective-date semantics, coverage guarantees, and an
  approved price/volume adjustment policy;
- authoritative provider-unavailability notices satisfying the versioned contract.

Until these arrive, omitted facts remain `UNKNOWN`; M2.1 makes no historical
completeness or training-readiness claim.
