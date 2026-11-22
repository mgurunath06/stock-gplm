# Strategy revamp handover, 2026-11-22 (session 6)

Read this first. Earlier sessions: `strategy-revamp-handover-2026-11-20.md` (sessions 1-5), then
`upmove-pipeline-handover-2026-11-20.md`. Branch `feature/m6-prd-v1`. Plan for today's product work:
`docs/plans/website-admin-notifications-plan-2026-11-22.md`.

## 0. Working rules (owner; break none of them)

* **Never run tests, jobs or programs on data yourself.** Write the code, give the owner a PowerShell command,
  the owner runs it and pastes results. Old/long-history data needs explicit permission. Syntax checks
  (`py_compile`) and reading result files are fine.
* **PowerShell syntax only** (`$env:PYTHONPATH='src'`, `& $py ...`). Quote `'@file'` arguments (bare `@` is
  splatting in PowerShell). Standard prelude:
  ```powershell
  cd H:\Guru\Learning\StockGLPM\app
  $env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'; $py='E:\Guru\Learning\Alekhya\venv\Scripts\python.exe'
  ```
* Ask before changing anything in the walk-forward design (owner asked to be consulted).
* Recent trades weigh more in every judgement; a strategy that failed earlier but wins now is MORE relevant.
* Judge each stock x TF on its own; keep a strategy on every TF where it wins.
* Trades/judgement from 2010; pre-2010 candles only warm up indicators.
* NSE MCP is unreliable for history; do not add NSE MCP fetch steps to chains. Daily candles we have are enough.
* Long jobs must self-chain (a .cmd), never poll with tokens. Short, direct replies; owner is often on mobile.
* Readable strategy names in reports (Chartink names), never bare ids.

## 1. What ran overnight (21 Nov 23:13 -> 23:47, all rc=0, 2,378 stocks each)

Chain: `state/reports/pipeline/2026-11-22/overnight.cmd` (log `overnight.log`, marker `overnight.done`).

1. Yahoo rebuild: 2,354/2,378 stocks extended, 1 dropped (disagrees with NSE). Report
   `data/derived/yahoo-adjusted/NSE/build-report.json`.
2. `wf-final-2026` (2026-01-01.., unseen months from 2026-04, TFs 1d,1w,5m,15m,30m,60m) ->
   `H:\Guru\Learning\StockGLPM\state\reports\forward\wf-final-2026\` (WALK_FORWARD.md, unseen.csv, active.json,
   exits_learned.json, all_unseen_trades.csv 342 MB, batch.log).
3. `wf-long` (longdaily config, 2010.., unseen years from 2013, TFs 1d,1w,1mo), Nifty 500 first then all ->
   `H:\Guru\Learning\StockGLPM\state-long\reports\forward\wf-long\`.

### Results (honest reading)

| Run | Selected-at-the-time trades | Win | Avg net | PF |
|---|---|---|---|---|
| wf-final-2026 (5m/15m/60m/1d) | 300,440 | 41.2% | -0.17% | 0.70 |
| wf-final-2026, all strategies | 2,515,729 | 44.2% | -0.18% | 0.64 |
| wf-long 1d 2013-2026 | 183,694 | 38.5% | +0.27% | 1.08 |

* 2026 selection is negative every month (PF 0.61-0.77). Selecting on past unseen performance does not yet
  produce an edge intraday. Per TF 2026: 15m PF 0.71, 5m 0.70, 60m 0.72, 1d 0.62 (-0.72%/trade).
* Long 1d: positive years 2014, 2016, 2017, 2020 (+3.83%), 2021, 2023 (+2.26%); negative 2015, 2018, 2019,
  2022, 2024, 2025, 2026. Recent three years negative -> long-run +0.27% is carried by old bull years.
* ACTIVE lists: 2026 = 3,440 combos on 696 stocks; long = 4,163 on 692 stocks. They contain absurd values
  (PF 746, recent avg +48%, PF 354 at 99% win) because the recency weights concentrate on a few trades: 10
  trades can act like 2. **The ACTIVE rule needs an effective-trade-count floor** (Kish n_eff = (sum w)^2 /
  sum w^2 >= ~8) and probably "positive in >= 2 distinct periods". Not changed yet - propose to owner first.
* Data-quality hint: some extreme recent averages (RPOWER +48%, RAYMOND +39% with plain -1.5% - demerger
  2025) may be corporate-action artefacts on daily data. Check `_applied.jsonl` for those stocks before trusting.

### Bug found and fixed this session: 1w and 1mo had ZERO trades in both runs

Cause: `m54/timeframes.weekly_bars/monthly_bars` mark a bar final only with a registered session calendar;
`lanes.all_bars(data)` is called without one in research, so every 1w/1mo bar was `incomplete` ->
`lanes.build_lane` sets `no_signal = incomplete` -> no signal ever fired. (This also means all earlier
forward_test/learning results for 1w/1mo were empty - the "weekly strategies never ran" note of session 5 was
only half fixed.)

Fix (commits c529d25, 0d7a5e9):
* `lanes.research_bars(data, today=None)`: copy of `all_bars` where a week/month is final when its calendar
  period has ended before today, OR (with the calendar file) when the stock has the candle of that period's
  last expected session (so Friday-evening jobs can use the week). Live code keeps strict `all_bars` + the
  registered calendar (LIVE blocker "calendar" is still open - see section 6).
* `walk_forward.run` and `forward_test.run` now use `research_bars`. Other research modules (research.py,
  discover.py, recency.py, upmove.py, stock_dna.py, m56.py, ml_discovery.py, feature_engine.py) still call
  `all_bars(data)` -> their 1w/1mo lanes are still empty. Switch them when they matter.
* 30m having almost nothing is genuine: only ONE library strategy lists 30m.

### New: NSE trading calendar from local candles (`src/stock_gplm/app/trading_calendar.py`)

* Owner pasted research on calendars (ad-hoc closures 2024-01-22, 2024-11-20, 2026-01-15; weekend sessions
  2024-03-02, 2024-05-18, 2025-02-01 Budget, Muhurat Sun 2026-11-08; 2026 has 16 weekday holidays, ~245
  sessions). Decision: build from candles already on disk, no yfinance/network.
* Rule: a date is a session when >= 50% of the sample stocks (first 200 universe lines, Nifty 50 first) that
  were listed at the time (first <= date <= last candle) have a daily candle. Sources: yahoo-adjusted (to 2022)
  + raw Shoonya (from 2022-10-31), via `universe_build._rows`. After the last observed day: weekdays minus the
  hard-coded 2026 holiday list (`HOLIDAYS_2026`).
* Output: `data/reference/nse_trading_calendar.csv` (date, weekday, weekend_session, source observed|scheduled,
  coverage); report `state/reports/calendar/<today>/CALENDAR.md` with checks (known holidays seen as sessions,
  known weekend sessions missing, coverage < 80%), sessions per year, weekend sessions, per-stock gaps.
* `research_bars` reads it via `trading_calendar.load()`; works without it (falls back to "period ended").
* NOT yet run. Not a registered (governance) calendar, so it does not unblock LIVE.

## 2. Owner run queue (give exactly these, in order; nothing run yet)

```powershell
& $py -m stock_gplm.app.trading_calendar
& $py -m stock_gplm.app.walk_forward --stocks '@deploy/oracle/live/universe.txt' --name wf-long-wm --config config\app\gplm.windows.longdaily.json --from 2010-01-01 --first-test 2013-01-01 --step year --timeframes 1w,1mo --workers 20
& $py -m stock_gplm.app.walk_forward --stocks '@deploy/oracle/live/universe.txt' --name wf-final-2026-w --timeframes 1w --workers 20
```
* Calendar first (a minute or two); ask owner to paste the "Checks" section if anything is not "none".
* `wf-long-wm`: weekly + monthly strategies, 2010.., learn on all years before, trade the next year blind,
  2013..2026. Output `state-long\reports\forward\wf-long-wm\`. Under ~30 min expected.
* `wf-final-2026-w`: 1w on 2026 only (defaults --from 2026-01-01 --first-test 2026-04-01 --step month,
  normal config). Low value: ~46 weekly bars, training needs >= 20 bars, so real testing starts ~June. Told
  owner it is optional.
* Walk-forward is resumable by per-stock `done` markers - a new `--name` is needed to recompute; rerunning the
  same name skips finished stocks (that is why the 1w/1mo reruns use new names).

### Pending decision (asked, no answer yet): recency decay per timeframe

`forward_test.recency_weights`: weight = (1 + age/100)^-2, age in BARS of that lane (`DECAY_SESSIONS=100`).
Half weight after ~40 bars: 1d ~40 days, 1w ~40 weeks, 1mo ~40 months (quarter weight ~8 years). Owner
worried old years dominate. I offered: same fade in calendar time (e.g. half weight ~1 year for 1w, ~2 years
for 1mo) - one-line change. **If the owner says go, change it BEFORE they run wf-long-wm** (or rerun with a new
name). Explained to owner: nothing is dropped permanently - every step re-judges every strategy on all history
up to that point; ACTIVE "now" uses all unseen trades with recency weights.

## 3. Walk-forward design (as built, `src/stock_gplm/app/walk_forward.py`)

* Owner choices: expanding windows (monthly for 2026, yearly for 2010+), ACTIVE = unseen profit, exits by max
  recency-weighted expectancy, recent trades weigh more.
* Per stock x TF, for each test period P: learn on [start, P): `exit_grid` over GRID_MAX
  [1%,1.5%,2.5%,4%,swing] x GRID_RR [0.3..3]; `pick_exit` per strategy (>= 8 trades) else TF-best (>= 20);
  selection = `passes(rule_stats(train trades))`: >= 10 trades, weighted avg > 0, PF > 1.2, edge > 0. Then
  trade P blind (signals fired inside P only, exits may run past P). All strategies' blind trades are kept with
  `selected` flag -> "Selected at the time" vs "All strategies" in reports.
* Final pass (period None) learns exits on everything -> per-stock `exits.json` -> `exits_learned.json`.
* `report()` -> WALK_FORWARD.md, unseen.csv (one row per stock x TF x strategy, `active` column),
  active.json, all_unseen_trades.csv.
* Batch: subprocess per stock (`--one`), `--workers`, resumable, `batch.log` lines `SYM rc=0 30s`.
* `learn_cycle.walk_forward()` (weekly/monthly cycle) runs wf-daily + wf-intraday and promotes learned exits
  into `config/strategies/exits.json`; `active.json` for live comes from `learning/walkforward/active.json`
  (in-sample one kept as `active_insample.json`). Check whether overnight wf-final-2026 should be promoted -
  it was NOT promoted automatically (separate name).

## 4. Scheduled jobs (Task Scheduler folder `StockGPLM`)

* LearnDaily Mon-Fri 19:30 (next 23-11-2026 19:30, Ready), LearnWeekly Sat 07:00, LearnMonthly first Sun
  07:00 **disabled** (owner had not permitted old-data runs). `deploy/windows/learn_cycle.cmd`.
* Learning cycle window is clamped to start 2026-01-01; `long_check` starts 2010 (monthly, disabled).
* These now pick up `research_bars` automatically (1w/1mo will start producing trades).

## 5. Data state

* Universe `deploy/oracle/live/universe.txt`: cleaned/ordered by `universe_build --write` on 2026-11-21
  (2,378 kept, 324 excluded, 7 added; backup `universe.before-2026-11-21.txt`). **universe.txt is modified but
  NOT committed**, nor `config/strategies/exits.json` (promoted exits). Ask owner before committing.
  Leftover ETFs still in universe: SBIEQLWT50, SBIMID150 (and likely others with unknown names) - extend
  `NOT_STOCK`/exclusions.
* `data/reference/listing_dates.json`, `symbol_aliases.json` (no renames detected), corporate actions in
  `data/reference/corporate-actions/` (2,378 fetched), applied at load by `corp_actions.apply` (log
  `_applied.jsonl`).
* Daily roots: `daily_extra_roots = [yahoo-adjusted, nse-mcp-recent]` in both `gplm.windows.json` and
  `gplm.windows.longdaily.json`. nse-mcp-adjusted removed (mis-scaled). Yahoo pre-Shoonya, checked vs raw NSE
  day-to-day changes (>2% of days differ >1% -> dropped).
* Most PC daily candles end around the latest cloud sync; owner said rely on existing candles.
* Cloud (Oracle, alias in memory `oracle-cloud-sne`): cloud_sync uses listing dates + universe order; auth
  guard (1 auto login / 6 h, stop after 2 failures) committed 96c60e6 - **owner was given an scp command;
  not confirmed run**. Token expires ~20:08 IST Sunday -> check auth status Monday morning before market.

## 6. Open items / next steps (priority order)

1. Owner runs the queue in section 2; read `wf-long-wm/WALK_FORWARD.md` - does weekly/monthly selection
   beat "all strategies" and stay positive in 2024-2026?
2. Recency-decay-per-TF decision (section 2).
3. Propose a stricter ACTIVE rule (n_eff floor, >= 2 positive periods, cap absurd PF) and re-report from the
   existing unseen trades (report-only change, no rerun needed: `all_unseen_trades.csv` has everything).
4. Best-picks tables the owner asked for: per stock, Nifty 50 / Bank / 100 / 200 / sector x TF, max gain per
   trade, best strategy per stock - from walk-forward unseen data, readable names (`group_report.py` exists).
5. Website revamp + admin + notifications per the plan doc; decisions still needed: push channel
   (Telegram/ntfy/email/browser), admin access, phone hosting, which web strategy candidates (ORB stocks in
   play, momentum/reversal by turnover, turn of month, IBS, RSI(2)) to add. Live lab still runs the old
   library and ATR exits - restart needed.
6. `walk_forward --strategies` filter (for admin "Retest" and quick single-strategy tests) - offered, not built.
7. Switch remaining research modules to `research_bars` where 1w/1mo matter.
8. LIVE blockers unchanged: registered calendar/universe governance, OOS + paper evidence, owner approval.
   The 2026 walk-forward result (PF 0.70) says: no live trading on current selection.

## 7. Commits this session

* c529d25 fix(research): weekly/monthly bars final once their period ended (research_bars)
* 0d7a5e9 feat(calendar): NSE trading calendar from local candles + research bars final at last expected session
* Earlier same branch (session 5): 22f9a83 plan, 77d0ad3 --step, daf8802 walk-forward, 96c60e6 auto re-login,
  f269d70 universe/corp actions, df6961f / 83f9758 / 9746b60 data fixes, 04f9305 monthly SS_Portfolio.
