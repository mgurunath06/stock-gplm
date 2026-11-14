# M3.9-B corporate-action remediation engine

M3.9-B is an engineering-only, dependency-free adjustment library. It proves
deterministic behavior on synthetic data; it does not establish that any real
historical series is adjusted, complete, licensed for derivation, or ready for
training.

## Boundary

`stock_gplm.m39b` accepts immutable in-memory records only. It has no CLI,
network client, file loader, collector, training import, or output writer. It
does not read M3.1/M3.2 runs or the protected holdout. Existing raw series are
represented by frozen `RawCandle` and `CandleSeries` records; the engine returns
a separate `AdjustmentResult` containing both raw and derived values.

M3.9 NSE MCP facts remain `REFERENCE_ONLY`. They cannot form a `VALIDATED`
M3.9-B event because derivation requires an explicit
`APPROVED_FOR_DERIVATION` licence status. No NSE data was downloaded or used by
this milestone.

## Canonical event contract

Every event records:

- a stable event ID, symbol and typed event kind;
- ex-date plus optional event, record and announcement dates;
- source name, source record identifier, licence status, availability timestamp
  and retrieval timestamp;
- explicit share ratio or cash terms when applicable;
- validation state (`VALIDATED`, `UNKNOWN`, `QUARANTINED`, or `UNSUPPORTED`) and
  a reason for every non-validated state.

Missing ex-dates, non-positive ratios, incomplete validated events, unlicensed
validated events and ambiguous economics are rejected. Mergers, demergers and
symbol changes must remain `UNSUPPORTED`. Rights records can preserve a ratio
and subscription price but the adjustment engine refuses them until a policy is
validated.

## Formulas and boundaries

All factors are exact rational numbers while they are accumulated. Derived OHLC
and volume values are rounded once to eight decimal places with
`ROUND_HALF_EVEN`.

| Event | Backward price factor for dates strictly before ex-date | Volume factor |
|---|---:|---:|
| Split or reverse split | old shares / resulting shares | reciprocal of price factor |
| Bonus | held shares / (held shares + bonus shares) | reciprocal of price factor |
| Cash dividend, price-return mode | 1; distribution excluded | 1 |
| Cash dividend, total-return mode | (validated cum-dividend reference price - cash per share) / reference price | 1 |

The ex-date candle is never adjusted by its own action. A candle before several
actions receives their chronological cumulative factor. Cash-dividend total
return treatment requires an explicitly supplied, validated pre-ex-date
reference price; the engine does not discover or infer one.

## Integrity gates

- Input basis must be `RAW_UNADJUSTED`; `DERIVED_ADJUSTED` and `UNKNOWN` inputs
  are refused, preventing reapplication and adjusted/unadjusted mixing.
- Duplicate source records are refused. Corroborating sources with identical
  economics are applied once and all evidence is retained. Conflicting factors
  quarantine the event group by raising `ConflictingEvents`.
- An event must have been available by the requested `knowledge_at` timestamp.
  Candles after that cutoff are refused, and announced future actions are not
  applied before their ex-date.
- Suspicious close-to-next-open discontinuities are flagged with cause
  `UNKNOWN`; price movement alone never creates an event.
- Result lineage binds the input series SHA-256, mode, cutoff, exact factors,
  event records, source identifiers and a deterministic adjustment ID.

## Limitations and next step

No real action source has been approved or registered for M3.9-B derivation.
Rights, mergers, demergers and symbol changes remain unsupported. Dividend
reference-price selection has deliberately not been automated. Decimal output
precision is a declared engineering policy, not exchange tick-size rounding.

M3.9-C should define a human-reviewed, immutable, checksum-bound intake mapping
from a legally approved source into this canonical contract, with independent
symbol/ISIN reconciliation and conflict quarantine. It should first run in
report-only shadow mode; it must not alter raw history, connect M3.9-B to
training, or access the protected holdout.
