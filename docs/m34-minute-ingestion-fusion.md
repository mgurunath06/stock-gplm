# M3.4 minute ingestion and point-in-time fusion

M3.4 adds a separate, immutable minute raw tree, the versioned `ShoonyaMinuteV1`
adapter, session and volume checks, and fusion of minute bars with strictly prior
daily features. It trains nothing, predicts nothing, does not re-extract the daily
dataset, and keeps `training_readiness` at `NOT_READY`.

## Commands

```bash
.venv/bin/python -m stock_gplm.m3.minute_cli import-snapshot \
  --archive <snapshot.tar> --sha256 <sha256> --source-hashes <sha256sum-output> \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --raw-root data/raw/shoonya-minute/NSE --audit-root reports/m34 --source-label <where-from>

.venv/bin/python -m stock_gplm.m3.minute_cli report \
  --minute-root data/raw/shoonya-minute/NSE --daily-root data/raw/shoonya/NSE \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --session-policy config/m3/minute-session-policy.json \
  --readiness-run reports/m3/<m31-id> --research-run reports/m32/<m32-id> \
  --import-audit reports/m34/import-<id> --output-root reports/m34 --source-label <where-from>
```

## Import

The tar is untrusted. It must be uncompressed, hold only regular files and plain
directories with publication names, and match both its SHA-256 and a `sha256sum`
list captured at the source. Each triplet must pass its marker and metadata contract
and name the current master's token. A bad triplet is listed and not published. A
destination that differs stops the import before any write. Files appear read-only by
exclusive hard link with the marker last. Content quality is judged later, so a
poor candle file is preserved rather than refused. A checksum proves bytes only.

## `ShoonyaMinuteV1`

A gzip JSON array, newest bar first, of objects with exactly the string fields `stat`,
`time`, `ssboe`, `into`, `inth`, `intl`, `intc`, `intvwap`, `intv`, `intoi`, `v`, `oi`.
`time` read as IST must equal `ssboe`. This describes the observed files, not a provider
guarantee; another shape is rejected, not coerced. Two metadata variants exist
(`GPLM_JOBS_V1`, `COLLECTOR_V01`), and only the second states the interval.

| Field | Measured behaviour | Use |
| --- | --- | --- |
| `intv` | Per-bar volume on sessions before 2026-08-03; negative values from then on | Features, unquarantined sessions only |
| `v` | Running total; `v` deltas equal `intv` after the first bar, and its first value can exceed `intv` | Checks only |
| `intvwap` | Stays inside the bar range | Checks only |
| `intoi`, `oi` | Always `0` | Ignored |

Whether a label marks a bar's start or end is undocumented, so a bar is treated as usable
one minute after its label, which is safe either way.

## Decisions

| Decision | Cause | Effect |
| --- | --- | --- |
| `REJECT` | Bad record, duplicate or out-of-order timestamp, bar from another date, bad OHLC, unreadable or oversized payload | No bars, no features; issues listed |
| `QUARANTINE` | A negative `intv`, or a date past the session policy's `valid_through` | Bars and reconciliation visible; no features |
| `REVIEW` | Off-session bar, missing regular minute, zero volume, volume spike (at least 20 times the session's median), a 5% bar move, cumulative break | Values unrepaired, flags carried into features |
| `ACCEPT` | None of the above; flat OHLC alone is informational | Features |

Duplicates are never deduplicated and missing minutes are never filled.

## Fusion rule

A session reads only the daily row of the immediately prior session on the axis.

- `feature_available_utc` is the bar label plus one minute.
- `daily_available_utc` is 00:00 IST on the day after `daily_as_of_date`, a lower bound
  because the provider's publication time is unknown.
- If the prior session has no candle, daily values are blank and `daily_status` says why.
- Only regular-session bars create rows. Windows crossing a missing minute are blank.
- Daily values are the unadjusted M3.2 features; corporate actions remain `UNKNOWN`.

The last minute's close is not a stand-in for the daily close: they differed on 141 of 142
sessions before 2026-08-03.

Every row passes `assert_causal`. The run also recomputes each session from inputs cut at
the session and at every 25th bar, and recomputes M3.2 features with later candles removed.
Any change raises `LeakageError` and publishes nothing. This proves the pipeline reads no
later input. It cannot prove the vintage of the data, because files were fetched after the
sessions they describe, so a later provider revision or retroactive adjustment is undetectable.

## Outputs

`minute-segments.csv`, `minute-bars.csv`, `bar-flags.csv`, `daily-reconciliation.csv`,
`symbol-coverage.csv` (every current EQ symbol), `coverage-gaps.csv`, `fused-features.csv`,
`leakage-audit.json`, `daily-confirmation.json`, `coverage-quality.json`,
`quality-gates.json` and `run-manifest.json`. A run ID binds its inputs and no timestamps,
so a rerun reproduces identical bytes and an existing run is never replaced.

`CONFIRMED_MINUTE_SCHEMAS` in `intraday.py` stays empty: an adapter being available does not
open the intraday label path while these gates are blocked.
