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

The exit code is `0` when no check FAILs (warnings are allowed) and `1` otherwise. Open
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

## Findings at the time of writing (commit of this document)

1. **Baseline bias (FAIL, validation).** `strategy_engine.baseline()` ignores `exit.when`, so a
   rule with a signal exit is compared with a baseline that holds to the time or stop exit. This
   mis-states excess return (up to 1.3% per trade on the fixtures), and with it p-values,
   q-values, gates and GP fitness. On the planted-edge fixture every selected GP genome uses the
   signal-exit gene and has *negative* absolute net return. The fix is small: pass the exit mask
   into the baseline simulation. It changes research results, so it needs an owner decision.
2. **Four interpretations labelled EXACT do not match the literal pseudo-code (FAIL,
   interpretation).** C4-004 NR4 and C4-005 NR7-ID evaluate `MIN(Range, n)` over a window shifted
   one bar back. C5-007 Piercing Line and C5-008 Dark Cloud Cover add candle-colour conditions
   taken from the prose. Each needs either a relabel to `APPROXIMATION` with the assumption stated,
   or a literal translation.
3. **Seeding convention (WARN, indicators).** DSL EMA/RMA-based indicators (EMA, RSI, ATR, MACD,
   ADX, DI, Supertrend) seed at the first bar. TA-Lib/Wilder and the M5.4 feature path seed with an
   SMA. Values agree to 0.1% only after about 50–530 bars (EMA200: about 530), so early-lane signals
   can differ from a charting platform. The arithmetic itself matches the reference to 1e-8.
4. **MFI conventions (WARN).** The DSL defines MFI one bar earlier than the textbook, and returns
   100 for a zero-volume window.
5. **Catalogue direction metadata (WARN).** The M5.6 parser marks 13 references as short because
   of substrings such as "Short-term". M5.7 uses the library's explicit `direction`, so this is
   metadata only.
6. **Coverage gaps (FAIL, minimum list).** Eleven listed calculations are not implemented: quadratic
   fit, Weis wave, order blocks, VCP, trendline/pitchfork, point & figure, DTW, ADF, HMM, kNN and
   meta-labelling. Five more are partial: virgin CPR, SMI, FVG mitigation, pattern geometry and
   harmonics. Rows needing delivery, free-float, F&O/options, pre-open, breadth or calendar data
   are WARN (`DATA`/`DEFERRED`).
7. **Tree rules (WARN, ML).** In the synthetic run, tree rows stayed in the matrix and were not
   written to the library because their definition files were not found. This is the known
   limitation in `docs/m57-strategy-engine.md`.

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
