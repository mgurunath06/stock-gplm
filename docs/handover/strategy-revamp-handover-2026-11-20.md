# Handover 2026-11-20 (latest): strategy revamp, Chartink + GeminiNeon library, shorts, Nifty 50

Read first: memory `trading-rules-2026-11-20`, `pc-capability`, `no-polling-automate`. Previous handover:
`upmove-pipeline-handover-2026-11-20.md` (up-move learner + 4-lane pipeline).

## Owner decisions in this session (binding)
1. Library revamp approved: dead / rare / blocked catalogue rules parked in `config/strategies/parked/` (not loaded,
   restorable, verdict in `notes`). Active catalogue = 155 (12 intraday-supported + 143 weak-positive).
2. Owner's Chartink scans are the core library, **named exactly as in Chartink** (O2-xxx); versions share the name and
   keep their own ID ("Version: ..." in `description`).
3. **Shorts are first-class:** owner trades them as intraday shorts or PE buys. Tags: `execution:intraday-short-or-PE`
   (intraday), `execution:PE` (daily/weekly), `execution:cash-long` / `execution:intraday-long-or-CE` for longs.
4. Liquidity floor on every new rule (median 20-day turnover >= Rs 15 Cr, close >= Rs 50); intraday no entries after 14:45,
   square-off 15:15; ATR label is secondary (ranking stays on +3%/5d/-1.5%); no holdout, per stock x TF judgement.
5. Review spec `docs/strategies/chartink_strategy_library_revised_review_two_gate_spec.md` (owner-supplied): adopted the
   practical parts only (see my review in chat: liquidity, session rules, archetype exits, ATR variants, circuit guard).
6. 7 "GeminiNeon-<Name>" strategies added on owner request.
7. Nifty 50 index: download it from the cloud worker in the background (owner: "don't download yourself").

## What was built (branch feature/m6-prd-v1)
| Commit | What |
|---|---|
| 7920a49 | park 195 dead/rare/blocked catalogue strategies |
| a210338 / b3f96c6 / 3641dd6 | Chartink library `config/strategies/library/owner/chartink.json` (exact names), engine v3 (15:15 / 14:45), up-move v2 (gap-through stops, ATR label, liquidity gate, Chartink state scans as building blocks), DSL `median(x, n)` |
| 2a1eaf7 | shorts: `upmove --side short` (down-move learner, bearish building blocks), pipeline steps downmove_1d/60m/15m, +20 Chartink scans from tmp/ChartInk2.mp4 (Bear 5 EMA series, GM Bearish 15/3 min Pinbar, ...), Claude entries exit on "Claude - Exit - Full Exit" |
| 3c4ff15 | `config/strategies/library/owner/gemini-neon.json` (GN-StrongestKidInClass, GN-MorningBox, GN-NoCeiling52wHigh, GN-CoiledSpringNR7, GN-InstitutionalFairPriceVWAP, GN-QuietPullback, GN-OvernightSurpriseGapAndGo; GN-StrongestKidInClass BLOCKED until index data) |
| (this commit) | cloud worker fetches "Nifty 50" (token 26000) first each sweep; this handover |

Library now: 256 strategies (155 catalogue, 85 Chartink, 9 O1, 7 GeminiNeon); long 54 names / short 27 names
(full table: `docs/strategies/chartink-analysis-2026-11-20.md`). All parse and run on real candles (3-stock smoke).

## Running now (detached, do not poll)
- Pipeline PID 26796 (`state/reports/pipeline/2026-11-20/status.json`): nse_history still RUNNING; up-move 1d/60m/15m and
  recency done (up-move ran partly before v2 code).
- Down-move learner chain: `state/reports/pipeline/2026-11-20/downmove_chain.cmd` (1d -> 60m -> 15m, resumable: rerun the
  file). Results: `state/reports/upmove/universe-{1d,60m,15m}-short/summary.md`, `today.csv`.
- Oracle `~/gplm-live/cloud_sync.py` restarted with `INDICES = {"Nifty 50": "26000"}` (backup
  `cloud_sync.py.bak-pre-index-20261120`). Daily via trading symbol "Nifty 50", minute via token. One-off daily fetch
  already saved 2021-2026 files under `~/StockGPLMDownloads/data/daily/shoonya/NSE/Nifty 50/` (thin: 2024 = 39 rows,
  2025 = 68; 2010-2020 failed) - check row counts before trusting.
- Gotcha: never `pkill -f "python cloud_sync.py"` inside an ssh command string (it kills the ssh session itself).

## Open points / next steps
1. **Nifty 50 into the local store:** cloud_link should pull `Nifty 50` folders; then make `bench()` accept it (it appends
   `-EQ` to symbols today) and unblock GN-StrongestKidInClass / C6 index rules (remove their `blocked` blocks). Verify daily row counts.
2. Read the down-move and up-move `summary.md` files; report triggers qualifying on most stocks + today's firings (long and short).
3. Live alerts: live-lab skips rules using `htf()` (8+ intraday Chartink rules). Add htf support or intraday-native variants.
4. Pre-existing bug: `minute_of_session` = bar START in research, bar END in live-lab (`live_lab.py:202`); align.
5. Not converted: VWAP Cross (not opened in videos), Claude - Exit - 50% Exit (partial exit not modelled),
   SS_Portfolio_Entry_Monthly (needs ~17y). Confirm with owner: GM W Pattern reading, GR - Intraday Sell (mirror),
   GM Bearish Daily Pinbar fix.
6. Pre-existing test failures (fail without these changes too): 4 in `tests/test_m54_unified.py`,
   `test_m57_remediation.py::test_false_gp_advantage_is_gone_and_planted_edge_found`.
7. Untracked, not committed on purpose: `config/strategies/ChartInk/` screenshots (contain P&L / email), `tmp/*.mp4`,
   `tests/test_research_feed_window.py` (owner decision pending from previous chat).

## Session 2 (2026-11-20 night) - done
- **All NSE indices (153, broad + sector + thematic + VIX)** fetched BY THE CLOUD from the official NSE MCP:
  `deploy/oracle/live/index_history.py` (+ `run_index_history.sh`, cron 17:05/20:05 IST, flock). Publishes Shoonya-format
  triplets to `~/StockGPLMDownloads/data/daily/nse-index/NSE/IDX_<NAME>-EQ/`. NSE serves 2024-10-07 onward (~524 rows).
  Raw payloads kept in `~/gplm-live/index/raw` (re-parse without refetch). Shoonya "Nifty 50" daily is gappy - not used.
- PC: `data.index_root` (both windows configs) = `data/raw/nse-index/NSE`; ingestion reads it; live lab CloudSync pulls it
  (outside the shoonya retention ledger); one-shot pull: `python -m stock_gplm.app.cloud_link indices`.
- DSL: `bench(expr, "Nifty Bank")` / `ref_symbol("Nifty IT", ...)` take official index names (aliases NIFTY, BANKNIFTY,
  FINNIFTY, INDIAVIX); default benchmark = official Nifty 50 (`IDX_NIFTY_50-EQ`), no ETF proxy.
- Up-move learner contract v3: average win/loss shrunk toward target/stop (TATATECH RR 120 artefact gone); "firing on
  latest candle" now prints the latest candle date. Rerun chain: `state/reports/pipeline/2026-11-20/learners_v3_chain.cmd`
  (long + short, 1d/15m/60m; writes `learners_v3.done`).
- GeminiNeon IDs are descriptive (owner): GN-StrongestKidInClass, GN-MorningBox, GN-NoCeiling52wHigh, GN-CoiledSpringNR7,
  GN-InstitutionalFairPriceVWAP, GN-QuietPullback, GN-OvernightSurpriseGapAndGo.
- Research feeder (`var/runs/research_feed.py`, untracked) now tracks the library hash per stock
  (`research_feed_done_lib.txt`): stocks researched on an older library are re-run (library lanes only, no LLM) first.
  93 stocks were researched on the old/partial library -> re-queued.
- `python -m stock_gplm.app.strategy_report <research dir> [--stock SYM]` -> STOCK_REPORT*.md (progress per stock + every
  strategy by name). RELIANCE check: `reliance_library_check.cmd` -> `state-reliance-check/reports/research/reliance-library/STOCK_REPORT-RELIANCE.md`.

## Next
1. After the cloud backfill: `python -m stock_gplm.app.cloud_link indices`, check row counts, then remove the `blocked` block of
   GN-StrongestKidInClass (and re-check parked C6 index rules).
2. Daily (1d) library research on the new library: the feeder runs intraday TFs only; most Chartink scans are 1d.
3. Pre-existing failures (also on clean HEAD): `tests/test_verify_suite.py::test_writes_auditable_outputs` (correctness FAIL),
   `tests/test_m5_historical_adapter.py` (Windows temp-dir permission check).
