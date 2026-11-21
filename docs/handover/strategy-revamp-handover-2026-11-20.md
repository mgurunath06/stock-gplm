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

## Session 3 (2026-11-20 late night) - state for the next session
- Library = owner Chartink (85) + GN (7) only; catalogue, O1 and ~32.6k ML rules parked (see commit dc4fb62).
- Exit (owner): stop 0.2% beyond last swing low/high, candle low/high when the swing is > max_swing_pct away;
  intraday until stop/target/15:15, daily until stop/target (OPEN_END at range end). Targets learned per stock x TF x
  strategy for `exits.json` target_win_rate 0.70 (rr 0.3-3) by `forward_test --learn-exits`.
- No date lock (`development_end = "latest"`), no date splits; judge by Confidence/RR/Edge (`confidence.py`), ACTIVE
  thresholds in `config/strategies/confidence.json` (default min_confidence 0.70, min_edge 0, min_trades 10).
  Re-score without rerunning trades: `python -m stock_gplm.app.confidence <forward run dir>`.
- Results: `state/reports/forward/watchlist-2026-v3/` (50 stocks; 90 ACTIVE combos, 74.5% win, +0.18%/trade net, in-sample).
- Overnight chain `state/reports/pipeline/2026-11-20/overnight_chain.cmd` (writes overnight.done): pull indices,
  forward_batch `complete-2026` (330 stocks complete from 19 Jan, all TFs), `universe-1d-2026` (daily, whole universe).
- Open: live alerts should use `active.json` + learned exits (live lab still on old library/ATR exits); nightly
  automatic re-run; unblock GN-StrongestKidInClass after checking index rows; calendar lacks special sessions
  (1 Feb Budget, 8 Nov Muhurat) so their minute data was never fetched.

## Session 4 (2026-11-21, Saturday) - start here
Results (in-sample: exits learned and judged on the same 2026 data):
- `state/reports/forward/complete-2026/` (330 stocks, all TFs, from 19 Jan): ACTIVE 598 combos on 238 stocks, 18,697 trades,
  75.0% win, +0.32%/trade net (1d 310, 15m 143, 60m 92, 5m 53).
- `state/reports/forward/universe-1d-2026/` (daily, 1,002 liquid stocks, from 1 Jan): ACTIVE 500 combos on 298 stocks,
  6,863 trades, 77.4% win, +1.04%/trade net.
- Each dir: `ALL_STOCKS.md`, `active.json`, `confidence.csv`. Top: Bear 5 EMA - Daily (short), 44EMA High,
  9 gt 20 at 45 deg, GM W Pattern, GM Bullish Daily Pinbar, 44EMA Low, 5 day price increase, GMAnalysis.
- Fix 89d4aff: signal exit on the latest candle with hold-to-target -> OPEN_END (5 stocks had crashed; re-run, reports rebuilt).
- Long-daily pipeline (`state/reports/pipeline/2026-11-20/SUMMARY.md`) finished 2026-11-21 13:23: NSE MCP history for 2,690
  stocks (1,507 extended), Yahoo history, up-move 1d long (`state-long/reports/upmove/universe-1d-long`), recency long.

Cloud (sne-cloud):
- Shoonya token expired 2026-11-21 09:38 -> backfill stalled until the owner gave a fresh OAuth code; login must use the
  venv: `printf '%s\n' <code> | ssh -T sne-cloud '~/stock-gplm-venv/bin/python ~/shoonya_session.py'`
  (system python3 lacks NorenRestApiPy). Backfill resumed by itself (sweep 2; 648/2,690 stocks complete from 19 Jan).
- The token expires daily. The auto re-login (m25 auth service/timer) exists only as UNCOMMITTED local edits
  (`deploy/oracle/m25/*`, `src/stock_gplm/m25/browser.py`, `tests/test_m25_auth.py`, docs) and is NOT installed on the cloud.
- Index history cron (`5 17,20 * * *`) runs fine (153 indices).

PC processes (since 2026-11-20 16:07): `live_lab --port 8765` (still OLD library -> restart needed), `universe_sync`
(pulls cloud minute files), `cloud_retention` (deletes cloud files once safe on PC). No training running; GPU idle.

Next (proposed, owner approval pending):
1. Long-history check (recommended to owner, awaiting go): forward test Chartink+GN on pre-2026 daily history with the
   2026 exits frozen (honest out-of-sample check of the 75-77% win rates), then re-learn exits on full history and rebuild
   ACTIVE/reports side by side. CPU, detached, writes a done-file.
2. Review + install the m25 auto re-login on the cloud.
3. Live alerts from `active.json` + learned exits; restart live lab on the 92-strategy library.
4. Nightly automatic re-run; `exits.json` os.replace retry on Windows contention (~20 first-attempt failures per batch).
5. Unblock GN-StrongestKidInClass (index rows present now); special sessions in the calendar.
6. Re-run 2026 forward tests once the cloud backfill completes more stocks.
7. LLM strategy variations (GPU, local qwen3:14b) with guardrails, gated by the long-history check.
Never commit: `config/strategies/ChartInk/` (screenshots with P&L/email), `*.mp4`, `tests/test_research_feed_window.py`.

## Session 5 (2026-11-21 evening) - long-history check running
- Owner gave the go for step 1. Chain `state/reports/pipeline/2026-11-21/long_check_chain.cmd` (detached, 20 workers,
  rerun to resume) writes to `state-long/reports/forward/long-1d/`:
  A) `oos-frozen2026/` pre-2026 daily (2000..2025) with the 2026 exits frozen (`exits-2026-frozen.json`) -> `COMPARE.md`, `A.done`;
  B) `full-learned/` 2000..latest with exits re-learned into `exits-long.json` (config exits.json untouched) ->
  `COMPARE.md` (adds the full-history section), `long_check.done`. Adopting exits-long.json = owner decision.
- `GPLM_EXITS_FILE` env var points the engine/learner at another exits file. `os.replace` retry added (step 4 half done).
- Bug fixed: daily hold-to-target trades were dropped when ANY blocked bar came later in the range (RELIANCE lost all
  trades before 2009-05-18). Now only the trade's own path is checked. 2026 reports were built with the old check
  (minor effect; re-run when convenient).
- Smoke (3 stocks): 2026-ACTIVE combos keep ~78% win pre-2026 but avg net drops to ~+0.07% (low-RR targets) - watch avg/PF, not win rate.
