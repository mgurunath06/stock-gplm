# Handover 2026-11-24 (session 11): owner EMA13 x EMA20 - trade-plan research (owner_plans)

Read first: `owner-strategy-handover-2026-11-22.md` (rules, owner answers, working rules). Branch `feature/m6-prd-v1`.
Owner task (2026-11-24): learn WHEN the EMA13xEMA20 cross is worth trading and WHICH entry / stop / target / trail plan
fits the price action at that moment. RELIANCE 15m longs first, then the universe with per-stock results.

## Working rules (unchanged)
* Never run heavy programs yourself: write code, `py_compile`, tiny deterministic tests only, give ONE PowerShell
  command; owner runs it. Owner files (`config/research/owner_trades*.csv`) and raw data are read-only.
* Recent trades weigh more: `(1 + age_sessions/100)^-2`. No date holdouts; honesty = chronological walk-forward,
  training trades must have CLOSED before the decision being scored.
* Times = TradingView candle START (IST). Do not claim profitability unless the walk-forward report supports it.

## Design (what owner_plans.py does)
1. **Audit** vs owner marks (`owner_trades.csv` + `owner_trades-RELIANCE-15m.csv`, 66 marks): IST start time, OHLC,
   EMA13/20/200, our cross (exact / one-bar / none), fill, stop candle under BOTH stop definitions, exit. Data checks:
   candle alignment, bars per session, EMA warm-up bars, EMA13-EMA20 gap at the owner's candle (near-cross = feed /
   warm-up difference, not the strategy).
2. **Plans per cross** (config `config/research/owner_plans.json`): entry {NEXT_OPEN, RETEST13} x stop {SWING = last
   confirmed swing low (k bars each side), PBLOW = lowest low of the pullback run} x management {T2R, T200, PT2R,
   PT200, TRAIL_TOUCH, TRAIL_CLOSE} + SKIP. Eligibility: T200/PT200 need EMA200 above entry with room >= min_room_r.
   Costs from the lane + slippage bps; gaps fill at the open; ambiguous same-candle stop/target resolved on 5m candles,
   else conservative (stop first) and flagged.
3. **Labels**: one row per cross x eligible plan: net R, MFE/MAE (R), bars held, exit reason, flags. V2 (engine)
   kept as a named baseline.
4. **Learning**: readable RULE selector (cell = near/far x above/below EMA200, best walk-forward mean) and a small
   MODEL (per-plan regressor of net R), both walk-forward, recency weighted, "insufficient evidence" when thin.
   Compared with baselines: always next open, always retest, fixed 2R, EMA20 trail, V2.
5. **Report** per cross: take/wait/skip, why, planned entry, stop + candle, target, trail, expected R, actual.

## Status (update as you go)
* [x] `src/stock_gplm/app/owner_plans.py` + `config/research/owner_plans.json` written, py_compile OK (NOT run yet).
* [x] `tests/test_owner_plans.py` (8 tiny tests: retest ordering, swing/pullback stop, EMA200 live target, EMA20 touch
  never loosens, close-below-EMA20 next open, same-candle stop/target + 5m resolution) - owner runs them.
* [x] `owner_strategy.fills` retest fix: cancel checked on the COMPLETED candle j-1 at candle j's open (was candle j's
  own closing EMAs -> cancelled limits that had already filled). Changes V11/V13/V15/V16 results slightly.
* [ ] owner run + "check" -> read `<reports>/tuning/owner-plans-15m/REPORT.md` and `stocks/RELIANCE/audit.md`.

## Commands (owner)
```powershell
cd H:\Guru\Learning\StockGLPM\app; $env:PYTHONPATH="src"; $py="E:\Guru\Learning\Alekhya\venv\Scripts\python.exe"
& $py -m pytest -q tests/test_owner_plans.py; & $py -m stock_gplm.app.owner_plans --symbol RELIANCE
# universe (after RELIANCE checks out): & $py -m stock_gplm.app.owner_plans --symbol RELIANCE --stocks 200 --workers 20
```
Checkpoint: `owner-plans-15m/stocks/<SYM>/data.json` - a stock with that file is not recomputed (`--fresh` redoes).
Note: the walk-forward is pooled over every stock in the run (RELIANCE-only run = RELIANCE-only model).

## Code map (owner_plans.py)
* Pure array layer (`Bars`, `crosses`, `swing_stop`, `pullback_low`, `retest_fill`, `order_in_bar`,
  `simulate_plan`, `plan_rows`, `features`) - unit-testable, no data loading.
* `load_stock` (OwnerStrategy.load + 5m via tune_eval._load), `audit` (section 1 + labels), `v2_result` (V2 baseline
  via the engine), `extract` (one stock -> data.json / plans.csv / audit.md; worker).
* `Learner` (walk-forward: refit every N crosses on rows CLOSED before the refit time; HGB per plan shrunk to the
  plan's recent mean, n/(n+100); RULE = best mean in the near/far x above/below-EMA200 cell).
* `Research.report` -> REPORT.md sections 2 selectors vs baselines, 3 plan x cell, 4 near/far definitions, 5 owner
  take/skip vs model, 6 per stock, 7 every cross; decisions.csv.

## Known limits / next
* Longs only (shorts: mirror + 15:15 square-off, not built). Each cross scored independently (overlaps possible).
* Trail-after-EMA200 as a separate decision (owner item) not built yet - next step after the first report.
* If the 5m load fails the run still works; ambiguous candles are then stop-first and flagged.
