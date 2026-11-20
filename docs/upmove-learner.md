# Up-move learner (`stock_gplm.app.upmove`)

Owner design, 2026-11-20. Learns, per stock, which triggers came before past up-moves, and scores each with:

- **Confidence**: recency-weighted chance the trade ends in profit (shrunk to the stock's base rate).
- **RR**: average win / average loss.
- **Edge** (ranking number, in R) = Confidence x RR - (1 - Confidence): expected gain per 1 unit of risk.

Up-move: +3% within 5 candles before -1.5% (`--target`, `--stop`, `--horizon`), costs included, stop counts first
when both hit in one candle. All history, weight (1 + age/100)^-2 (`--decay-bars`, `--decay-power`).
Triggers: 27 basic conditions (EMA 13 vs 20/50/100/200, close x EMA 13, MACD, RSI, volume, OBV, breakouts, candles,
EMA 13 stretch/snap-back, ATR contraction) plus AND-pairs across families; duplicates (same firing days) dropped.
Live-like check (no split date): each firing is rescored from earlier, closed firings only; firings with Edge > 0 then
are "taken". QUALIFIED = Edge > 0, lift > 1, >= 5 taken with a positive recency-weighted result.

```powershell
$env:PYTHONPATH="src"
python -m stock_gplm.app.upmove --config config/app/gplm.windows.longdaily.json --stocks @deploy/oracle/live/universe.txt --tf 1d --name all-1d
```
Output `<home>/reports/upmove/<name>/`: `summary.md`, `triggers.csv`, `today.csv` (firing on the latest candle),
`stocks/<SYM>-<tf>.json` (reused while data is unchanged). Any timeframe works (`--tf 15m` etc.).

Library: `config/strategies/library/owner/owner-basics.json` adds O1-001..009 (EMA 13 x 20/50/100/200, MACD x signal,
RSI x 50, volume surge, EMA 13 snap-back long/short with EMA 13 as the moving target) for the recency ranking.
