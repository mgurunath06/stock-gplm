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
* [x] First RELIANCE run (2026-11-24 15:13, old folder `owner-plans-15m`; the owner then started --stocks 200 into the
  same folder). Fixes after it (commit after 662ae3a): `_doc` baseline row removed; audit explains each "none" and
  tests swing k=1/2/3 vs the owner's stop candle; section 2b costs in R by stop width; output folders now
  `owner-plans-15m-RELIANCE` (one stock) and `owner-plans-15m-pool<N>` (pooled).

### RELIANCE 15m result (first run, 139 crosses Feb-Nov 2026) - NOT profitable
* Every baseline loses: next open -43.9R (22% win), retest -63.9R, fixed 2R -55.7R, EMA20 touch -41.1R, V2 -50.6R.
  MODEL -7.9R on 25 trades (t -1.44, negative) - it "beats" baselines only by skipping. RULE: insufficient evidence.
  Hindsight best plan avg +0.81R/cross (diagnostic only).
* Costs ~0.24R per trade: gross R of next-open/EMA20-close is -0.08, net -0.32. Stops < 0.6% lose most; stops > 1%
  are +0.17 gross / +0.06 net. Stop width in R is the key driver - consider min-stop-% eligibility next.
* Near/far: FAR crosses did better than near on every definition (0.5 ATR: near -0.62R, far -0.24R); retest worse
  than next open in every group -> owner's "wait for EMA13 when far" hypothesis not supported on RELIANCE 15m.
* Audit: 48 long marks: exact 18, one-bar 11, none 19 (2 before warm-up, most = owner entries without a fresh
  cross, i.e. re-entries while EMA13 already above EMA20). Stop candle: SWING(k=3) 12/45, PBLOW 10/45 - the owner
  usually takes a nearer, more recent swing low (k=1/2 counts in the next audit run).
* Owner "skip" marks (16/17/25 Sep) are not at crosses in our data (EMA13 still below EMA20) -> 0 skip labels.

### Next ideas (propose to owner first)
1. Eligibility: minimum stop width (e.g. >= 0.8%) or R >= k x round-trip cost. 2. Re-entry trigger (EMA13 above
EMA20, pullback to EMA13/20 then up-close) to cover the owner's non-cross entries. 3. Swing k from the audit.
4. Pooled universe result (owner-plans-15m-pool200) before any conclusion.

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
