# M5 real-data backtest report — 2026-11-14

**Status: strict real-data backtesting is BLOCKED.** No genuine dataset satisfies
`STRICT_RESEARCH` (see [m5-eligibility-audit.md](m5-eligibility-audit.md)). The
runs below are `DIAGNOSTIC_ONLY_NON_INVESTABLE`. They show that the pipeline
runs end to end on genuine local files. They are **not** validated historical
performance, cannot be ranked or promoted, and say nothing about whether either
strategy works.

## Setup

| Item | Value |
| --- | --- |
| Code | `658782b9c01f20b549f7a36a5580baf205fbf39d`, clean worktree; V1/M5 source hash `331c5206…` |
| Universe rule (recorded in `reports/m5/demo-universe-2026-11-14.json` before any run) | RELIANCE-EQ and TCS-EQ (the M3.1 exploration symbols), plus the first 18 symbols in sorted order that are not ETFs by the heuristic and have all five standard windows published and accepted (1,510 symbols qualify) |
| Symbols | RELIANCE, TCS, 20MICRONS, 21STCENMGM, 3MINDIA, 3PLAND, 5PAISA, 63MOONS, A2ZINFRA, AAATECH, AAKASH, AAREYDRUGS, AARON, AARTIDRUGS, AARTIIND, AARVI, AAVAS, ABB, ABBOTINDIA, ABCAPITAL (all `-EQ`) |
| Data range assessed | 2022-10-31 to 2025-10-20 (cutoff 2025-10-21 withheld) |
| Evaluation range requested / actual | 2023-11-01 to 2025-10-20 / 2023-11-01 to 2025-10-20, 483 sessions; earlier bars are warm-up only |
| Session axis | union of released observations (no registered calendar) |
| Price basis | `PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED`; no corporate-action adjustment applied |
| Dataset fingerprint | `e75fe0ccc1f11177513075d1f2a227f1f96bcd7f01ea35721f015fdd39103c99` (identical for both runs) |
| Portfolio | INR 1,000,000; at most 10 positions; position fraction from the strategy, capped at 0.2; gross exposure ≤ 1 (no margin); whole shares |
| Costs per side | strategy commission 5 bps + slippage 10 bps; statutory 12.41 bps on buys, 10.91 bps on sells (M3.5 policy values, labelled as an assumption) |
| Execution | `completed_close_signal_next_session_open_v2`; gap-through stop fills at the open; target capped at its level; stop fills first when both are touched |

## Results (diagnostic only)

| Metric | swing-breakout-v1 | mean-reversion-v1 |
| --- | ---: | ---: |
| Strategy version / config hash | v1 / `7458ff27…f391` | v1 / `a0fb58a5…ad23` |
| Run ID | `5fc5c727f3f9…` | `c7d9b0f4b393…` |
| Entry signals | 41 | 235 |
| Closed trades | 40 | 234 |
| Winning / losing | 11 / 29 | 120 / 114 |
| Win rate | 27.5% | 51.3% |
| Gross P&L (before costs and slippage) | −10,998 | +39,859 |
| Net realized P&L | −32,386 | −19,630 |
| Fees charged | 13,366 | 37,174 |
| Unrealized at range end | +7,766 (1 open: ABCAPITAL) | −950 (1 open: AARVI) |
| Total return (includes final marks) | −2.46% | −2.06% |
| Annualized return | −1.29% | −1.08% |
| Maximum drawdown | −12.52% | −4.86% |
| Average / median trade return | −0.78% / −6.65% | −0.16% / +0.04% |
| Profit factor | 0.845 | 0.926 |
| Average holding (sessions) | 12.3 | 3.7 |
| Average gross exposure | 10.7% | 7.2% |
| Sessions with a position | 59.6% | 66.7% |
| Turnover (traded notional / mean equity) | 8.15 | 22.38 |
| Ending equity | 975,381 | 979,420 |
| Sharpe / Sortino (annualized, rf = 0) | −0.28 / −0.43 | −0.35 / −0.49 |
| Exit reasons | stop 15, exit condition 12, target 7, target gap 3, stop gap 3 | exit condition 128, stop 77, stop gap 15, target 12, target gap 1, max holding 1 |
| Benchmark: equal-weight buy-and-hold, same 20 symbols, same sessions, same costs | +38.86% | +38.86% |

Sharpe and Sortino are reported because both runs exceed the thresholds (252
sessions, 30 closed trades). They remain diagnostic numbers on an unverified
price basis.

Observations, stated without performance claims:

- Neither configuration made money after costs over this window and universe.
  Both lagged the same-universe buy-and-hold by about 41 percentage points while
  running at low average exposure (7–11%).
- Mean reversion was gross positive and net negative: its 22× turnover cost more
  than its gross edge. This matches the M3.5–M3.8 finding that apparent gross edge
  does not survive costs.
- 18 of the 274 closed trades exited through a gap past the stop. Those trades
  bore losses beyond the configured stop percentage, which a non-gap-aware
  simulator would hide.

These observations come from one small, non-random universe and one window. They
must not drive parameter changes. Retuning on them would spend research budget on
non-investable data.

## Comparison and ranking

`stock-gplm-m5 compare` over the two runs returned `comparable: false` with
reason `only STRICT_RESEARCH runs may be ranked`, and
`ranking_by_total_return: null`. That is the intended behaviour for diagnostic
runs.

## Reproducibility

Each run was executed twice into separate output roots with the same code and
inputs. `run.json`, `signals.json`, `orders.json`, `trades.json`,
`equity-curve.json` and `report.md` were byte-identical; only `created_at_utc` in
`run-manifest.json` differs. Re-running into an existing output root is refused
(`FileExistsError`).

Two earlier runs, `64f77016…` and `ae039de2…`, were produced from the same source
before the M5 commit (`git_worktree_dirty: true`). Their metrics are identical to
the final runs. They are kept unmodified as superseded pre-commit evidence.

Commands (from the primary checkout once M5 is merged, or with
`PYTHONPATH=<worktree>/src`):

```bash
.venv/bin/python -m stock_gplm.m5.cli backtest --evidence reports/m5/evidence-paths-2026-11-14.json \
  --mode DIAGNOSTIC_ONLY --start 2022-10-31 --end 2025-10-20 --symbols <20 symbols above> \
  --settings config/m5/backtest-settings.example.json \
  --registry reports/m5/strategy-registry-2026-11-14.sqlite3 \
  --strategy-file config/v1/strategies/swing-breakout-v1.json --output-root reports/m5/runs
```

The same command with `--mode STRICT_RESEARCH` exits with status 2 and reports
`BLOCKED` for all 20 symbols (`reports/m5/cli-strict-mean-reversion-v1-final.json`).
