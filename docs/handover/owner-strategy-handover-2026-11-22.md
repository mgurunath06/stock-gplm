# Handover 2026-11-22 (session 9): owner's EMA13 x EMA20 strategy -> learn when it succeeds / fails

Read this first. Previous: `discovery-handover-2026-11-22.md` (session 8), `llm-tuning-handover-2026-11-22.md` (session 7).
Branch `feature/m6-prd-v1`. Last commit of this session: see section 7.

## 0. Working rules (owner, all still in force)

* **Never run programs/tests/LLM calls yourself.** Write code, `py_compile`, give ONE PowerShell command, owner runs it
  and says "check". Reading files/reports is fine. (memory: no-runs-without-go)
* **Do not change code beyond what the owner agreed** - propose first, then build. (He objected this session.)
* **Most important rule: recent trades weigh more** in every judgement: weight `(1 + age_sessions/100)^-2`.
  **No date splits / holdouts** - honesty checks are CAUSAL REPLAYS (stand at each moment, closed trades only).
* **Labels:** HC (high confidence: >= 12 trades, recent-weighted win >= 60%), **HCLT** (high confidence, low trades:
  3-11 trades, 0 losses below 5 trades / max 1 loss) - never discard few-trade winners; **HCLT fires in ANY trend**
  (no trend/phase gate, no pause). MC medium, LC avoid. Config `config/research/trigger_set.json -> labels`.
* Per stock x TF, found by the program itself (don't ask the owner to pick). Look facts up, don't ask.
* Every program prints progress on screen; checkpoint/resumable; commit only own files (never `universe.txt`,
  `config/strategies/exits.json`, the other pre-existing uncommitted files).
* Short plain answers; readable strategy names; times shown as **TradingView candle START times (IST)**.

PowerShell prelude:
```powershell
cd H:\Guru\Learning\StockGLPM\app; $env:PYTHONPATH="src"; $py="E:\Guru\Learning\Alekhya\venv\Scripts\python.exe"
```
PC: i7-14700K 28 threads, 64 GB, RTX 5070 12 GB; venv has **torch 2.11 + CUDA 12.8**, torchvision, numba, sklearn
(no lightgbm). Data: minute (5m/15m/60m) **from 2026-01-01 only**, ~2690 stocks; daily from 2000 (judge from 2010).
Intraday reports: `H:\Guru\Learning\StockGLPM\state\reports\tuning\`; daily: `...\state-long\reports\tuning\`.

## 1. The owner's strategy (from config/research/owner_trades.csv - owner-filled, RELIANCE 15m; git-ignored, on disk only - never delete)

| Part | Rule |
|---|---|
| Main entry (long) | EMA13 crosses above EMA20 (15m), buy next candle open |
| Milestone entry | EMA13 crosses above EMA200 (14 Oct 14:15) |
| Stop | below the LAST SWING LOW of the pullback before the signal - can be days back (5 Oct -> 1 Oct 14:00; 26 Oct -> 23 Oct 12:00; 13 Oct -> 13 Oct 11:45; 21 Sep -> 21 Sep 10:45) |
| Exit if price BELOW EMA 200 at entry | target = EMA 200 |
| Exit if price ABOVE EMA 200 at entry | hold until price breaches EMA 20 (29 Oct -> 3 Nov 14:30; 9 Nov -> 11 Nov 13:45; 17 Nov -> 18 Nov 09:30; 14 Oct -> 15 Oct 14:45) |
| Short | mirror: EMA13 crosses below EMA20 (7 Sep 09:30 short, stop above swing high 4 Sep 14:30, exit 7 Sep 12:15 at EMA 200) |
| Skip | 16 / 17 / 25 Sep: "no setup" (16 Sep: "didn't even breach previous day high (PDH)") |

CSV reading rules: dates are DD-MM-YYYY (Excel); times < 09:15 are PM (02:30 = 14:30, 01:00 = 13:00, 02:45 =
14:45); rows 16-17 lack symbol/side -> RELIANCE 15m long, row 17 decision -> take; 21 Sep appears at 12:30 and 13:15
(13:15 = cross candle). Our data shows EMA13 x EMA20 crossing ONE candle later than the owner's chart sometimes
(EMA history starts 2026-01; values differ by ~0.1 from TradingView).

**Open questions to ask first next chat:**
1. Is breaking the previous day's high (PDH) part of the long setup, or one reason for 16 Sep only?
2. Train the success/failure model on this strategy across the 200 stocks of `universe.txt` (pooled; RELIANCE alone
   has only ~20 signals) - owner said "train on just this one", meaning this STRATEGY; confirm pooling.
3. Shorts: same-day only (cash), or overnight allowed (7 Sep stop came from 4 Sep)?
4. Exactly which swing low (lowest low of the pullback? pivot strength?).

**Owner answers (session 10, 2026-11-22):**
1. PDH: NOT a rule - only the reason for skipping 16 Sep (a feature for the learning step).
2. Works on ALL stocks, but every stock gets its own signals and verdict irrespective of other stocks.
3. Cash shorts same-day (15:15 square-off); options can be multi-day (deferred).
4. Stop = lowest low of the pullback (encoded: last run of EMA13 < EMA20 up to the signal candle).

**Session 10 build:** engine `exit.pullback_stop {fast, slow, buffer, lookback}` and `exit.ema_regime {anchor,
trail, target live|fixed, breach_fill close|next_open}`; `app/owner_strategy.py` + `config/research/owner_strategy.json`
(check vs owner_trades.csv for 4 exit readings, signals not in the file, every trade, label, today's signal).
`& $py -m stock_gplm.app.owner_strategy --symbol RELIANCE`

## 2. NEXT TASK (owner's direction, agreed in principle)

"Train on just this one (keep the others) - learn when it succeeds and when it fails."
1. **Encode the owner strategy exactly** (rules above) - new exit option needed in the engine: *conditional exit*
   (below EMA200 at entry -> target EMA200; above -> exit when close breaches EMA20). Engine already has
   `exit.when` (DSL signal exit), `target_ema`, swing stop with `strength`, `min_rr`, `overnight`.
   Verify trade-by-trade against `owner_trades.csv` (signal, stop candle, exit candle) and list mismatches.
2. **Every signal** of the strategy (long + short) on the chosen stocks, 15m 2026, outcome = success/failure (+R).
3. **Learn success vs failure:** (a) chart-reader model on GPU (torch): last 64 candles OHLC + EMA 9/13/20/50/200 +
   volume, normalised -> P(success); walk-forward by month, recent weighted; (b) readable view: which indicator
   conditions separate winners from losers (short trader list + PDH etc.). Owner's take/skip rows = extra labels and
   the check ("does it decide like me?").
4. Output: per signal take/skip with probability, HC/HCLT per stock, causal replay trade list with dates/times,
   today's signals. Then later: variations of the template ("more of such strategies") and the full combination
   grid (owner asked "apply every combination" - ~53k per stock x TF; plan in section 5).

## 3. What was built this session (all committed; code map)

* `app/phase_patterns.py` + `config/research/phase_patterns.json`: phase (EMA 9/13/20/50 stack), moves (intraday
  +2% before -1%, held overnight, holds 1/3/5 days; 1d +1 ATR / -0.5 ATR), 207-234 values per bar (indicators + their
  3/10-candle changes), patterns per phase with streak back from today, recency-weighted scores, `set_cut()` /
  `analyse()` / `wt()` for causal use, "Moves found" table (matched the owner's chart), -10..+10 profile.
* `app/stock_tf.py`: phase patterns -> DSL strategies -> engine -> recency gate -> causal replay. Result RELIANCE 15m:
  replay 3 trades all lost (search over ~1M combos overfits) - superseded by move_triggers.
* `app/pattern_check.py` + `config/research/wakeup_15m.json`: spec variants on focus stock + 200 stocks.
* `app/trigger_check.py` + `config/research/triggers.json`: trigger + structure exits, `--around` candle dump.
* **`app/move_triggers.py` + `config/research/trigger_set.json`** (current main tool): 75 generated event triggers
  (all EMA pairs, price x EMA, MACD, RSI, stoch, breakouts, volume, VWAP, supertrend/PSAR, candles) + library; 10
  structure exits; best filter from a SHORT trader list (12 conditions incl. NotBelowFallingHourlyEMA200); labels;
  same-move-once grouping; trend guard OFF (blocked owner's reversal setups); catch table; `--check` candles; causal
  replay; today's signals; candidates.json.
  Command: `& $py -m stock_gplm.app.move_triggers --symbol RELIANCE --tf 15m --check "2026-10-13 14:00"`
* Engine `strategy_engine.py` Exit: `stop_pct`, `target_pct`, `overnight` (no 15:15 square-off), `swing_stop.strength`
  (2 minor / 5 major, `swing_level_k`), `target_ema` (EMA at signal candle, no trade if price beyond), `min_rr`.
  All off by default; library validation updated (`strategy_library.py`).
* Library: `config/strategies/library/owner/stock-specific.json` - RELIANCE-15m-ChopDropVolSurgeLowMfi-3d
  (owner-approved, PAPER only).
* `stock_strategy.py` (year-by-year builder) - owner rejected the approach; kept, unused.

## 4. Results (RELIANCE 15m, 2026)

* move_triggers latest: HC 4, HCLT 8, MC 153, LC 730. Causal replay last 3 months: 12 trades, 5 won, -0.4%:
  Aug 21-Sep 30 seven losses (falling market), Oct 5-Nov 17 five wins (+7.1%). Losing-streak pause hand-check
  gained only ~0.8% and missed 5 Oct -> not built.
* **The owner's own strategy was NOT reproduced**: replay missed 31 Aug 12:45, 13 Oct 14:15, 26 Oct 09:15,
  17 Nov 10:00, milestone 14/15 Oct; exited 29 Oct at +0.66% instead of holding to 3 Nov (no EMA20 trail); no shorts.
  Cause: the program selects among ~750 machine strategies by label; the owner's setup on its own fixed-exit stats
  scored MC/LC (~38% win) and was never picked; his edge is in exits + skipping. Owner: "I wanted ML to find MORE of
  such strategies; instead you are not even finding what I know."
* Owner wants ML that "sees" charts and learns entries AND exits like a human, then creates strategies. Agreed plan:
  chart-reader model (numbers of the chart, not screenshots), pooled across stocks, walk-forward, explanations, then
  distil into readable rules. No frontier APIs needed (local Qwen / existing providers for idea/rule text only).

## 5. Later plan (owner asked; not built)

Full combination grid per stock x TF: trigger (~51) x direction (2) x filter (13) x stop (4: minor/major swing, 1%,
1.5 ATR) x exit (~10: EMA 50/100/200 target, 2R, 3R, trail close-below EMA 9/13/20/50, EMA200-then-EMA20) ~ 53k.
Simulate trigger x dir x stop x exit once (numba), filters = subsets; ~20 processes; luck control: pooled across
stocks + random-entry baseline + per-stock labels + causal replay.

## 6. Earlier still pending (session 8 list)

DISC-1d-Ret20BreakoutHighVol approval/paper; stale moves-1d / moves-5m reruns; Bucket C last-3 switch; tuning chain
API phase paused; NSE filings download (resumable); session-7 TO DO (fundamentals, Nifty history).

## 7. Commits this session

9b0bed5 stock_strategy; 6f70659 c0e1ffd b354448 fedc3df d3e220a 755a447 phase_patterns (+ 4d928e1 closed_at fix,
152a700 recency + replay); d0ebfeb pattern_check + engine pct/overnight; 6a3558f stock_tf + RELIANCE library entry;
51b06a9 trigger_check; 270f02b move_triggers + engine structure exits; c49fefc short filters / same-move / guard;
634f1d9 guard off; 58fd504 + f595499 HCLT = any trend (filters allowed); this handover + owner_trades.csv.
