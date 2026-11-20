# Chartink scans → strategy library (analysis, 2026-11-20)

Source: 66 screenshots in `config/strategies/ChartInk/`. Draft output: `config/strategies/drafts/chartink-draft.json`
(57 strategies, IDs O2-001..057, **not loaded by any run until approved**).

## What was in the folder
- **7 are not scans** (live-lab alerts, broker P&L, device list): 092152, 101930, 110819, 111422, 120310, 120632, 184959. Skipped.
- **59 scan screenshots → 58 distinct scans** (200511 is a crop of 200524).
- "Futures segment" read as cash market everywhere (as instructed).
- Disabled (greyed) conditions ignored. Market-cap filters ignored (no market-cap data); index segment
  (Nifty 50/100/200/500) kept as a tag only.

## Duplicates and merges
| Scan | Outcome |
|---|---|
| 200524 EMA13 × EMA200 (13×50, 13×100 disabled) | = existing **O1-004** (and O1-002/003). Not added. |
| 201344 1h EMA13 > 20 and EMA13 × EMA100 | = **O1-003** on 60m (extra filter is almost always true at the cross). Not added. |
| 200648 daily + 202041 weekly MACD × signal below zero | one rule, two timeframes → O2-004 |
| 201400 daily + 202020 weekly "below EMA200 recovery" | one rule, two timeframes → O2-032 (monthly 202010 impossible: needs a 200-month EMA ≈ 17 years) |
| 200900 branches 2 + 3 | merged (only differ on high above/below upper band) → O2-034 |
| 201554 (±1% of EMA200) + 200943 (±2%) | one rule with grid 1% / 2% → O2-052 |
| 201125 vs 201447 | near-duplicates (rising closes vs closes above highs) → kept as O2-010 / O2-011 |
| 201041 vs 201836 | identical firings on 15m (the daily 10% filter never binds) → O2-021 can be dropped in favour of O2-023 |
| Mirrors (kept both sides) | 201324 / 201334, 201803 / 201816, 201125 / 201603, 201936 / 202033, 201616 = short side of O1-001..004 |

## Bugs found in the Chartink scans
| Scan | Problem | Draft |
|---|---|---|
| 201750 shooting star | Can never fire: branch A has "3 days ago close > 3 days ago close"; branch B needs close > open **and** open > close | O2-041 = red shooting star after 2 green days, gap-up fails (confirm) |
| 200943 near EMA200 | "passes any 1" with close ≤ 1.02×EMA and close ≥ 0.98×EMA → matches every stock | fixed in O2-052 |
| 201411 | "Lower Bollinger < SMA20" is always true → scan is only RSI < 50 and open > yesterday's open | not drafted (ask) |
| 201803 / 201816 | 1h EMA100 compared with **15m** EMA200 | drafted as 1h vs 1h |

## The 57 drafted strategies by family
- **EMA / MACD (7):** O2-001 EMA9×45, O2-002 EMA9×200, O2-003 MACD turning up below zero + RSI3 ≥ 80, O2-004 MACD × signal below zero (1d, 1w), O2-005 monthly version, O2-006 Entry A MACD reversal, O2-007 Predictive MACD.
- **Momentum streaks / bursts (13):** O2-008 5 days of ≥ 4.9%, O2-009 5 higher closes, O2-010/011 momentum + volume (O2-012 short mirror), O2-013 15m breakout + RSI3, O2-014 5m EMA 4/9/18, O2-015 5m rising closes, O2-016/017 15m 5% bar, O2-018 volume > 2× 15-day max, O2-019 low-volume strength, O2-020 "9 GT 20 at 45°".
- **EMA 50 reclaim in a downtrend (5):** O2-021, O2-022, O2-023 (1d/60m/15m), O2-024 Heikin-Ashi, O2-025 short.
- **Stacks / crosses (7):** O2-026 short EMA13 × 20/50/100/200, O2-027 30m EMA100×200, O2-028/029 1h + daily stack breaks prior-day high / low, O2-030/031 5m + 15m stack, O2-032 below-EMA200 recovery.
- **Bollinger / multi-timeframe RSI (5):** O2-033/034 SMA20 cross with d/w/m RSI ≥ 59, O2-035 1h tight base breakout, O2-036 monthly RSI3 < 20 + 2 up days, O2-037 1h RSI divergence.
- **Candles (7):** O2-038 hammer, O2-039 hammer at a lower low, O2-040 hammer + 2-day confirmation, O2-041 shooting star (short), O2-042/043 5 red / 5 green 5m candles, O2-044 15m stall after a strong day.
- **Supertrend / Heikin-Ashi (2):** O2-045 short flip, O2-046 long flip.
- **Support touches / pullbacks (5):** O2-047/048 EMA44 of lows / highs, O2-049 Entry B pullback, O2-050/051 close × EMA200 up / down.
- **Condition-only (6):** O2-052 near EMA200, O2-053 15m squeeze, O2-054 1h EMA convergence, O2-055 m/w/d RSI, O2-056/057 all-timeframe stacks. These stay true for long stretches. They work better as building blocks for the up-move learner's pairs and triples than as standalone entries.

## Mapping notes
- **3-minute scans** (201250, 201301, 201827, 202059): we have no 3m bars → mapped to 5m (or 15m for the confirmation bar).
- "Daily …" inside intraday scans uses the **last completed day** (Chartink uses the day so far). This avoids look-ahead in backtests.
- Weekly and monthly values come from completed weeks and months (`htf(…, "1w")`, `htf(…, "1mo")`).
- Chartink gives entries only. Draft exit: 10 bars, stop 1.5 × ATR, target 2.5 × ATR. The up-move learner still judges every trigger by its own per-TF rule (1d: +3% within 5 days before −1.5%).

## Smoke check (3 stocks: RELIANCE, HDFCBANK, ICICIBANK; NSE data; no costs)
All 57 parse and run on real candles, with no errors on any timeframe (1d, 1w, 1mo, 60m, 30m, 15m, 5m, cross-timeframe).
Firing counts are sane except these, which are rare by design: O2-005 monthly MACD (0), O2-008 five days of ≥ 4.9% (0),
O2-016 15m 5% bar (0), O2-027 (1), O2-033 (1). **Three stocks prove nothing about edge**; the full evidence comes from
the owner-run pipeline after approval. Per-rule numbers: `state/reports/strategy-audit/2026-11-20/chartink-smoke.json`.

## Open questions for the owner
1. **Direction unclear:** 200813 (price higher, RSI lower), 200920 (15m red candle cuts EMA50), 201312 (5% down bar),
   201703 / 201713 (5 red / 5 green 5m candles), 201723, 201908, 201926. Proposal: test **both long and short**
   and let each stock × TF result decide.
2. **3-minute → 5m** mapping OK?
3. **Market cap / Nifty segment:** run on the full universe (proposal) or restrict to index lists?
4. **Condition-only scans** → add to the up-move learner's building blocks (proposal) or keep as standalone strategies?
5. **201411** (only RSI < 50 and open > yesterday's open after the always-true condition): what did you intend?
6. **Exits:** do you use a specific target or stop with these scans?
