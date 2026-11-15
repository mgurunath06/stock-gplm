# M5.7 Generic strategy engine and declarative library: handover

Status 2026-11-15: code complete for the stocks-first scope and committed locally (not pushed).
Large research runs are operated by the owner (see "Run it").

## What changed

| Piece | File |
|---|---|
| Strategy expression language (safe parser, no `eval`, 159 causal primitives) | `src/stock_gplm/app/strategy_dsl.py` |
| Dynamic strategy library (multi-root, versions, history, dedupe, edit/restore) | `src/stock_gplm/app/strategy_library.py` |
| Generic engine (long + `SHORT_SYNTHETIC`, discovery-only parameter grid, validation, folds, regimes) | `src/stock_gplm/app/strategy_engine.py` |
| Genetic-programming discovery (`--methods evolve`) | `src/stock_gplm/app/strategy_evolve.py` |
| LLM translate/propose (governed, aggregate-only) | `src/stock_gplm/app/strategy_ai.py` |
| `research-matrix` rewritten on the library; `gplm library ...` command | `research.py`, `cli.py` |
| 340 catalogue entries translated (350 interpretations) | `config/strategies/library/catalogue/*.json` |

No strategy lives in Python. The engine reads every `*.json` under
`config/strategies/library` (immutable defaults) and `<home>/strategy-library` (writable: `user/`,
`discovered/ml`, `discovered/ai`, `state.json`, `.history/`).

## Catalogue coverage on RELIANCE daily, development window (in-process check, 2026-11-15)

| | Before (old engine) | After |
|---|---|---|
| Catalogue references executed and backtested with costs | 25 | 255 |
| Blocked: missing data (F&O, options, delivery, VIX, calendars, free float, minute data in window) | 60 | 39 |
| Deferred: index-relative, no ETF-for-index substitution | – | 14 interpretations |
| Blocked: named implementation gap (harmonics, pitchfork, P&F, Weis wave, ...) | 227 | 10 |
| Discovery methods (336–340) | – | 8 |
| Timeframe or symbol scope (intraday-only rules, stock-specific pairs) | 28 | 24 |

Evolve on the same lane: 226 genomes evaluated on the discovery segment, 5 selected, validated once
and saved to the library. The full lane ran in under 1 s on 727 bars.

## Run it (PowerShell, from `H:\Guru\Learning\StockGLPM\app`)

```powershell
# 1. One stock, whole library + ML discovery
.\windows\gplm.cmd research-matrix --stocks RELIANCE --timeframes 1d --mode both --methods evolve,tree --budget standard --name reliance-1d --device cpu

# 2. A few stocks, daily + weekly, 12 worker processes
.\windows\gplm.cmd research-matrix --stocks RELIANCE,HDFCBANK,TCS,INFY,ICICIBANK,SBIN,LT,ITC --timeframes 1d,1w --methods evolve --budget standard --name pilot-8 --workers 12

# 3. Nifty 500 (put NSE's ind_nifty500list.csv, browser-downloaded, in config\universes\)
.\windows\gplm.cmd research-matrix --universe nifty500 --timeframes 1d --methods evolve --budget quick --name n500-1d --workers 20

# 4. Whole local equity universe (ETF-like names are excluded automatically)
.\windows\gplm.cmd research-matrix --universe all --timeframes 1d --methods evolve --budget quick --name all-1d --workers 20
```

Options:

- `--stocks A,B` / `--universe all|nifty500|file.csv|file.json` / `--max-symbols N`
- `--timeframes 1d,1w` (`5m,15m,30m,60m` are accepted, but no minute data exists inside the development window)
- `--mode both|catalogue|ml`
- `--methods evolve,tree,screen|none`
- `--budget quick|standard|deep` (evolve population × generations: 40×8, 80×15, 160×30)
- `--strategies "C3-*,USR-*,ML_DISCOVERED"` (ids, reference ids or origins; globs allowed)
- `--library DIR` (extra root)
- `--include-disabled`
- `--workers N` (default: CPU count − 2; lanes run in parallel processes)
- `--device cpu|gpu|auto` (only `screen` uses the GPU; the DSL engine and evolve are NumPy on CPU)
- `--start/--end`

`--end` can never pass `m56.development_end`, which is 2025-10-20.

## Outputs

Each run writes to `H:\Guru\Learning\StockGLPM\state\reports\research\<name>\`:

| File | Contents |
|---|---|
| `summary.md`, `summary.json` | Headline counts, coverage, top candidates, cross-stock table |
| `matrix.csv`, `matrix.sqlite` | Every stock × strategy × timeframe row, blocked and rejected included |
| `ranked_candidates.csv` | VALIDATED / EXPERIMENTAL / DISCOVERED rows with entry rule, exit and stop/target text, validation metrics, regime breakdown, lane q and run-wide q |
| `ranked_validated.csv` | VALIDATED rows only |
| `catalogue_coverage.csv` | Per reference: EXECUTED, or the blocking reason |
| `strategy_cross_stock.csv` | Pooled validation evidence per strategy across stocks, BH-adjusted |
| `run-config.json`, `catalogue-report.json` | Run configuration and catalogue parse report |
| `audit/trades/`, `audit/ml/`, `audit/index.jsonl` | Per-trade ledger and every ML candidate evaluated, per lane (see `docs/verification.md`) |

New ML strategies are written to `state\strategy-library\discovered\ml\*.json` and are reloaded by
every later run (`ML_DISCOVERED`, stock-specific applicability).

## Resume

Rerun the identical command with the same `--name`.

- Completed stock × timeframe lanes are skipped.
- Per-strategy results are cached by data fingerprint and definition SHA, so after a library edit
  only new or changed strategies are simulated.
- An interrupted run loses only the lanes in flight.

## Add or edit strategies (no Python)

```powershell
.\windows\gplm.cmd library add config\strategies\examples\user-reliance-gap-reclaim.json
.\windows\gplm.cmd research-matrix --stocks RELIANCE --timeframes 1d --strategies "USR-*" --methods none --name user-demo
.\windows\gplm.cmd library list --origin ML_DISCOVERED
.\windows\gplm.cmd library edit C3-001 --set exit.max_hold_bars=5
.\windows\gplm.cmd library restore C3-001
.\windows\gplm.cmd library disable C1-002
.\windows\gplm.cmd library enable C1-002
.\windows\gplm.cmd library history C3-001
.\windows\gplm.cmd library functions
.\windows\gplm.cmd library compare C3-001 C3-002 --run H:\Guru\Learning\StockGLPM\state\reports\research\reliance-1d
```

LLM translation and proposals are approved for aggregate data only (2026-11-15). To use them, set
`ai.enabled=true` and `ai.allow_remote=true`, provide `ANTHROPIC_API_KEY` in the environment, then:

```powershell
.\windows\gplm.cmd library translate "Buy when ..." --allow-remote --allow-unknown-cost
.\windows\gplm.cmd library propose --run <run dir> --n 10 --allow-remote --allow-unknown-cost
```

## Statistics and statuses

- Parameter grids are fitted on the discovery segment (60%) only.
- The validation segment (20%, purged) is read once per configuration.
- BH correction runs over every fresh validation test on the lane, across all origins. The M5.6
  gates apply: at least 20 trades, q ≤ 0.05, positive net return, drawdown ≥ −30%, positive folds.
- A gate pass triggers the once-only final development-segment read (`final_reads` table).
- Statuses: `VALIDATED` (all gates plus the final segment), `EXPERIMENTAL` (validation passed),
  `DISCOVERED` (promising, p < 0.10, but gates not met), `REJECTED`, `REPLAY` (re-execution on the
  same data it was discovered on; not new evidence).
- The summary also reports a run-wide BH (`global_q_value`).

## Correctness verification

`.\windows\verify.cmd` checks interpretation, indicators, fills, statistics and ML on synthetic fixtures (`docs/verification.md`). Engine v2 / DSL v2 / evolve v2 (2026-11-15) fixed the signal-exit baseline bias and adopted TA-Lib seeding, so results from earlier runs are recomputed automatically.

## Known limitations

- **History is short.** Daily data starts 2022-10-31, so there are about 727 development bars and
  a validation segment of about 115 bars per stock. The 20-trade gate is rarely met per lane, so
  expect few or no VALIDATED rows. Cross-stock pooled evidence is the realistic route; its p-values
  are optimistic because trades share market moves.
- **Intraday rules can't run under this protocol.** Minute data exists only for 2026, after the
  holdout boundary. A separate, owner-authorized 2026 intraday protocol is needed.
- **Deferred and blocked entries stay in the library with exact data requirements:** index-relative
  rules (need official index history), F&O/options/VIX/delivery/free-float/calendars, and 10 named
  primitives.
- **Approximations are labelled.** Cross-sectional "top decile" rules run as time-series
  thresholds (`fidelity=APPROXIMATION` with assumptions). Position sizing is not modelled.
- **Tree rules** are written to the library only when their source definition file is found; the
  matrix row is kept either way.
- **Not built:** methods `knn`/`meta` (339/340) and HMM (311).
