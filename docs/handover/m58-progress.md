# M5.8 implementation checkpoint (2026-11-15)

Branch `feature/m55-gplm-app`; local only, not pushed or merged.

Commits:

- `700782d` session-aligned 4h and configurable intraday frames
- `2495f65` reusable calculation cache, coverage matrix and `feature-coverage` CLI
- `78100ee` independently checked quadratic fit and causal Weis wave volume
- `711b6c3` virgin CPR, Smart Money Index and FVG mitigation state
- `313707f` causal Stock DNA discovery and reporting
- `c1485c2` cutoff-frozen prediction, separate reveal and evaluation
- `dc2d04e` resumable chronological freeze-then-reveal walk-forward
- `5f5da31` independently checked causal DTW VCP distance
- `745f878` range-specific walk-forward steps, run-only totals, evaluate filters, frozen
  content-addressed feature snapshots, steps JSON/CSV, progress-panel suppression

Verification performed (synthetic only):

- M5.8 feature-engine targeted tests: 5 passed.
- Stock DNA tests: 2 passed, including future-candle mutation isolation.
- Prediction/reveal/walk-forward tests: 3 passed, including append-only freeze and deterministic resume.
- Timeframe tests: 2 passed.
- Full calculation section: 125 rows, 1,483 expression x timeframe evaluations across all eight
  governed frames; no unexpected errors. Current counts: 25
  independent-reference, 57 computed, 28 composed, 8 not implemented, 2 partial, 5 need data.
- No full repository test suite, real-data calculation, training, research or backtest was run.

Untracked pre-existing files intentionally untouched: `.git.wsl-backup`,
`config/app/gplm.windows.json`.

The completion path is documented in `docs/m58-feature-computation.md`: request Stock DNA at the
2025-08-13 cutoff, freeze a 2025-08-14 prediction, then reveal it by id. Next safe increment is the
remaining OHLCV algorithm families, one independently verified family per commit.

## Increment: walk-forward step report and feature snapshots (2026-11-15, Claude after Codex)

Fixed: named walk-forward previously embedded the stock's cumulative evaluation (165 rows for a
19-step run). Now `walk-forward-summary.json` has `steps` and totals restricted to the invocation;
cumulative mode exists only via unfiltered `prediction-evaluate` and is labelled `CUMULATIVE`.

Files: `src/stock_gplm/app/{m58_prediction,m58_snapshot,cli,db,lanes,progress}.py`,
`config/app/feature-space.json` (`snapshot_indicators`, report-only; excluded from lane feature hash so
existing prediction IDs are unchanged), `tests/test_m58_prediction.py`, `docs/m58-feature-computation.md`.

Tests (synthetic only): `test_m58_prediction.py` 9 passed; M5.8 + Windows locking set 20 passed;
`test_app_gplm.py` + `test_m56.py` 28 passed, 1 skipped (CUDA). Fixture console smoke rendered table,
details and totals. No real-data command was run.

Owner commands (PowerShell, repo root):

```powershell
$env:PYTHONUTF8="1"
.\windows\gplm.cmd prediction-walk-forward --stock "ICICIBANK" --start 2025-08-01 --end 2025-08-29 --timeframe 1d --horizon 1 --name icicibank-daily-aug-2025
.\windows\gplm.cmd prediction-walk-forward --stock "ICICIBANK" --start 2025-08-01 --end 2025-08-29 --timeframe 15m --horizon 1 --name icicibank-15m-aug-2025
```

Reports: `<home>\reports\prediction-evaluation\<name>\walk-forward-summary.json`,
eports\prediction-evaluation\<name>\walk-forward-summary.json`,
`walk-forward-steps.json`, `walk-forward-steps.csv`, `prediction-evaluation.json`.

Known limitations: failed steps appear in `errors`, not as table rows; `--run-name` resolves only the
default report location (not a custom `--out`); each reused step still rebuilds the as-of lane (existing
predict cost) because the snapshot/identity check needs it; the 15m run is ~25x the daily step count.
Next: owner runs the two commands above; then remaining OHLCV algorithm families.

## Fix: direction labels were cost-biased (contract v2, 2026-11-15)

Owner observed only DOWN/UNCERTAIN predictions. Cause: Stock DNA discovery, calibration labels, reveal
`actual_direction` and Brier all defined UP as "next-open-to-close return beats the ~39 bps delivery
round trip". Typical daily moves are smaller, so patterns were learned as DOWN, the DNA quality check
credited the cost drag to DOWN patterns, and small gross rises were scored as actual DOWN.

Fix: `stock-gplm.stock-dna.v2` and `stock-gplm.m58-prediction.v2` label direction on the gross move.
Costs apply only to the TRADE gate (confident calibrated UP plus positive mean long net return over
>= `m58.prediction.minimum_trade_gate_outcomes` (default 20) similar earlier calibration rows) and to trade P&L.
v1 predictions/evaluations remain frozen and untouched; evaluations now count only the current contract.
Consequence: rerunning the August daily command creates 19 new v2 predictions (new IDs), not reuses.
Tests: 18 M5.8 passed incl. cost-invariance regressions; app/m56/locking 32 passed, 1 skipped.
