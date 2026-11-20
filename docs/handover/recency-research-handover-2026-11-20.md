# Handover: rules reset, NSE MCP history, recency research design (2026-11-20, late evening)

Continues `research-automation-handover-2026-11-20.md` (read its "What runs where" + caveats). Branch `feature/m6-prd-v1`.
Committed: `6adbb03` (all work up to the scorecard), `a6fce0a` (handover). **Everything below is uncommitted.**

## Owner decisions today (binding)
1. **Personal, non-commercial research. No self-imposed constraints.** AGENTS.md rewritten: old holdout, M3.x gates,
   M5.2 tiers, "reference-only" NSE MCP and licence gates are gone. Keep hygiene (raw immutable, no fake candles,
   provenance, realistic costs, no false claims). Memory: `personal-research-rules.md`.
2. **NSE data only via the official NSE MCP** (`stock_gplm.m39.client`), never scraping the NSE website.
3. **Testing:** agents run only basic tests; complete testing = a program the owner runs (`tests/owner/`).
4. **Find what works NOW** (all TFs). Stock behaviour changes; strategies found on old candles and dropped when they
   fail recently is the wrong approach. See "Next" below.
5. Owner wants very short, plain answers (no jargon, no long tables).

## Uncommitted changes
| File | What |
|---|---|
| `AGENTS.md` | rewritten rules (above) |
| `src/stock_gplm/app/nse_history.py` | NSE MCP daily history: 3-month chunks (NSE max), steps past NSE's holes, adjusts splits via prevClose, joins Shoonya on first 20 overlap days, writes Shoonya-format files to `data/derived/nse-mcp-adjusted/NSE/<SYM>-EQ/`, writes `config/app/gplm.windows.longdaily.json` (home `state-long`), optional `--research` (1d,1w). |
| `src/stock_gplm/app/settings.py`, `ingest.py` | optional `data.daily_extra_roots` (only the longdaily config uses it; running research unaffected) |
| `tests/test_nse_history.py` | 1 synthetic test (passed) |
| `tests/owner/check_nse_history.py` | owner end-to-end check, live MCP, temp dir: **11/11 passed** (RELIANCE, TCS) |
| `docs/m81-test-budget.md`, `docs/m39-nse-mcp.md` (note appended) | |

Run: `$env:PYTHONPATH="src"; python -m stock_gplm.app.nse_history all --stocks @deploy/oracle/live/universe.txt --research`

## Findings
- **NSE MCP history effectively starts 2021-11-22** (older = "Symbol not found"), has holes (Dec 2023 missing,
  Jun 2023 one day), raw (unadjusted) prices with prevClose. Adds only ~263 days before Shoonya (2022-10-31).
  10+ years needs another source; Yahoo (`.NS`, adjusted, free, unofficial) was suggested — owner not yet decided.
- Why 1d/1w show nothing: ~940 daily / ~195 weekly candles to 2026-08-13; research splits 60% discover / 20% validate
  (~190 days, 5–10 trades) / 20% final; gates: ≥20 val trades, BH q≤0.05 over ~700 tests/lane, 3 of 4 folds positive
  (`m56_status.GatePolicy`, `research._dev_segments`, `research._discovered`). Too strict + judges on old data.
- Strategy grid page (private): https://claude.ai/artifact/A8tKKtNAZC5vB8XuaEhVcr — data builder
  `build_grid.py` was in the session scratchpad (gone after reboot); 0 validated, 100 promising, 782 discovered.
- Research feeder status at 15:58: intraday deep done for 40 stocks; LLM intraday running; scorecard not yet built.

## Next (owner to confirm the design, then build)
**Recency research mode** (all TFs), proposed and explained to the owner, awaiting "go":
1. Rank every strategy per stock×TF on a recent window only, recency-weighted (5m/15m ≈ 1 month, 30m/60m ≈ 3 months,
   1d ≈ 12 months, 1w ≈ 3 years).
2. Validate the *method*, not the strategy: walk back through history; at each step pick the top strategies from the
   prior window and check they made money in the following week/month. Assign a strategy to a stock×TF only where
   "recent winner keeps winning" holds.
3. Re-rank continuously; strategies rotate in/out; drop on recent decay. Final judge: Aug–Nov 2026 test period.
Deliver as a new research mode + `tests/owner/` check program. Then: live lab alerts from the assigned list (old TODO 4).

Older TODOs still open (see previous handover): commit, scorecard summary when ready, PWA/mobile, Yahoo history (if OK'd).

## Next-chat prompt
> Read docs/handover/recency-research-handover-2026-11-20.md and continue.

## Update (later session, same day): recency mode + Yahoo BUILT (owner said "go" and "yes, Yahoo")
- Commit `3014c74`: everything listed under "Uncommitted changes" above.
- New: `src/stock_gplm/app/recency.py` (design exactly as "Next" above; details `docs/recency-research.md`),
  `src/stock_gplm/app/yahoo_history.py` (Yahoo `.NS` daily since 2000, only dates older than NSE MCP/Shoonya),
  `nse_history.build` now rebuilds Yahoo afterwards (no overlap); longdaily config includes the Yahoo root.
- Tests: `tests/test_recency.py` (3), `tests/test_yahoo_history.py` (2) pass. Owner programs:
  `tests/owner/check_recency.py` (10/10 on RELIANCE 1d+15m, incl. no-look-ahead), `tests/owner/check_yahoo_history.py`
  (8/8 on RELIANCE: from 2000-01-03, join +0.93%).
- Smoke RELIANCE: 1d and 15m both NOT_USED (picks lose less than "everything active" but still lose after costs).
- Owner to run (heavy): NSE history first, then Yahoo, for the universe, then recency on 50 stocks all TFs and long 1d/1w
  (commands in `docs/recency-research.md`). Then: live lab alerts from `assigned.csv` (old TODO 4).

## Update (owner, 2026-11-20): NSE first; recent candles weigh more
- `yahoo_history build` skips stocks without NSE MCP history; the owner check fails fast on that precondition.
- Recency weight half-life is now 0.25 × lookback (was 0.5); the oldest trade in the window counts 1/16. `--half-life` overrides it. The policy is part of the lane key, so the next run recomputes every lane.

## Update (owner, 2026-11-20, later): always all history; last 100 candles weigh more (contract v2)
- Score = 0.7 x (last 100 candles, weight 1-age/100) + 0.3 x (all older trades, equal weight), both shrunk (prior 3).
  No lookback cut-off; `Window.lookback_days` -> `warmup_days`. CLI `--recent-bars`, `--recent-share`.
- Top-5 1d (NSE MCP, data from 2021-11-22): BHARTIARTL WATCH (dev +1.43%/trade vs ctrl +0.25%, t 2.6), ICICIBANK WATCH,
  HDFCBANK/RELIANCE/TCS NOT_USED. Output state-long/reports/recency/top5-1d.
- Open point: rare strategies (5 trades, none recent) can top the list on a big old mean; consider higher 1d min_trades.

## Update (owner, 2026-11-20, latest): no 13-Aug split; rolling recency-weighted track record
- verdict() = one rolling track record over all steps (last 12 steps 70%, shrunk by prior 3 to the long-run record;
  older 30%). Status ASSIGNED/WATCH/NOT_USED; DROPPED_IN_TEST removed. Steps: 5m/15m 2d, 30m/60m 5d, 1d 7d, 1w 28d.
- Top-5 1d: only ICICIBANK ASSIGNED (weighted +1.59% vs +0.93%, but plain +0.01%, t 0.03 -> weak).
  BHARTIARTL NOT_USED (plain +1.31%, t 2.6, but recent picks trail the control +1.42%).
- Proposed next (owner to choose): regime-matched ranking (bull/bear/sideways), wider parameter search.

## Update (owner, 2026-11-20, latest 2): smooth decay, no 70:30
- Weight w = (1 + age_candles/100)^-2 everywhere (ranking and track record); `--decay-bars`, `--decay-power`.
  Track record needs >= 10 *effective* trades ((sum w)^2/sum w^2). recent_bars/recent_share/recent_steps removed.
- Firing-rate fact (top-5 1d): ~26 trades/variant over 5 years (~0.45/month); busiest ~0.8/month (no new entry while
  holding, holds 6-30 bars). Top picks show 1-5 trades in the last 100 candles.
- Top-5 1d now: ICICIBANK, RELIANCE ASSIGNED (weak: plain averages negative); others NOT_USED. Verdicts flip with
  weighting choices -> no robust 1d edge on these 5; judge on 50 stocks before more tuning.

## Update (owner, 2026-11-20, latest 3): up-move learner + owner basic strategies
- `src/stock_gplm/app/upmove.py` + `docs/upmove-learner.md` + `tests/test_upmove.py` (3 pass). Confidence / RR / Edge
  per trigger, live-like walk-forward check, today's firings. Top-5 1d: qualified RELIANCE 3, HDFCBANK 25, TCS 0,
  BHARTIARTL 14, ICICIBANK 14. ~760 triggers tested -> some qualify by luck; weighted live-like avg leans on few recent firings.
- `config/strategies/library/owner/owner-basics.json`: O1-001..009 (library now 359).
- Next: run on 50 stocks; feed QUALIFIED triggers into live-lab alerts with Confidence/RR/Edge.
