# M2 historical inventory and data quality

M2 analyzes explicitly supplied local daily segments. It builds on M1 integrity
and `ShoonyaDailyV1` validation and never changes raw files. It does not discover,
download, or synchronize symbols.

## Components

- `InstrumentInventory` stores canonical NSE symbol, optional ISIN and Shoonya
  token, listing/delisting dates, bounded historical aliases, per-field provenance,
  an explicit membership-verified-through boundary, and alias-history coverage
  status. Unknown values serialize as `null` or `UNKNOWN`; a listing date alone
  does not imply indefinite membership, and an empty alias list does not prove no
  aliases existed.
- `NSETradingCalendar` accepts only supplied `TRADING_SESSION`, `SPECIAL_SESSION`,
  and `MARKET_CLOSED` entries, each with provenance. Dates without entries are
  `UNKNOWN`. A calendar declaring complete coverage must include every calendar
  date in its bounds, including explicitly closed dates.
- `CorporateActionCatalog` stores effective dates, action type, provenance,
  details, adjustment status, and catalog coverage. It never changes prices.
  Unknown or incomplete coverage blocks a completeness claim.
- `analyze_history` reruns M1 per segment, detects requested-range overlaps,
  repeated dates, contradictory OHLCV, and missing dates, then reports each
  explicitly requested historical window.

## Missing-date classifications

| Classification | Required evidence |
| --- | --- |
| `PRE_LISTING` | verified listing date later than the date |
| `MARKET_CLOSED` | explicit calendar entry |
| `PROVIDER_UNAVAILABLE` | explicit session plus a provenance-backed outage range |
| `MISSING_EXPECTED_SESSION` | explicit session and verified listed membership |
| `UNKNOWN` | calendar, membership, or other required evidence is absent |
| `OBSERVED` | validated ShoonyaDailyV1 candle exists |

No weekend, holiday, IPO date, outage, alias, or corporate action is inferred.
Missing dates are never filled and are never converted to zero volume.

## Completeness and decisions

Prediction-horizon requests are explicit named date windows. M2 does not invent a
lookback length for product horizons. Each window reports observed and applicable
expected sessions, a ratio when a denominator is known, classifications by date,
and blockers.

- `ACCEPT`: all source segments pass M1, point-in-time listing membership is
  verified, every date has explicit calendar status, corporate-action coverage is
  complete for the window, and no gaps, overlaps, duplicates, or contradictions
  remain.
- `QUARANTINE`: publications are intact but any required evidence or observation
  is unknown, missing, unavailable, duplicated, contradictory, or adjustment
  metadata is unresolved.
- `REJECT`: at least one segment fails M1 publication integrity.

`ACCEPT` describes only the requested stock and date windows. It does not establish
full-universe completeness or research/model readiness.

M2.1 exposes these conclusions separately in every historical report:

- `raw_candle_acceptance`: publication and ShoonyaDailyV1 validity only;
- `historical_completeness`: calendar, point-in-time membership, actions, gaps,
  duplicates, overlaps and contradictions for the requested horizons;
- `model_training_readiness`: remains `NOT_READY` in M2.1 because chronological
  holdouts, leakage controls and training policy are outside this milestone.

## Current truthful RELIANCE inputs

The checked-in inputs deliberately retain unknown values:

- [instrument-inventory.json](../config/m2/instrument-inventory.json) knows the
  symbol from the accepted local path but has no verified ISIN, token, listing or
  delisting date, or alias-history coverage.
- [nse-trading-calendar.json](../config/m2/nse-trading-calendar.json) has no supplied
  calendar days and claims no coverage.
- [reliance-quality-request.json](../config/m2/reliance-quality-request.json)
  requests the accepted segment window but has unknown corporate-action coverage.

This means the current real RELIANCE M2 report must be `QUARANTINE`, even though
the underlying M1 segment remains `ACCEPT`.

Run the local report without contacting any external service:

```bash
.venv/bin/python -m stock_gplm.m2.cli \
  --symbol RELIANCE-EQ \
  --inventory config/m2/instrument-inventory.json \
  --calendar config/m2/nse-trading-calendar.json \
  --quality-config config/m2/reliance-quality-request.json \
  --output-dir reports/m2 \
  data/raw/shoonya/NSE/RELIANCE-EQ/2026-10-01_2026-10-07.json.gz
```

The command creates, without overwriting:

```text
reports/m2/RELIANCE-EQ.inventory.json
reports/m2/RELIANCE-EQ.historical-quality.json
```

Exit codes are 0 `ACCEPT`, 1 `QUARANTINE`, 2 `REJECT`, and 3 for invalid inputs or
artifact errors. Outputs must be outside all raw segment directories.

## Research boundaries

Inventory and provenance are preserved as-of their supplied dates. Later aliases,
listings, or actions are not backfilled without evidence. M2 does not select a
training universe, split data, create labels, adjust prices, or inspect future
observations. Future training must still use point-in-time universes,
chronological holdouts, and leakage controls.
