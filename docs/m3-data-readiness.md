# M3 local data readiness

M3 creates deterministic descriptive research artifacts from immutable local
daily publications. It does not download data, mutate raw inputs, fill candles,
adjust prices, predict outcomes, generate trading signals, or establish training
readiness.

## Pipeline

```bash
.venv/bin/python -m stock_gplm.m3.cli \
  --raw-root data/raw/shoonya/NSE \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --output-root reports/m3 \
  --explore RELIANCE-EQ \
  --explore TCS-EQ
```

The pipeline refuses an active M2.4 publication lock, symlinked roots, output
inside the raw tree, symbols absent from the current master, changed inputs, and
an existing run ID. Inputs are size-bounded and SHA-256-bound. After writing to a
private staging directory, all raw hashes are checked again before the derived
run is renamed into place.

Each run contains:

- `inventory.json`: discovered files and M1 publication/content results;
- `coverage-quality.json`: observed coverage and independent readiness gates;
- `research-dataset.csv`: ascending validated OHLCV with source path/hash and
  causal EMA20, RSI14, and MACD (12,26,9) columns when enough observations exist;
- per-symbol JSON and Markdown exploration summaries;
- an SVG price/volume/indicator view when at least one validated candle exists;
- `run-manifest.json`: the deterministic run binding and output hashes.

Derived reports and datasets remain ignored local artifacts. Market data must
not be committed.

## Evidence boundaries

An observed candle can establish only its validated source values and UTC date.
Without checksum-bound reference inputs, M3 reports the following separately:

- current-master membership: known only for the current master snapshot;
- point-in-time membership and aliases: `NOT_ESTABLISHED`;
- market-session and missing-date meaning: `UNKNOWN` without an authoritative
  NSE calendar;
- corporate-action adjustment coverage: `NOT_EVALUATED`;
- historical completeness: `INCOMPLETE`;
- training readiness: `NOT_READY`.

EMA20 requires 20 observations, RSI14 requires 15 closes, MACD requires 26, and
the MACD signal requires 34. Unavailable indicators are left blank and labeled
as insufficient; they are never backfilled or fabricated.

## Initial local evidence — 2026-11-01

The initial local run found one accepted RELIANCE-EQ segment containing four
validated candles from 2026-10-01 through 2026-10-07. TCS-EQ had no local daily
publication. RELIANCE price and volume were plotted; EMA20, RSI14, and MACD were
labeled unavailable because four observations are insufficient. The dates
2026-10-02 through 2026-10-04 are unobserved inside the published filename range,
but their reason remains `UNKNOWN` because no authoritative calendar, listing,
outage, or corporate-action evidence was supplied.
