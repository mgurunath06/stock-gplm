# CURRENT STATE — M5.4 unified daily+minute strategy discovery

Updated 2026-11-15. Read this first after a context reset.

## Location

| Item | Value |
| --- | --- |
| Worktree / branch | `/home/mgurunath/projects/stock-gplm-m54`, `feature/m54-discovery` (local only; not pushed or merged) |
| Code commit | `2c9064d` feat(m54): unified daily+minute multi-timeframe strategy discovery (then a docs commit) |
| Untouched | `stock-gplm` main checkout (Codex's uncommitted M5.3), `stock-gplm-m45` (Codex), `stock-gplm-claude` |
| Python | `/home/mgurunath/projects/stock-gplm/.venv/bin/python` via `bin/gplm` |

## Unified engine (done, tested)

`bin/gplm discover --stock RELIANCE --data all-available --timeframes auto --budget standard --device auto --resume`
See `docs/m54-unified-discovery.md`. Modules: `m54/candles.py`, `timeframes.py`, `backtest.py`, `recency.py`,
`unified.py`, `unified_report.py`, `unified_bulk.py`, `fixture.py`. Config: `config/m54/unified-default.json` and
`unified-budgets.json`. Without `--data` or `--timeframes`, the legacy daily engine runs unchanged.

Tests: full suite **938 passed, 2 skipped** (`PYTHONPATH=src …/.venv/bin/python -m pytest -q`). The 17 unified tests
are in `tests/test_m54_unified.py` and are synthetic only.

## Real-data acceptance (daily only; minute BLOCKED)

The M3.2 cutoff is 2025-10-21. All 27,110 local minute files (2026) are excluded whole, and their payloads are never
opened; only sidecar metadata is read. The 2025 daily file straddles the cutoff and stays withheld. The daily lane
uses 527 bars (2022-10-31 → 2024-12-31) with weekly cross-timeframe confirmation. The weekly lane has 114 bars, below
its 150-bar minimum, so it is used as higher-timeframe context only.

| Stock | Run | Discovery tests | Min BH q | Tiers |
| --- | --- | ---: | ---: | --- |
| RELIANCE | `787834d3…` | 8618 | 0.0059 | 11 REJECTED_VALIDATION, 1 VALIDATION_ONLY |
| HDFCBANK | `948951de…` | 8640 | 0.359 | 12 REJECTED_VALIDATION |
| ICICIBANK | `736157fa…` | 8627 | 0.136 | 1 PROMISING_NOT_SIGNIFICANT, 11 REJECTED_VALIDATION |
| INFY | `db6f11ab…` | 8644 | 0.0088 | 12 REJECTED_VALIDATION |
| SBIN | `f5579b81…` | 8410 | 0.0014 | 12 REJECTED_VALIDATION |

**No robust strategy for any stock.** Reports: `reports/m54/unified/<SYM>-EQ/<run>/report.md`. Batch:
`reports/m54/batches/b86497b7…/summary.csv`. Timings: RELIANCE 4.0 s, repeat 0.7 s; five-stock batch 4.3 s.

## Minute-path acceptance (synthetic fixture)

`bin/gplm fixture --out reports/m54-fixture/data`, then the same discover command with
`--daily-root reports/m54-fixture/data/daily/NSE --minute-root reports/m54-fixture/data/minute/NSE --output-root reports/m54-fixture/out`.
Run `29ab8e73…` covered lanes 5m, 15m, 30m, 60m and 1d (47 stages) in 30 s; the repeat was reused in 1.8 s.
44 holdout minute sessions and one straddling daily file were excluded whole.

## Blockers and decisions for the user

1. **Real minute data** needs the independent 2026 study authorization (`docs/m54-minute-2026-study-authorization-DRAFT.md`).
   The precondition is to score or retire M3.2 first. Minute production acceptance is BLOCKED until then.
2. AGENTS.md (main checkout) does not name M5.4 or the unified engine. Add a scope rule when accepting.
3. Governed M5 trust root (T4) is still not installed. That is unchanged and does not affect this engine.
4. The V1 MACD dialect conflict with Codex's M5.3 is unchanged.

## Next commands

```bash
cd /home/mgurunath/projects/stock-gplm-m54
bin/gplm report --stock ICICIBANK --print
bin/gplm discover --universe all-eligible --data all-available --budget quick --workers 8 --max-memory-gb 3 --dry-run
bin/gplm discover --stocks RELIANCE,HDFCBANK,ICICIBANK,INFY,SBIN --data all-available --budget deep --workers 5 --max-memory-gb 4
```

## Next engineering actions

1. Independent (Codex) review of `unified.py` timing, purging and multiple-testing counts.
2. Cross-stock pooled hypothesis view with pooled BH.
3. A `gplm backtest --strategy <user file>` re-evaluation command for edited library copies (diagnostic only).
4. After authorization: an `AUTHORIZED_STUDY` policy basis as specified in the draft.

## Recovery

Re-run the same command. Completed runs are verified and reused, completed stages and chunks are reused, and outputs
are rebuilt from checkpoints if missing. A STOP file halts at the next checkpoint.

## History: Claude five-stock MVP (superseded for discovery)

`stock_gplm.mvp` remains the V1-strategy diagnostic runner (`config/mvp/five-stock-diagnostic.json`,
results in `stock-gplm-claude/reports/mvp5/`). Its loader is reused by M5.4.
