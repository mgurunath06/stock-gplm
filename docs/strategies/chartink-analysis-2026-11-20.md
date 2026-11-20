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
- **Shorts are first-class (owner 2026-11-20):** intraday shorts → `execution:intraday-short-or-PE`; daily/weekly shorts →
  `execution:PE` (no overnight cash short). Longs → `execution:cash-long` / `execution:intraday-long-or-CE`.
- **Down-move learner:** `upmove --side short` (same move sizes, mirrored: 1d = −3% within 5 days before +1.5%), bearish building
  blocks incl. GM Bear EMA Sorted and the 5 EMA alert candle; pipeline steps downmove_1d / 60m / 15m.
- **Claude exits:** Excellent - Entry A, Claude Predictive MACD and Entry Type B exit on 'Claude - Exit - Full Exit'
  (close < EMA 21 and EMA 9 < EMA 21); the 50% partial exit is not modelled (engine exits the whole position).
- "Futures segment" read as cash; disabled conditions ignored; Nifty 50/100/200/500 kept as `segment:` tags.
- 3-minute scans run on 5m (or 15m for the confirmation bar).

## Directions settled by the scan names
GM Bearish RSI Divergence - Hourly = short; BB 15 min Short strategy = short; PoS 5 EMA 15 mins = 5-EMA alert-candle short;
Volume Based Bulltrade = long; GM W Pattern = Bollinger W-bottom (interpretation, confirm). Still two-sided (both tested):
5 Red / 5 Green candles - 5 min (each split into opening drive 09:20–09:45 and midday 10:00–14:30) and D-VolumeSpike.

## Moved to the up-move learner as building blocks (state scans, not entries)
Near EMA200 (±1%), Copy - Bollinger band Squeeze, Copy - Copy - Bullish Hourly Convergence New, Sowmya scan, GM Bull EMA Sorted, Monthly MACD Below 0.
GM Bear EMA Sorted is a building block of the down-move (short) learner.

## Not converted
- Daily Imp - 9c200 (EMA13 × EMA200) = existing O1-004; H-VolumeSpike = same conditions as D-VolumeSpike.
- SS_Portfolio_Entry_Monthly: needs a 200-month EMA (~17 years of history).
- VWAP Cross: not opened in the videos (conditions unknown). SS 3min Short-Voi (testwatchlist copy): test scan.
- Copies: Copy - 3 MIN 2BULL-CANDLE, Copy - Two bullish candle latest close grt pdh, 5 Min Golden Cross - Test Voi.
- Claude - Exit - 50% Exit: partial exit (MACD histogram turns negative) - not modelled.

## Bugs found in the saved Chartink scans
- GM Bearish Daily Pinbar Candle cannot fire (branch A compares a close with itself; branch B needs close > open and open > close).
- Near EMA200 (Nifty 50): "any of ≤ 1.02× / ≥ 0.98×" matches every stock.
- GM W Pattern: "lower band < SMA 20" is always true.
- GM Bull/Bear EMA Sorted compare 1h EMA100 with 15m EMA200.

## Strategy list
| ID | Chartink name | Version / what it does | TF | Side | Execution | Exit |
|---|---|---|---|---|---|---|
| O2-001 | GMAnalysis | Version: EMA 9 x 45. Daily EMA 9 crosses above EMA 45 | 1d | long | cash-long | trend |
| O2-002 | GMAnalysis | Version: EMA 9 x 200. Daily EMA 9 crosses above EMA 200 | 1d | long | cash-long | trend |
| O2-003 | GMAnalysis | Version: MACD up below zero + RSI3. MACD turning up while MACD or signal <= 0, RSI(3) >= 80 | 1d | long | cash-long | trend |
| O2-004 | MACD Cross below 0 | Daily MACD crosses above signal while below zero | 1d | long | cash-long | trend |
| O2-004-W | Weekly MACD below 0 | Weekly MACD crosses above signal while below zero | 1w | long | cash-long | trend |
| O2-006 | Excellent - Entry A - MACD Reversal | MACD crosses above signal below zero, close above EMA 21/50/200 | 1d | long | cash-long | claude |
| O2-007 | Claude Predictive MACD | Histogram below zero but rising 3 days, uptrend, volume 1.5x, RSI 40-55 and rising | 1d | long | cash-long | claude |
| O2-008 | 5 day daily 5% up | 5 days in a row each close >= 4.9% above the previous; no locked (high = low) day, volume expanding | 1d | long | cash-long | trend |
| O2-009 | 5 day price increase | 5 higher closes in a row | 1d | long | cash-long | trend |
| O2-010 | SS-2Bullish-Daily | 3 rising closes, yesterday RSI(3) > 80, EMA 20 reclaim or EMA13 > EMA20, volume >= 2x | 1d | long | cash-long | trend |
| O2-011 | SS-2Bullish-Hourly | 2 closes above prior highs, RSI(3) > 80, EMA 20 clause, volume >= 2x (daily candles) | 1d | long | cash-long | trend |
| O2-012 | SS-2Bearish-Daily | 3 falling closes and lows, yesterday RSI(3) < 20, EMA 20 break or EMA13 < EMA20, volume >= 2x | 1d | short | PE | trend |
| O2-018-L | D-VolumeSpike | Version: Long. Volume > 2x the highest of the previous 15 days, green day | 1d | long | cash-long | meanrev |
| O2-018-S | D-VolumeSpike | Version: Short. Volume > 2x the highest of the previous 15 days, red day | 1d | short | PE | meanrev |
| O2-019 | Volume Based Bulltrade | Yesterday: very low volume (< half EMA 20 volume) with RSI(3) > 80 | 1d | long | cash-long | trend |
| O2-020 | 9 gt 20 at 45 deg | EMA 9 > EMA 20 and EMA 9's 1-day rise > 45% of the day's range | 1d | long | cash-long | trend |
| O2-020-ATR | 9 gt 20 at 45 deg | Version: (ATR slope). EMA 9 > EMA 20 and EMA 9 rose >= 0.25 ATR per day over 3 days (scale-free variant) | 1d | long | cash-long | trend |
| O2-023 | JULY23_DAILY_BULL_BBSCAN | Daily candle reclaims EMA 50 in a downtrend (EMA13 < 50 < 200) | 1d | long | cash-long | meanrev |
| O2-026 | Bearish EMA Crossover | EMA 13 crosses below EMA n with close below it | 1d | short | PE | trend |
| O2-032 | SS_Portfolio_Entry | Below EMA 200 recovery: 2 higher closes, above EMA 13, RSI(300) >= 49, volume up | 1d | long | cash-long | meanrev |
| O2-032-W | SS_Portfolio_Entry_Weekly | Weekly: below EMA 200 recovery, 2 higher closes, above EMA 13, RSI(300) >= 49, volume up | 1w | long | cash-long | meanrev |
| O2-033 | DS - Bollinger Band Bullish Breakout - Daily | Candle crosses SMA 20 from below and pierces the upper band; d/w/m RSI >= 59 | 1d | long | cash-long | trend |
| O2-034 | DS - Bollinger Band Bullish Breakout - Daily | Version: (follow-through). Day after an SMA 20 cross closes higher; d/w/m RSI >= 59 | 1d | long | cash-long | trend |
| O2-036 | GM_Longterm_InvestmentStrategy | Monthly RSI(3) < 20 (deeply oversold) + 2 up days | 1d | long | cash-long | meanrev |
| O2-038 | GM Bullish Daily Pinbar Bull Candle | Green hammer: lower wick >= 2x body and >= 1.75x upper wick | 1d | long | cash-long | meanrev |
| O2-039 | GM Bullish Daily Pinbar Candle Today | Hammer (green or red) that makes a lower low | 1d | long | cash-long | meanrev |
| O2-040 | GM Bullish Daily Pinbar Candle - Final | Hammer 2 days ago, yesterday closes above its high, today higher close | 1d | long | cash-long | meanrev |
| O2-041 | GM Bearish Daily Pinbar Candle | Shooting star after 2 green days, gap-up open fails | 1d | short | PE | meanrev |
| O2-047 | 44EMA Low | Daily candle touches EMA 44 of lows | 1d | long | cash-long | meanrev |
| O2-048 | 44EMA High | Daily candle touches EMA 44 of highs | 1d | long | cash-long | meanrev |
| O2-049 | Entry Type B — Trend Continuation Pullback | Uptrend pullback to EMA 21, MACD hist > 0, RSI 45-62, quiet volume, turnover > 10 cr | 1d | long | cash-long | claude |
| O2-050 | Near EMA200 | Version: cross above. Close crosses above EMA 200 | 1d | long | cash-long | trend |
| O2-051 | Near EMA200 | Version: cross below. Close crosses below EMA 200 | 1d | short | PE | trend |
| O2-058 | GM W Pattern | Bollinger W-bottom: a low below the lower band in the last 10 days, now holding above it, RSI < 50, open above yesterday's open | 1d | long | cash-long | meanrev |
| O2-013 | GR - Intraday buy | 15m: close above previous high, two green bars, RSI(3) > 80 | 15m | long | intraday-long-or-CE | intraday |
| O2-014 | 5 Min Golden Cross | 5m: EMA 4 > 9 > 18, green bar, higher close, daily RSI(3) > 75 | 5m | long | intraday-long-or-CE | intraday |
| O2-015 | Sowmya 3 min Strategy | 5m (scan is 3m): two rising closes, daily volume > 125000, daily RSI(3) > 79 | 5m | long | intraday-long-or-CE | intraday |
| O2-016 | 15 min Volume Shockers | Version: Up. 15m: 5% up bar on 1.5x volume | 15m | long | intraday-long-or-CE | intraday |
| O2-016-ATR | 15 min Volume Shockers | Version: Up (ATR). 15m: bar up >= 2.5 ATR on 1.5x volume (scale-free variant) | 15m | long | intraday-long-or-CE | intraday |
| O2-017 | 15 min Volume Shockers | Version: Down. 15m: 5% down bar on 1.5x volume | 15m | short | intraday-short-or-PE | intraday |
| O2-017-ATR | 15 min Volume Shockers | Version: Down (ATR). 15m: bar down >= 2.5 ATR on 1.5x volume (scale-free variant) | 15m | short | intraday-short-or-PE | intraday |
| O2-021 | New BB 15 Min | Candle reclaims EMA 50 in a downtrend (EMA13 < 50 < 200) | 15m, 60m | long | intraday-long-or-CE | intraday |
| O2-022 | July23_15MIN_BuLL_BBSCAN | Previous 15m bar reclaimed EMA 50 in a downtrend, current bar closes higher (3m confirmation mapped to 15m) | 15m | long | intraday-long-or-CE | intraday |
| O2-024 | HA-13-50-200 | Heikin-Ashi close crosses above EMA 50 in a downtrend (signal on HA, fills on real prices) | 15m, 60m | long | intraday-long-or-CE | intraday |
| O2-025 | BB 15 min Short strategy | 15m red candle cuts EMA 50 down (EMA13 crossed below 50, or daily EMA13 > 50) | 15m | short | intraday-short-or-PE | intraday |
| O2-026-I | Bearish EMA Crossover | Version: (intraday). Intraday: EMA 13 crosses below EMA n with close below it | 15m, 60m | short | intraday-short-or-PE | intraday |
| O2-027 | BB 100-200-400 30 min - GM | 30m: EMA 100 crosses above EMA 200, EMA 200 >= EMA 400, EMAs within 5% | 30m | long | intraday-long-or-CE | intraday |
| O2-028 | SS-Swing-1hr-Bullish | 1h + daily stack 13 > 20 > 100, 1h volume up, breaks yesterday's high | 60m | long | intraday-long-or-CE | intraday |
| O2-029 | SS-Swing-1hr-Bearish | 1h + daily stack 13 < 20 < 100, 1h volume up, breaks yesterday's low | 60m | short | intraday-short-or-PE | intraday |
| O2-030 | SS - 3 MIN 2BULL-CANDLE | 5m (scan is 3m) + 15m stack 13 > 20 > 50 > 100, open = low, breakout bar, RSI(3) > 80 | 5m | long | intraday-long-or-CE | intraday |
| O2-031 | Final-SS-GM-3Min-Bearish | 5m (scan is 3m) + 15m stack 100 > 20 > 13, volume up, new day low | 5m | short | intraday-short-or-PE | intraday |
| O2-035 | Bollinger Feb25 | 1h: tight 20-bar base (low within 5% of SMA 20), close above the highest upper band, RSI(3) > 80 | 60m | long | intraday-long-or-CE | intraday |
| O2-037 | GM Bearish RSI Divergence - Hourly | 1h: price above a bar 1-5 back while RSI(14) is lower, RSI >= 60 | 60m | short | intraday-short-or-PE | intraday |
| O2-042-L-OPEN | 5 Red candles - 5 min | Version: Long (opening drive). 5 consecutive candles, opening drive window, long side | 5m | long | intraday-long-or-CE | intraday |
| O2-042-L-MID | 5 Red candles - 5 min | Version: Long (midday). 5 consecutive candles, midday window, long side | 5m | long | intraday-long-or-CE | intraday |
| O2-042-S-OPEN | 5 Red candles - 5 min | Version: Short (opening drive). 5 consecutive candles, opening drive window, short side | 5m | short | intraday-short-or-PE | intraday |
| O2-042-S-MID | 5 Red candles - 5 min | Version: Short (midday). 5 consecutive candles, midday window, short side | 5m | short | intraday-short-or-PE | intraday |
| O2-043-L-OPEN | 5 Green candles - 5 min | Version: Long (opening drive). 5 consecutive candles, opening drive window, long side | 5m | long | intraday-long-or-CE | intraday |
| O2-043-L-MID | 5 Green candles - 5 min | Version: Long (midday). 5 consecutive candles, midday window, long side | 5m | long | intraday-long-or-CE | intraday |
| O2-043-S-OPEN | 5 Green candles - 5 min | Version: Short (opening drive). 5 consecutive candles, opening drive window, short side | 5m | short | intraday-short-or-PE | intraday |
| O2-043-S-MID | 5 Green candles - 5 min | Version: Short (midday). 5 consecutive candles, midday window, short side | 5m | short | intraday-short-or-PE | intraday |
| O2-044 | PoS 5 EMA 15 mins | 5 EMA short: previous 15m bar entirely above EMA 5 (alert candle), current red bar breaks its low, after a strong prior day | 15m | short | intraday-short-or-PE | intraday |
| O2-045 | Supertrend Bear 15 min | 15m Supertrend(4,2) flipped bearish on the previous bar, lower close | 15m | short | intraday-short-or-PE | intraday |
| O2-046 | MySuperTrend | 15m Supertrend(4,2) flips bullish, RSI(3) > 80, two rising green Heikin-Ashi bars | 15m | long | intraday-long-or-CE | intraday |
| O2-059 | Super Swing Setup -Bullish | 1h EMA 13 > EMA 20 and EMA 13 crosses above EMA 100 | 60m | long | intraday-long-or-CE | intraday |
| O2-009-B | 5 day price increase | Version: 3 higher closes + RSI(14) >= 60. 3 higher closes in a row with daily RSI(14) >= 60 | 1d | long | cash-long | trend |
| O2-060 | Daily Imp - 10 20 30 EMA ADX 20+ | EMA 10 crosses above EMA 20 and EMA 30 on the same day, ADX(8) > 20 | 1d | long | cash-long | trend |
| O2-061 | GM Bearish 15 min Pinbar Candle | Shooting star (red or green) after 2 rising green bars, next bar red with a lower close | 15m | short | intraday-short-or-PE | intraday |
| O2-062 | GM Bearish 3 min Pinbar Candle | 5m (scan is 3m): shooting star after 2 rising green bars, next bar red with a lower close | 5m | short | intraday-short-or-PE | intraday |
| O2-063 | Bear EMA 5 -5min | 5 EMA short (5m): previous candle entirely above EMA 5 (alert candle), current candle breaks its low | 5m | short | intraday-short-or-PE | intraday |
| O2-064 | Bear 5 EMA - 15 min | 5 EMA short (15m): previous candle entirely above EMA 5 (alert candle), current candle breaks its low | 15m | short | intraday-short-or-PE | intraday |
| O2-065 | Bear 5 EMA - 1hr | 5 EMA short (60m): previous candle entirely above EMA 5 (alert candle), current candle breaks its low | 60m | short | intraday-short-or-PE | intraday |
| O2-066 | Bear 5 EMA - Daily | 5 EMA short (1d): previous candle entirely above EMA 5 (alert candle), current candle breaks its low | 1d | short | PE | trend |
| O2-067 | Bear 5 EMA - Weekly | 5 EMA short (1w): previous candle entirely above EMA 5 (alert candle), current candle breaks its low | 1w | short | PE | trend |
| O2-068 | Bear 5 EMA - Monthly | Monthly 5 EMA short on daily candles: last month's candle above its EMA 5, price breaks that month's low | 1d | short | PE | trend |
| O2-069 | Final-SS-GM-3min-Bullish | 5m (scan is 3m) + 15m stack 13 > 20 > 100, volume up, new day high | 5m | long | intraday-long-or-CE | intraday |
| O2-070 | SS 3min Short-Voi | 5m (scan is 3m) + 15m stack 100 > 20 > 13, daily volume above its 20-day average | 5m | short | intraday-short-or-PE | intraday |
| O2-071 | SS 3min Long-Voi | 5m (scan is 3m) + 15m stack 13 > 20 > 100, daily volume above its 20-day average | 5m | long | intraday-long-or-CE | intraday |
| O2-072 | Chartink 3 min LONG | 5m (scan is 3m): candle makes a new high of the day | 5m | long | intraday-long-or-CE | intraday |
| O2-073 | ChartinK - 3 Min SHort | 5m (scan is 3m): candle makes a new low of the day | 5m | short | intraday-short-or-PE | intraday |
| O2-074 | BB Strategy 5 mins | 5m squeeze release: previous band width < 2 ATR, now > 2 ATR, high above the upper band, green candle | 5m | long | intraday-long-or-CE | intraday |
| O2-075 | PDH break out | 5m (scan is 3m): close crosses above the previous day's high | 5m | long | intraday-long-or-CE | intraday |
| O2-076 | GM_RSI Div_New Bullish 15 min (RSI 14) | 15m bullish divergence: open below a bar 1-5 back while RSI(14) and volume are higher | 15m | long | intraday-long-or-CE | intraday |
| O2-077 | Bearish - 5 Min Golden Cross | 5m: EMA 4 <= 9 <= 18, red candle, lower close, daily RSI(3) < 30 | 5m | short | intraday-short-or-PE | intraday |
| O2-078 | GR - Intraday Sell | 15m: close below previous low, two red bars, RSI(3) < 20 (mirror of GR - Intraday buy) | 15m | short | intraday-short-or-PE | intraday |

## Checks
All strategies parse and run on real NSE candles (RELIANCE, HDFCBANK, ICICIBANK; 1d, 1w, 60m, 30m, 15m, 5m and
cross-timeframe). Rare by design (0 firings on 3 stocks): 5 day daily 5% up, 15 min Volume Shockers - Up (5%),
SS_Portfolio_Entry_Weekly. Three stocks say nothing about edge; the owner-run pipeline gives the evidence.
