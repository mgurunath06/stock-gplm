# M5.8 calculation engine and coverage matrix

Status: **USER-FACING PIPELINE DELIVERED; FEATURE REMEDIATION IN PROGRESS**. Stock DNA, frozen
prediction, separate reveal and walk-forward evaluation are runnable. Remaining calculation blockers
stay explicit and prevent declaring complete feature coverage.

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
  Smart Money Index; fair-value-gap mitigation state; normalized causal DTW against a registered VCP
  template.

## Stock DNA and historical prediction

`stock-dna` automatically tests quantile-defined behaviours from the configured cached feature space.
Thresholds are learned on the chronological discovery segment; evidence status is assigned only on a
later validation segment with independent event spacing, Wilson confidence intervals, baseline lift,
net returns, stability halves, regime breakdown and Benjamini-Hochberg correction. A final later
segment is reserved for forecast calibration.

`predict` windows the data before constructing any bar, feature or DNA evidence, stores an append-only
`HISTORICAL_FROZEN` row and exposes a probability only after the existing Platt calibrator has enough
untouched outcomes. Otherwise it returns `UNCERTAIN` / `NO_TRADE`. `prediction-reveal` is the only
command that reads the requested outcome. `prediction-walk-forward` runs freeze then reveal in strict
chronological order and resumes through prediction natural keys.

## Run locally (PowerShell, repository root)

```powershell
$env:PYTHONPATH="src"
$env:PYTHONUTF8="1"

# Small calculation-only pilot. No model training or backtest.
.\windows\gplm.cmd feature-coverage --stocks "RELIANCE" --timeframes "1d,1w" --name m58-reliance-daily

# All required timeframes for selected stocks. Existing accepted local candles only.
.\windows\gplm.cmd feature-coverage --stocks "RELIANCE,HDFCBANK,ICICIBANK" --timeframes "1m,5m,15m,30m,60m,4h,1d,1w" --name m58-pilot

# Resume/reuse: rerun the identical command. Fingerprinted expression arrays are reused.

# Stock DNA available at the evening of 13 August 2025.
.\windows\gplm.cmd stock-dna --stock "ICICIBANK" --as-of 2025-08-13 --timeframes "1d,1w" --horizons 1 --name icicibank-20250813

# Freeze the forecast for 14 August. Save the printed prediction_id.
.\windows\gplm.cmd predict --stock "ICICIBANK" --as-of 2025-08-13 --target 2025-08-14 --timeframe 1d --horizon 1

# Only after the prior command has frozen the row, reveal the actual target outcome.
.\windows\gplm.cmd prediction-reveal --prediction "<prediction_id>"

# Aggregate every frozen/revealed ICICIBANK forecast.
.\windows\gplm.cmd prediction-evaluate --stock "ICICIBANK" --name icicibank-evaluation

# Owner-operated chronological evaluation; rerun the same command to resume.
.\windows\gplm.cmd prediction-walk-forward --stock "ICICIBANK" --start 2025-01-01 --end 2025-08-13 --timeframe 1d --horizon 1 --name icicibank-wf-2025

# Synthetic correctness verifier; exit 2 means correctness passed but tracked catalogue gaps remain.
.\windows\verify.cmd
```

Outputs are `<home>\reports\feature-coverage\<name>\feature-coverage.csv` and `summary.json`.
No unsupported calculation is promoted by cache success: it remains `BLOCKED_MISSING_DATA` or
`BLOCKED_IMPLEMENTATION`.

## Remaining completion blockers

OHLCV-computable missing (8): Order Block, VCP contraction, trendline/pitchfork, point-and-figure,
ADF, causal HMM posterior, historical kNN analogues, and meta-labelling. Partial (2): chart-pattern
geometry and Fibonacci harmonic geometry. Data-governed rows (delivery, free float, official index/
point-in-time universe, F&O/options, pre-open, breadth, NSE calendar) remain
`BLOCKED_MISSING_DATA`; no values are inferred.

Until every remaining calculation blocker is resolved or explicitly justified, complete feature
coverage is not claimed.
