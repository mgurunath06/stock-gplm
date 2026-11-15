# M5.8 calculation engine and coverage matrix

Status: **IN PROGRESS**. This is not milestone completion and does not authorize a holdout read or a
research/backtest workload.

The semantic source is the owner-supplied 340-strategy library in
`config/strategies/reference-300-strategies.txt` plus its registered addendum. Its machine-readable
calculation decomposition is `config/verify/primitives.json`: 125 rows in 11 modules. The latter is
the execution and coverage contract; it does not silently discard duplicate or blocked source ideas.

## Implemented foundation

- Governed timeframes: `1m,5m,15m,30m,60m,4h,1d,1w`. Additional bounded `Nm` and `Nh` intraday
  durations can be constructed without code changes.
- Intraday resampling is anchored at 09:15, never crosses a session, never fills missing minutes and
  marks incomplete buckets. A 4h NSE session is a 240-minute bucket plus the 135-minute remainder.
- `gplm feature-coverage` evaluates each unique DSL expression once per stock/timeframe/data
  fingerprint, stores compressed arrays under `<home>/cache/feature-catalogue/`, and reuses them.
- `feature-coverage.csv` has every calculation x requested timeframe x stock row, exact expressions,
  extracted parameters, valid/missing observations, warm-up, incomplete candles, cache status,
  verification status and an exact blocker.
- Parameter values live in configuration. The standard feature space now includes EMA
  10/20/34/50/100/200; the catalogue expressions and strategy JSON remain editable inputs.
- Newly completed prior gaps: quadratic OLS curve fit; causal Weis wave volume; virgin-CPR state;
  Smart Money Index; fair-value-gap mitigation state.

## Run locally (PowerShell, repository root)

```powershell
$env:PYTHONPATH="src"
$env:PYTHONUTF8="1"

# Small calculation-only pilot. No model training or backtest.
.\windows\gplm.cmd feature-coverage --stocks "RELIANCE" --timeframes "1d,1w" --name m58-reliance-daily

# All required timeframes for selected stocks. Existing accepted local candles only.
.\windows\gplm.cmd feature-coverage --stocks "RELIANCE,HDFCBANK,ICICIBANK" --timeframes "1m,5m,15m,30m,60m,4h,1d,1w" --name m58-pilot

# Resume/reuse: rerun the identical command. Fingerprinted expression arrays are reused.

# Synthetic correctness verifier; exit 2 means correctness passed but tracked catalogue gaps remain.
.\windows\verify.cmd
```

Outputs are `<home>\reports\feature-coverage\<name>\feature-coverage.csv` and `summary.json`.
No unsupported calculation is promoted by cache success: it remains `BLOCKED_MISSING_DATA` or
`BLOCKED_IMPLEMENTATION`.

## Remaining completion blockers

OHLCV-computable missing (9): Order Block, VCP contraction, trendline/pitchfork, point-and-figure,
DTW, ADF, causal HMM posterior, historical kNN analogues, and meta-labelling. Partial (2): chart-pattern
geometry and Fibonacci harmonic geometry. Data-governed rows (delivery, free float, official index/
point-in-time universe, F&O/options, pre-open, breadth, NSE calendar) remain
`BLOCKED_MISSING_DATA`; no values are inferred.

Stock-DNA behavioural evidence, cutoff-frozen prediction/reveal/evaluation commands, and integration
of the complete cached catalogue into ML search remain future M5.8 increments. Until those and every
calculation blocker are resolved or justified, M5.8 is not complete.
