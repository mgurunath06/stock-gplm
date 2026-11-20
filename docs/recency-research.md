# Recency research ("find what works now") and Yahoo long history

Owner decisions, 2026-11-20 and 21. Personal, non-commercial research.

## Recency research: `stock_gplm.app.recency`
Stock behaviour changes, so this judges each stock × timeframe on recent candles. It tests the method
("a recent winner keeps winning here") instead of trusting a strategy found on old data.

1. Every applicable library strategy (catalogue, user, AI, ML) is simulated once over all candles, using the
   normal engine, costs and fills. Each strategy is run with up to `--max-combos` parameter sets, and
   identical trade lists are kept only once.
2. Walk forward. At each step date it uses only trades that closed before that date, inside the lookback window.
   Variants are ranked by a recency-weighted, shrunk average net return (recent trades weigh more: weight halves every
   quarter of the lookback, so the oldest trade counts 1/16 of the newest; `--half-life` changes the fraction), and the top 3 (one per strategy) are
   picked. What the picks earn in the next step is compared with every active variant (the control).

   | TF | lookback | step | min recent trades |
   |---|---|---|---|
   | 5m, 15m (1m) | 30 days | 7 days | 5 |
   | 30m, 60m | 91 days | 14 days | 5 |
   | 1d | 365 days | 30 days | 4 |
   | 1w | 1095 days | 91 days | 3 |

   You can override these in the config under `recency.windows.<tf>` = `{lookback_days, step_days, min_trades}`.
3. Verdict per period: **dev** covers steps whose next window ends by `m56.development_end` (2026-08-13), and **test**
   covers steps decided after it. HOLDS means the picks averaged above 0 and beat the control, over at least 4 steps
   with trades and at least 10 trades.
4. Lane status: **ASSIGNED** (holds in dev and in test), **WATCH** (holds in dev, too few test trades),
   **DROPPED_IN_TEST**, **NOT_USED**, **NO_DATA**.

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
