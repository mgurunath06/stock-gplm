# CURRENT STATE — Claude five-stock MVP

Updated 2026-11-15.

## Location

| Item | Value |
| --- | --- |
| Worktree | `/home/mgurunath/projects/stock-gplm-claude` |
| Branch | `feature/claude-five-stock-mvp` (local only; not pushed or merged) |
| Base | `5c34813` (committed head of `feature/m52-data-training-readiness`) |
| HEAD | see `git log -1`; MVP code commit `fee5744` |
| Concurrent work (not touched) | `/home/mgurunath/projects/stock-gplm-m45` holds Codex's uncommitted M5.2 work (`src/stock_gplm/m52/`, `AGENTS.md`). The main checkout `/home/mgurunath/projects/stock-gplm` is read-only for data and M3.2 reports. |

## Completed and verified

1. **Five-stock diagnostic backtest on genuine daily data.** RELIANCE, HDFCBANK,
   ICICIBANK, INFY and SBIN (`-EQ`) were all accepted, with 527 bars each from
   2022-10-31 to 2024-12-31. There are 5 strategies × 3 horizons = 15 runs, with
   evaluation from 2023-11-01 to 2024-12-31. Results:
   `reports/mvp5/five-stock-diagnostic-v1/` (gitignored). Code hash `a7b47399…`.
2. **V1 indicators** `RSI`, `MACD`, `ATR` and `VOLATILITY` were added. Existing
   strategy hashes are unchanged (tested).
3. **Resumability.** A second run skipped all 15 runs. A fresh output root
   reproduces byte-identical run directories (synthetic test).
4. **Raw immutability.** SHA-256 of all five symbols' raw files is identical
   before and after the run.
5. **Research interfaces** (`mvp.research`): rolling windows, equal or exponential
   half-life weights, a comparison grid and the causal intraday join protocol.
   Nothing is fitted.
6. **Tests.** The full suite on `7ecdcb0` gives 896 passed and 2 skipped (real-data
   fixtures are absent in this worktree). Command:
   `PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q`

## Reproduce

```bash
cd /home/mgurunath/projects/stock-gplm-claude
PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m stock_gplm.mvp.cli run \
  --experiment config/mvp/five-stock-diagnostic.json --output-root reports/mvp5/five-stock-diagnostic-v1
```

## Data, manifests and decisions

- Holdout pin: M3.2 run `26e41d00…`, split plan SHA-256 `5c0b0a9d…`, global cutoff
  **2025-10-21**. Only segments whose window ends before the cutoff are opened.
  Per-symbol bar hashes and opened/withheld segments are in `data-manifest.json`.
- **The M5 runner is blocked.** `stock_gplm.m5.cli backtest` returns
  `HOLDOUT_BOUNDARY_UNVERIFIED` for every symbol, in both modes, until a governed
  trust root is installed. That is a T4 action, not done and not bypassed. The
  MVP is a separate diagnostic path.
- Costs are assumptions. The price basis is unverified (see
  `docs/mvp-five-stock.md`).
- No minute file was opened. No model fit, trial, holdout read, ranking, push or
  merge took place.

## Blockers and approvals needed

1. **Recent daily data (2025–2026).** All of it is at or after the M3.2 cutoff.
   Using it means consuming the M3.2 holdout, or scoring M3.2 first and then
   declaring a new study. This needs an explicit decision.
2. **2026 minute study** (`config/mvp/minute-2026-experiment.DRAFT.json`). Minimal
   approval needed:
   - (a) the copy-only reconciliation into a new versioned minute root has run, with
     its hash manifest;
   - (b) authorization to decode that root for this separate study;
   - (c) acceptance that 2026 dates are consumed for this study, after M3.2 is
     scored once (or M3.2 is formally retired);
   - (d) partitions and the trial budget in the draft are approved.
3. **Universe expansion.** There is no local point-in-time NIFTY constituent
   record. Expansion with current membership must be labelled
   `CURRENT_CONSTITUENT_DIAGNOSTIC`.

## Next action

Expand to a NIFTY 50 current-constituent diagnostic. Add a symbol-list file and an
experiment config with the same runner. Symbols that fail integrity are
quarantined automatically. Process the list in batches by splitting experiment
files. Completed runs are skipped.

## Recovery

When interrupted, re-run the same command. Completed `runs/<id>/run.json` are
skipped, and any `*.partial` directory is rebuilt. If the code or inputs change,
new run IDs are produced and old runs stay as stale evidence.
