# Handover 2026-11-20 (latest): recency rules, up-move learner, unattended pipeline

Read first: memory `pc-capability`, `personal-research-rules`, `no-polling-automate`. Previous handovers:
`recency-research-handover-2026-11-20.md` (has the step-by-step update log of this session).

## Owner decisions made in this session (binding)
1. **Order: NSE history first, then Yahoo.** `yahoo_history build` skips stocks without NSE MCP history.
2. **Always use all history.** No lookback cut-off; older bull/bear behaviour stays, with less weight.
3. **Weight falls smoothly with age in candles. No hard-coded splits (no 70:30).** Used everywhere:
   w = (1 + age/100)^-2 → 100 candles back = 1/4, 200 = 1/9, 1000 = 1/121, never 0.
4. **No fixed split date (13 Aug dropped).** Judge "does it work" with a rolling, live-like check: each pick is
   scored only on candles after it, weighted by the same decay. No research-on-old / test-on-new.
5. **Up-move (1d) = +3% within 5 candles before −1.5%** (costs included; stop first if both hit in one candle).
6. **Every trigger carries Confidence, RR and Edge.** Edge = Confidence × RR − (1 − Confidence) = expected gain per
   1 unit of risk; rank by Edge (better-paying moves rank higher).
7. Owner wants many entries: daily stocks give ~5–8 "+3% within 5 days" days per month (~2 distinct moves/month);
   catching 7–10/month needs short holds or intraday TFs.
8. **Don't spend tokens watching jobs; use the GPU, CPU and APIs in parallel** (PC specs in memory `pc-capability`).

## What was built (all committed on `feature/m6-prd-v1`)
| Commit | What |
|---|---|
| 20ce8d8 | NSE-first rule for Yahoo; recency half-life (superseded) |
| 92724d7, 3b480c7, 64b8adc | recency: all history, no split, rolling weighted track record, smooth decay (contract v2) |
| 3986184 | `upmove.py` learner + `config/strategies/library/owner/owner-basics.json` (O1-001..009) |
| 4cc7ccc | GPU triple screen, parallel workers, per-TF up-move sizes; `pipeline.py` (4 lanes) |

- `src/stock_gplm/app/recency.py`: ranking score = Σw·net/(Σw+3); track record weighted by the same decay; WORKS
  needs ≥4 steps with trades and ≥10 *effective* trades. Status ASSIGNED / WATCH / NOT_USED. Steps: 5m/15m 2 d,
  30m/60m 5 d, 1d 7 d, 1w 28 d (warm-up 30/91/365/1095 d). CLI `--decay-bars`, `--decay-power`.
- `src/stock_gplm/app/upmove.py` (docs `docs/upmove-learner.md`, tests `tests/test_upmove.py` 3 pass): 27 basic
  conditions (EMA13 × 20/50/100/200, close × EMA13, MACD, RSI, volume/OBV, breakouts, candles, EMA13 stretch/
  snap-back, ATR contraction) + pairs + GPU-screened triples (top 150 confirmed exactly). Live-like check: each
  firing rescored from earlier closed firings; QUALIFIED = Edge > 0, lift > 1, ≥5 taken with positive weighted
  result. Outputs `summary.md` (triggers qualifying on most stocks, best per stock, qualified firing now),
  `triggers.csv`, `today.csv`, `stocks/<SYM>-<tf>.json` (reused while data unchanged).
  Per-TF up-move: 1w 6%/3%/4, 1d 3%/1.5%/5, 60m 1.5%/0.75%/6, 30m 1.2%/0.6%/8, 15m 1%/0.5%/8, 5m 0.6%/0.3%/12.
- Library: O1-001..004 EMA13 × 20/50/100/200, O1-005 MACD × signal, O1-006 RSI × 50, O1-007 volume surge,
  O1-008/009 EMA13 snap-back long/short (stretched > k·ATR, reversal candle, exit when back at EMA13). Library = 359.
- `src/stock_gplm/app/pipeline.py`: lanes api (nse_history → yahoo_history, universe 2,690), gpu (upmove 1d → 60m →
  15m, universe), cpu (recency 15m/60m top 200), long (after api: upmove 1d long + recency 1d/1w long). Resumable.

## Running now (started 2026-11-20 19:48, detached, PID 26796)
`python -m stock_gplm.app.pipeline --run 2026-11-20 --gpu-workers 6 --cpu-workers 16`
Status: `H:\Guru\Learning\StockGLPM\state\reports\pipeline\2026-11-20\status.json`, logs `<step>.log`, final
`SUMMARY.md`. NSE download of 2,690 stocks ≈ 12 h. If it stopped: rerun the same command (resumes).

## Results so far (top 5, 1d, NSE data from 2021)
- Recency: verdicts flipped with each weighting change → no robust 1d edge on 5 stocks; judge on many stocks.
- Up-move learner (pairs version): qualified RELIANCE 3, HDFCBANK 25, TCS 0, BHARTIARTL 14, ICICIBANK 14.
  Best: HDFCBANK "EMA13 × EMA50 + close > prev high" (Conf 50%, RR 2.14, Edge +0.59); ICICIBANK "close × EMA13 +
  ADX > 20" (57%, 1.64, +0.50). ~760–1,345 triggers tested per run → some qualify by luck.

## Caveats / open points
- Decay 100 candles ≈ 4 trading days on 15m: intraday rankings follow the last few sessions (owner's rule; flag
  if results look jumpy).
- Rare strategies can top recency on a big old mean (e.g. 5 trades, none recent); owner not yet asked to decide a
  min-trades / min-recent rule.
- Commit 4cc7ccc accidentally included untracked `tests/test_research_feed_window.py` (not written in this session);
  owner told, awaiting decision.
- Idea offered, not built: market-phase matching (bull/bear/sideways), combine top picks (2 of 3 agree).

## Next steps
1. When the pipeline finishes: read `SUMMARY.md` and the up-move `summary.md` files; report the triggers that
   qualify on the most stocks (believable ones) and today's qualified firings, short.
2. Feed QUALIFIED triggers (with Confidence / RR / Edge, entry, +target/−stop/time exit) into live-lab alerts
   (`live_lab.py`, `alerts.py`, `notify.py`).
3. Optionally schedule the pipeline daily (Task Scheduler) so rankings rotate with new candles.
