# Strategy discovery handover, 2026-11-22 (session 8)

Read this first, then `llm-tuning-handover-2026-11-22.md` (session 7: rules in section 0, long TO DO in section 5).
Branch `feature/m6-prd-v1`. Working rules unchanged: **never run programs/tests/LLM calls yourself** - write code,
`py_compile`, give ONE PowerShell command, owner runs it; every program prints progress on screen; commit only your
files (`universe.txt`, `config/strategies/exits.json` stay uncommitted); short plain answers, readable names.

PowerShell prelude:
```powershell
cd H:\Guru\Learning\StockGLPM\app; $env:PYTHONPATH="src"; $py="E:\Guru\Learning\Alekhya\venv\Scripts\python.exe"
```

## 1. State right now

* Local model: **qwen3.6:35b-a3b** (Ollama; always `think=False`, num_ctx 16384, ~27 GB RAM). Never run two LLM
  chains at once (OOM). Discovery and the tuning chain are run one after another.
* `deploy/windows/discover_round2.cmd` (moves 1d/15m/5m/60m, then discover2-1d/15m) was running - check whether it
  finished (`progress.log` in each report folder).
* **Owner was about to run** the one-stock move table (just built, not yet run):
  `& $py -m stock_gplm.app.move_table --tf 1d --symbol RELIANCE` -> read
  `H:\Guru\Learning\StockGLPM\state-long\reports\tuning\move-table-1d-RELIANCE\PATTERNS.md` with the owner.
* `state-long\reports\tuning\moves-1d` is STALE (bar counts, before the episode fix 478e8e8). Rerun:
  `Rename-Item H:\Guru\Learning\StockGLPM\state-long\reports\tuning\moves-1d moves-1d-old-v2; & $py -m stock_gplm.app.discover --tf 1d --mode moves --workers 16`
* moves-5m was run with calendar features (now disabled) - rename to `moves-5m-old-v1` and rerun with `--tf 5m`.
* Tuning chain API phase paused (1d API ~14 calls, $0.21). Resume later: `cmd /c ...\deploy\windows\tune_phase1.cmd`.
* NSE filings index download was running (resumable).

## 2. Results so far (all judged on unseen CONFIRM months, costs included)

* Tuning existing strategies (local Qwen3.6), 1d/15m/5m/60m: **0 passed**.
* Discovery: **one pass** - `DISC-1d-Ret20BreakoutHighVol` (bucket B): 20-day return > 10.2% and ATR > 4.23% of
  price, swing stop rr 3.5, max 20 days. CONFIRM 538 trades, 34% win, +3.2%/trade, PF 1.39, 16/22 months +.
  In `state-long\reports\tuning\discover-1d\candidates.json`, awaiting owner approval.
* 1d near-misses: ~25 ideas profitable on CONFIRM (PF 1.2-1.57, +0.6-2.9%/trade, ~30% win) rejected only by
  "60% of months profitable" - almost all one family (high-volatility momentum: gap-ups, strong 20-day runs).
* Intraday (15m/5m/60m): nothing profitable on CONFIRM. Tune window only ~7 months (2026); edges 0.1-0.2% vs
  ~0.2% costs. 5m "MidMonth" ideas were calendar date-fitting (tune 65% win -> confirm 45%); calendar features
  `day_of_week`, `session_of_month` now in `disabled` of `config/research/move_features.json`.

## 3. Owner's direction (latest)

1. "Gate too strict" - discussed: on 1d the monthly rule kills trend strategies; on intraday it is real failure.
   Owner then proposed **Bucket C = a stock x strategy x TF whose last 3 trades won** (behaviour changes, signals
   come months apart). Agreed design, **NOT yet built**:
   * per stock (owner chose); last 3 trades all winners AND within 90 days (90 proposed, owner did not object);
   * causal month-by-month replay on CONFIRM: trades the switch lets through vs all signals; variants 3/3, 2/3, 3/5;
   * output report + live "Hot right now" list. Place next to `rolling_check.py`.
2. Owner: "we must find what causes the move - not stuck in the same rut" -> built `move_table.py` (one stock,
   every move with all ~66 feature values, groups, rules). Next: read its PATTERNS.md with the owner, then decide
   whether to run it on more stocks / feed the groups to the LLM / turn group rules into per-stock strategies.
3. Other proposals pending owner choice: Bucket C variant "paper-worthy" (CONFIRM PF >= 1.2, avg > 0, >= 300 trades,
   >= 50% quarters non-negative, max DD limit) - owner paused it in favour of the last-3 idea.

## 4. What was built this session (code map)

* `tune_llm.py`: `repair_dsl` (atr(close,n) -> atr(n)); Ollama `think=False` always; `trail_atr` exit option.
* `tune_eval.py`: `win_low` (Beta 20% cautious win), `month_consistency`, `Variant.label` + `_label_bars` (label
  every bar hit/stop/horizon; rows now carry `bar` index), features from `config/research/move_features.json`
  (`feature_exprs(intraday)`; common / intraday / daily sections, `disabled` list).
* `tune.py`: gate adds `win_lb` 0.6 and `months_pos_frac` 0.6 (config `config/tuning/tuning.json` -> `gate`);
  exit sweep includes `trail_atr`.
* `confidence.py`: pooled/shrunk scores, per-TF thresholds (`config/strategies/confidence.json` `timeframes`:
  5m/60m pool rule, 15m/1d off). `rolling_check.py`: causal monthly selection test with presets.
* `discover.py`: LLM discovery from probe facts (`--mode probe|moves`, `--phase all|facts|report`).
* `discover_moves.py`: move-first discovery - moves per TF in tuning.json `discover.moves`; fingerprints,
  2/3-condition combos, decision tree, per-stock rules counted in EPISODES (`discover.stock_rules`), recurrence vs
  chance; outputs FACTS.md, STOCK_RULES.md, PER_STOCK_RULES.md, DISCOVERY.md, candidates.json. Parallel helpers
  capped by free RAM.
* `move_table.py` (new, unrun): `--tf --symbol [--move H,T,S] [--side] [--top]` -> MOVES.csv, ALL_BARS.csv,
  PATTERNS.md.
* Chains: `deploy/windows/tune_phase1.cmd` (prints to screen), `discover_round2.cmd`, `discover_then_tune.cmd`
  (still redirects to discover.log - fix to print on screen if reused).

## 5. TO DO (priority)

1. Read move_table PATTERNS.md (RELIANCE 1d) with owner; next steps per section 3.2.
2. Build the "last 3 trades won" Bucket C switch (section 3.1); give owner the command.
3. Rerun stale moves-1d and moves-5m (section 1).
4. Approval flow candidates.json -> library (DISC-1d-Ret20BreakoutHighVol first); frozen paper trading.
5. Test PER_STOCK_RULES as strategies (intraday per-stock, not only market-wide ideas).
6. Then session-7 TO DO list (fundamentals track, Nifty history, analyst must-haves).

## 6. Commits this session

2b26c05, 4fcae4c, 9a7af9e, 3ff7ea6, 93bab78, e0ccbba, dedb263, 2d54fd9, 0749a7c, 57b55b1, 501a9aa, 478e8e8
(episode counts), 450c80c (RAM cap, calendar off), cf2ea15 (move_table).
