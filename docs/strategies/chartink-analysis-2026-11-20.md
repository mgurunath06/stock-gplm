# Chartink scans → strategy library (2026-11-20, approved by owner)

Active file: `config/strategies/library/owner/chartink.json` (65 strategies). Names = the owner's Chartink scan
names (scan dashboard video `tmp/ChartInk1.mp4`, page 1 of 2 = 50 of 88 scans), matched to the 59 scan screenshots in
`config/strategies/ChartInk/` (7 other screenshots were not scans).

## Rules applied to every Chartink strategy
- **Liquidity floor** (replaces market cap): 20-session median turnover ≥ ₹15 Cr and close ≥ ₹50 from completed sessions
  (weekly: 4-week median ≥ ₹75 Cr; intraday: mean of the previous 20 sessions). Same gate in the up-move learner.
- **Exits by type:** `trend` = 2 ATR stop trailing at 2 ATR, no target, 25 bars; `meanrev` = 1.5 ATR stop, 2 ATR target, 8 bars;
  `intraday` = 1.2 ATR stop, 2.5 ATR target, 12 bars.
- **Intraday session (engine v3, up-move v2):** no new entries after 14:45, square-off at 15:15 IST.
- **Daily/weekly shorts** are tagged `cash-short:research-only`: no overnight cash short in India, so read them as
  exit-long / avoid signals until F&O is in scope. Intraday shorts are tradable.
- "Futures segment" read as cash; disabled conditions ignored; Nifty 50/100/200/500 kept as `segment:` tags.
- 3-minute scans run on 5m (or 15m for the confirmation bar).

## Directions settled by the scan names
GM Bearish RSI Divergence - Hourly = short; BB 15 min Short strategy = short; PoS 5 EMA 15 mins = 5-EMA alert-candle short;
Volume Based Bulltrade = long; GM W Pattern = Bollinger W-bottom (interpretation, confirm). Still two-sided (both tested):
5 Red / 5 Green candles - 5 min (each split into opening drive 09:20–09:45 and midday 10:00–14:30) and D-VolumeSpike.

## Moved to the up-move learner as building blocks (state scans, not entries)
Near EMA200 (±1%), Bollinger band Squeeze, Bullish Hourly Convergence, Sowmya scan, GM Bull EMA Sorted, Monthly MACD Below 0.
GM Bear EMA Sorted is not used (the learner is long-only).

## Not converted
- Daily Imp - 9c200 (EMA13 × EMA200) = existing O1-004.
- SS_Portfolio_Entry_Monthly: needs a 200-month EMA (~17 years of history).
- Scans on the dashboard without a screenshot (e.g. Claude - Exit - 50% / Full Exit, Bear 5 EMA - 5min..Monthly, GR - Intraday Sell,
  PDH break out, 15-VolumeSpike, Daily Imp - 10 20 30 EMA ADX 20+, GM Bearish 15/3 min Pinbar) and page 2 (38 scans).

## Bugs found in the saved Chartink scans
- GM Bearish Daily Pinbar Candle cannot fire (branch A compares a close with itself; branch B needs close > open and open > close).
- Near EMA200 (Nifty 50): "any of ≤ 1.02× / ≥ 0.98×" matches every stock.
- GM W Pattern: "lower band < SMA 20" is always true.
- GM Bull/Bear EMA Sorted compare 1h EMA100 with 15m EMA200.

## Strategy list
| ID | Chartink name | TF | Side | Exit | Screenshot |
|---|---|---|---|---|---|
| O2-001 | GMAnalysis - EMA 9 x 45 | 1d | long | trend | 200442 |
| O2-002 | GMAnalysis - EMA 9 x 200 | 1d | long | trend | 200442 |
| O2-003 | GMAnalysis - MACD up below zero + RSI3 | 1d | long | trend | 200442 |
| O2-004 | MACD Cross below 0 | 1d | long | trend | 200648 |
| O2-004-W | Weekly MACD below 0 | 1w | long | trend | 202041 |
| O2-006 | Excellent - Entry A - MACD Reversal | 1d | long | trend | 202152 |
| O2-007 | Claude Predictive MACD | 1d | long | trend | 202206 |
| O2-008 | 5 day daily 5% up | 1d | long | trend | 200604 |
| O2-009 | 5 day price increase | 1d | long | trend | 201544 |
| O2-010 | SS-2Bullish-Daily | 1d | long | trend | 201125 |
| O2-011 | SS-2Bullish-Hourly | 1d | long | trend | 201447 |
| O2-012 | SS-2Bearish-Daily | 1d | short | trend | 201603 |
| O2-018-L | D-VolumeSpike - Long | 1d | long | meanrev | 201926 |
| O2-018-S | D-VolumeSpike - Short | 1d | short | meanrev | 201926 |
| O2-019 | Volume Based Bulltrade | 1d | long | trend | 201908 |
| O2-020 | 9 gt 20 at 45 deg | 1d | long | trend | 202112 |
| O2-020-ATR | 9 gt 20 at 45 deg (ATR slope) | 1d | long | trend | 202112 |
| O2-023 | JULY23_DAILY_BULL_BBSCAN | 1d | long | meanrev | 201836 |
| O2-026 | Bearish EMA Crossover | 1d | short | trend | 201616 |
| O2-032 | SS_Portfolio_Entry | 1d | long | meanrev | 201400 |
| O2-032-W | SS_Portfolio_Entry_Weekly | 1w | long | meanrev | 202020 |
| O2-033 | DS - Bollinger Band Bullish Breakout - Daily | 1d | long | trend | 200900 |
| O2-034 | DS - Bollinger Band Bullish Breakout - Daily (follow-through) | 1d | long | trend | 200900 |
| O2-036 | GM_Longterm_InvestmentStrategy | 1d | long | meanrev | 201628 |
| O2-038 | GM Bullish Daily Pinbar Bull Candle | 1d | long | meanrev | 201500 |
| O2-039 | GM Bullish Daily Pinbar Candle Today | 1d | long | meanrev | 201534 |
| O2-040 | GM Bullish Daily Pinbar Candle - Final | 1d | long | meanrev | 201520 |
| O2-041 | GM Bearish Daily Pinbar Candle | 1d | short | meanrev | 201750 |
| O2-047 | 44EMA Low | 1d | long | meanrev | 201846 |
| O2-048 | 44EMA High | 1d | long | meanrev | 201859 |
| O2-049 | Entry Type B — Trend Continuation Pullback | 1d | long | meanrev | 202141 |
| O2-050 | Near EMA200 - cross above | 1d | long | trend | 200943 |
| O2-051 | Near EMA200 - cross below | 1d | short | trend | 200943 |
| O2-058 | GM W Pattern | 1d | long | meanrev | 201411 |
| O2-013 | GR - Intraday buy | 15m | long | intraday | 200722 |
| O2-014 | 5 Min Golden Cross | 5m | long | intraday | 200748 |
| O2-015 | Sowmya 3 min Strategy | 5m | long | intraday | 202059 |
| O2-016 | 15 min Volume Shockers - Up | 15m | long | intraday | 201312 |
| O2-016-ATR | 15 min Volume Shockers - Up (ATR) | 15m | long | intraday | 201312 |
| O2-017 | 15 min Volume Shockers - Down | 15m | short | intraday | 201312 |
| O2-017-ATR | 15 min Volume Shockers - Down (ATR) | 15m | short | intraday | 201312 |
| O2-021 | New BB 15 Min | 15m, 60m | long | intraday | 201041 |
| O2-022 | July23_15MIN_BuLL_BBSCAN | 15m | long | intraday | 201827 |
| O2-024 | HA-13-50-200 | 15m, 60m | long | intraday | 201947 |
| O2-025 | BB 15 min Short strategy | 15m | short | intraday | 200920 |
| O2-026-I | Bearish EMA Crossover (intraday) | 15m, 60m | short | intraday | 201616 |
| O2-027 | BB 100-200-400 30 min - GM | 30m | long | intraday | 201109 |
| O2-028 | SS-Swing-1hr-Bullish | 60m | long | intraday | 201324 |
| O2-029 | SS-Swing-1hr-Bearish | 60m | short | intraday | 201334 |
| O2-030 | SS - 3 MIN 2BULL-CANDLE | 5m | long | intraday | 201250 |
| O2-031 | Final-SS-GM-3Min-Bearish | 5m | short | intraday | 201301 |
| O2-035 | Bollinger Feb25 | 60m | long | intraday | 202131 |
| O2-037 | GM Bearish RSI Divergence - Hourly | 60m | short | intraday | 200813 |
| O2-042-L-OPEN | 5 Red candles - 5 min - Long (opening drive) | 5m | long | intraday | 201703 |
| O2-042-L-MID | 5 Red candles - 5 min - Long (midday) | 5m | long | intraday | 201703 |
| O2-042-S-OPEN | 5 Red candles - 5 min - Short (opening drive) | 5m | short | intraday | 201703 |
| O2-042-S-MID | 5 Red candles - 5 min - Short (midday) | 5m | short | intraday | 201703 |
| O2-043-L-OPEN | 5 Green candles - 5 min - Long (opening drive) | 5m | long | intraday | 201713 |
| O2-043-L-MID | 5 Green candles - 5 min - Long (midday) | 5m | long | intraday | 201713 |
| O2-043-S-OPEN | 5 Green candles - 5 min - Short (opening drive) | 5m | short | intraday | 201713 |
| O2-043-S-MID | 5 Green candles - 5 min - Short (midday) | 5m | short | intraday | 201713 |
| O2-044 | PoS 5 EMA 15 mins | 15m | short | intraday | 201723 |
| O2-045 | Supertrend Bear 15 min | 15m | short | intraday | 201936 |
| O2-046 | MySuperTrend | 15m | long | intraday | 202033 |
| O2-059 | Super Swing Setup -Bullish | 60m | long | intraday | 201344 |

## Checks
All strategies parse and run on real NSE candles (RELIANCE, HDFCBANK, ICICIBANK; 1d, 1w, 60m, 30m, 15m, 5m and
cross-timeframe). Rare by design (0 firings on 3 stocks): 5 day daily 5% up, 15 min Volume Shockers - Up (5%),
SS_Portfolio_Entry_Weekly. Three stocks say nothing about edge; the owner-run pipeline gives the evidence.
