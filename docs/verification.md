# GPLM correctness verification (`gplm verify`)

Status 2026-11-15. One command produces a PASS/FAIL report showing whether GPLM understood each
strategy, computed the indicators correctly, backtested and validated honestly, and whether its ML
discovery is real. Everything runs on deterministic, seeded synthetic fixtures. No real candle is read
and no research-scale workload runs. It takes about 15 seconds on a CPU.

## Run it

```powershell
cd H:\Guru\Learning\StockGLPM\app
.\windows\verify.cmd                       # report under <home>\reports\verify\<UTC timestamp>\
.\windows\verify.cmd --out C:\temp\verify  # or a folder of your choice
```

The exit code is `0` when everything passes, `1` on a correctness failure and `2` when only tracked calculation gaps remain. Open
`VERIFY_REPORT.md` first.

## What each section proves

| # | Question | How it is checked |
|---|---|---|
| 0 | Does the verifier catch bugs? | Defects are planted and must be detected: wrong RSI smoothing, a primitive that reads tomorrow, a mistranslated golden cross, an early exit, and BH without its step-up. |
| 1 | Did it understand each strategy? | All 350 interpretations of the 340 references are checked. **Semantic:** an independent evaluator (`verify/specparse.py`, with its own parser and pure-Python indicators) runs the original pseudo-code and compares it bar by bar with the DSL entry on three 1,500-bar fixtures. **Structural:** indicator families and constants in the original must appear in the interpretation. Declared approximations are listed with their assumptions and measured agreement. |
| 2 | Are indicators right? | 38 indicators are compared with `verify/reference.py`, written from textbook definitions with no NumPy or pandas. Also checked: hand-computed values, edge cases (constant, monotonic, alternating, spike, zero volume, too short), the TA-Lib/Wilder seeding deviation during warm-up, and agreement with the separate M5.4 feature path. |
| 3 | Signals, fills, costs, leakage | 14 hand-built fill scenarios cover next-bar entry, time, stop, gap stop, target, stop-before-target, trailing and signal exits, shorts, non-overlap, purging, discontinuities and suppressed signals. Also: costs on both legs, parity between the M5.7 and M5.4 simulators, causality of all 155 primitives and all 303 shipped rule expressions (truncation plus random perturbation of future bars), as-of joins, and parser safety. |
| 4 | Validation & statistics | Purged 60/20/20 split; optimisation spy (the grid touches discovery only, validation is read once, the final segment never); folds; BH and the t-test against independent implementations; gate boundaries and fail-closed behaviour; holdout refusal. An end-to-end `research-matrix` run on synthetic raw files is audited: q-values are recomputed, gates re-checked, and resume must not re-read the final segment. |
| 5 | ML discovery | The genetic programme is spied on: fitness must use discovery bars only, the run must be deterministic per seed and elitist, and every genome is logged. Novelty: discoveries are compared with all executable catalogue rules by canonical text and by signal overlap. Discoveries must be backtested, saved, deduplicated, reloaded and re-backtested identically. A tree path must convert to an equivalent DSL rule and persist. Pipeline ML rows must land in the library. |
| 6 | Minimum calculation coverage | `config/verify/primitives.json` maps every row of the owner's list (titled 118 calculations; the tables hold 125 rows) to DSL expressions, each evaluated on 1d, 1w, 5m and 15m lanes for finiteness and causality, or records why it is missing (`NOT_IMPLEMENTED` fails; `DATA`/`DEFERRED` warn). |

## Exit codes

`0` = everything passes. `1` = a correctness check (sections 0-5) failed. `2` = correctness passes,
but calculations from the owner's minimum list are still missing (tracked gaps, below).

## Remediation status (2026-11-15)

| First-run finding | Status | Change |
|---|---|---|
| The baseline ignored `exit.when`, so excess returns, p/q-values, gates and GP fitness were biased | Fixed | `strategy_engine.baseline(..., xmask)`. Engine contract `v2`, evolve contract `v2`. GP selection also requires a positive absolute mean net. Regression: the v1 artefact genome (fitness 11.86) now scores <= 0, and GP rediscovers the planted edge `falling(close, 3)` -> long. |
| NR4, NR7-ID, Piercing Line and Dark Cloud Cover did not match their literal formulas | Fixed | Literal translations in `config/strategies/library/catalogue/`. The previous interpretation is kept in `notes`, and the library `.history` keeps the old version. All four now reproduce bar for bar. |
| Indicator warm-up seeding differed from TA-Lib | Fixed | DSL contract `v2`: EMA/RMA seed with the SMA of the first n consecutive defined values; true range is undefined on bar 0, so ATR(n) starts at bar n. This matches TA-Lib SMA/EMA/RSI/ATR and the M5.4 feature path. Residual: TA-Lib's MACD aligns its fast-EMA seed, and its ADX seeds Wilder sums with n-1 values; both differ only during warm-up. TA-Lib is not installed here, so the check is against an independent reference written to the TA-Lib convention. |
| MFI returned 100 with zero volume and started one bar early | Fixed | NaN when the window has no money flow; first value at bar n. |
| "Short-term" etc. read as short trades | Fixed | `m56_catalogue._direction` uses explicit markers plus bearish-by-definition setup names. It now agrees with the curated library for all 340 references. Reference digests of the reclassified entries change, so their M5.6 checkpoints recompute. |
| Tree rules not saved to the library | Fixed | `definition_file` is resolved against the run directory. |
| No trade or ML-search records in real runs | Fixed | `research-matrix` writes `audit/` (below). |
| ADX: M5.4 feature path counts bar-0 directional movement as 0 | WARN, kept | M5.4 is unchanged so cached features and tree results stay reproducible. The difference is warm-up only. |

Results stored before engine v2 are not reused: the engine contract is part of every cache and lane key,
so the next `research-matrix` run recomputes all evidence. A final segment already read under v1 is not
read again (the once-only rule holds); such rows say so in `reasons`. Strategies discovered under v1 keep
their old `evidence` block until they are rediscovered.

## Tracked gaps (owner's minimum list; exit code 2 until implemented)

Not implemented (11): quadratic curve fit, Weis wave volume, order-block detector, VCP contraction
engine, trendline/pitchfork fit, point & figure, DTW distance, ADF test, HMM regime posterior, kNN
analogues, meta-labelling. Partial (5): CPR (virgin-CPR tracking), SMI, FVG (mitigation tracking),
chart-pattern geometry, Fibonacci (harmonics). Rows that need data not held locally (delivery,
free float, F&O/options, pre-open, breadth, NSE calendars) are WARN, not FAIL. The source of truth is
`config/verify/primitives.json` (`missing[].kind`).

## Audit trail in real research runs

`research-matrix` writes, per stock x timeframe lane, atomically and before the lane is committed
(an interrupted lane is recomputed and overwritten; a completed lane is never rewritten):

| File | Contents |
|---|---|
| `audit/trades/<SYMBOL>/<tf>.csv.gz` | Every validation and final-segment trade of every tested strategy: strategy id/sha, params, segment, signal/entry/exit bar dates, prices, exit reason, gross/net return, bars held, MFE/MAE |
| `audit/ml/<SYMBOL>/<tf>.jsonl.gz` | Every GP genome evaluated (generation, entry, exit, fitness, trades, mean/excess net, baseline) and every tree/screen candidate with its validation evidence and library id |
| `audit/index.jsonl` | One line per committed lane: files, row counts, SHA-256 of the uncompressed content |

Config, in the `research` section: `"audit": {"trades": "validation_final" | "all" | "none", "ml_log": true}`.
`all` also keeps the discovery-segment trades of the chosen configuration (about 3x larger). Storage
is gzip CSV/JSONL, roughly 20-30 bytes per trade, so a 1,000-lane run with ~5,000 trades per lane is on
the order of 100-150 MB.

## Audit trail produced

`strategies.csv` (original rule, DSL rule, verdict, bar-level agreement, mismatch examples),
`indicators.csv` (error, warm-up, textbook deviation, sample values), `primitives.csv`,
`engine-trades.csv`, `ml-evolve-log.jsonl` (every genome evaluated), `verify-report.json`,
`audit-manifest.json` (git commit, library versions and the SHA-256 of every input and code file),
and `work/pipeline/research-out/` (the end-to-end run's `matrix.sqlite` and `summary.md`).

## Extending

- New indicator: add a reference function to `verify/reference.py` and one line to
  `INDICATORS` in `verify/suite_indicators.py`.
- New primitive requirement: add a row to `config/verify/primitives.json` (no code).
- New catalogue entry: picked up automatically. If its pseudo-code uses only the subset in
  `verify/specparse.py`, it is also verified semantically.
