# Recency research ("find what works now") and Yahoo long history

Owner decisions, 2026-11-20 and 21. Personal, non-commercial research.

## Recency research: `stock_gplm.app.recency`
Stock behaviour changes, so this judges each stock × timeframe on recent candles. It tests the method
("a recent winner keeps winning here") instead of trusting a strategy found on old data.

1. Every applicable library strategy (catalogue, user, AI, ML) is simulated once over all candles, using the
   normal engine, costs and fills. Each strategy is run with up to `--max-combos` parameter sets, and
   identical trade lists are kept only once.
2. Walk forward. At each step date it uses **all** trades that had closed before that date (owner rule: always all
   history; old bull/bear behaviour stays in, with less weight). The last **100 candles** carry extra weight that falls
   linearly, newest first (rolling). Score = 70% recent-100-candle weighted average + 30% average of all older trades,
   both shrunk towards 0. The top 3 (one per strategy, at least `min trades` in all history) are picked. What the picks
   earn in the next step is compared with every eligible variant (the control).

   | TF | warm-up before first pick | step | min trades |
   |---|---|---|---|
   | 5m, 15m (1m) | 30 days | 2 days | 5 |
   | 30m, 60m | 91 days | 5 days | 5 |
   | 1d | 365 days | 7 days | 4 |
   | 1w | 1095 days | 28 days | 3 |

   Overrides: config `recency.windows.<tf>` = `{warmup_days, step_days, min_trades}`; CLI `--recent-bars`, `--recent-share`.
3. **No split date** (owner, 2026-11-20). Rolling track record: each step's picks are judged only on the candles
   after that pick, like live use. Weighted like the ranking: last 12 steps give 70% (newest most, shrunk towards the
   long-run record when they hold few trades), all older steps 30%. WORKS = weighted picks > 0 and beat the control
   (every eligible variant), with >= 4 steps with trades, >= 10 trades, >= 1 trade in the recent steps.
4. Lane status: **ASSIGNED** (works now), **WATCH** (too few trades), **NOT_USED**, **NO_DATA**.

Run it again each day. Lanes whose data changed are recomputed and the ranking rotates. Unchanged lanes are reused.

```powershell
$env:PYTHONPATH="src"
python -m stock_gplm.app.recency --universe deploy/oracle/live/universe.txt --max-symbols 50 --timeframes all `
  --name now --workers 12 --library ..\state-intraday\strategy-library
# 1d/1w with long history (after nse_history / yahoo_history):
python -m stock_gplm.app.recency --config config/app/gplm.windows.longdaily.json --universe deploy/oracle/live/universe.txt `
  --timeframes 1d,1w --name now-long --workers 12 --library ..\state-wide\strategy-library
```
Output is `<home>/reports/recency/<name>/`: `summary.md`, `lanes.csv`, `current.csv` (today's ranking),
`assigned.csv`, `steps.csv` (every step's picks), and `lanes/<SYM>/<tf>.json`.

Owner check: `python tests/owner/check_recency.py [--stocks ...] [--library ...]`. It covers the unit tests, a full run,
the rules for picks, the split boundary, reuse on rerun, and no look-ahead (a run on candles cut at `--cut`
must make the same picks before the cut).

## Yahoo long daily history: `stock_gplm.app.yahoo_history`
Unofficial, free and split-adjusted. It goes back to 2000 and fills only the dates before the NSE MCP history
(2021-11-22) and Shoonya (2022-10-31). Raw replies are saved untouched in `data/raw/yahoo/<SYM>/<date>.json`.
Derived Shoonya-format files go to `data/derived/yahoo-adjusted/NSE/<SYM>-EQ/`.
Rows with missing or impossible prices, or zero volume, are dropped and counted in `build-report.json`.
The join uses the median close ratio of the first 20 days that overlap Shoonya, and those days must agree within 1%.
**Order: NSE first, then Yahoo.** `yahoo_history build` skips any stock without NSE MCP history. `nse_history build` rebuilds Yahoo's files afterwards, so the two sources never overlap. The `config` step writes
`gplm.windows.longdaily.json` with both extra roots.

```powershell
python -m stock_gplm.app.nse_history all --stocks @deploy/oracle/live/universe.txt
python -m stock_gplm.app.yahoo_history all --stocks @deploy/oracle/live/universe.txt
```
Owner check: `python tests/owner/check_yahoo_history.py [--stocks ...]`.
