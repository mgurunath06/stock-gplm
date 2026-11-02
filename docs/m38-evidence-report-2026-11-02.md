# M3.8 evidence report — 2026-11-02

All results are `EXPLORATORY_BIASED` development-data diagnostics: survivorship, no
corporate-action catalog, and assumed costs. No new model was selected, and the trial
ledger is unchanged. The locked holdout was not read.

| Item | Value |
| --- | --- |
| Run | `reports/m38/bdecc646035ebff050c3852d6bbe8f0563c35b2f3b49584e392729f16a40fc38` |
| Replayed | M3.7 `316b5edc…`: 12 counted trials, every pooled AUC matched exactly |
| Trial ledger | Hash `419f2d83…` unchanged; still 9, 9, 9 and 3 trials of 20 for 1-7d, 7-15d, 15-30d and 1-3m |
| Development data | Candles before 2026-04-29: 1.62 million rows, 2,429 symbols |
| Hypothesis tests | 280 |

## 1. Why ranking skill does not become net return

Top-decile LightGBM figures, per non-overlapping holding period:

| Horizon | Periods | Gross excess vs benchmark | t | Actual turnover | Net at full round trip (M3.7 basis) | Net at actual turnover | Breakeven round trip | Benchmark minus bottom decile | Beta |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1-7d | 63 | +0.33% | 1.97 | 53% | −0.16% | +0.07% | 0.62% | +0.36% | 0.98 |
| 7-15d | 30 | +0.20% | 0.51 | 60% | −0.30% | −0.11% | 0.32% | +0.56% | 0.82 |
| 15-30d | 13 | −0.07% | −0.10 | 71% | −0.57% | −0.43% | <0 | +0.79% | 0.77 |
| 1-3m | 5 | −0.19% | −0.11 | 73% | −0.68% | −0.57% | <0 | +1.61% | 0.87 |

1. **Cost accounting.** M3.5 and M3.7 charge a full 0.49% round trip every period, as if the
   whole portfolio turned over. At 1-7d about half the picks repeat. Re-priced at actual
   turnover, the 1-7d gross edge of 0.33% leaves a net of +0.07% per period. That is not
   significant and not a profitability claim; the result was negative only because of the
   cost assumption. The edge is about the size of the cost: at 1-7d it breaks even at a
   0.62% round trip, and at longer horizons there is no gross edge at all.
2. **Where the skill sits.** At 15-30d and 1-3m the models rank the bottom of the
   distribution well: the bottom decile trails the benchmark by 0.8% and 1.6%. The top
   decile does not beat it, and at 15-30d it is worse than decile 9. A long-only top decile
   cannot use loser-avoidance skill. AUC and IC measure the whole ordering, so both look
   healthy while the investable tail is flat.
3. **Market exposure.** Beta is 0.77–0.98 and alpha per period is near zero. At 1-7d, LightGBM
   tilts towards liquid names (72nd turnover percentile), lower volatility (40th) and weaker
   5-day momentum (40th). That is a quality and short-term-reversal mix, not stock-specific
   alpha.
4. **Regimes.** At 15-30d and 1-3m, top-decile excess swings with breadth and trend. For
   example, 1-3m LightGBM made +1.4% in low-volatility periods and −6.5% in high-volatility
   ones. With 5–13 independent periods, these swings dominate the means.

## 2. The labels do not measure what the strategy earns

The frozen label marks rows above the same-date median. The benchmark is the same-date
equal-weight mean, and costs come on top.

| Horizon | Dates where mean > median | Mean − median | Positives that do not beat the mean after cost | Rows beating the mean after cost |
| --- | --- | --- | --- | --- |
| 1-7d | 89% | 0.48% | 17% | 41% |
| 7-15d | 86% | 0.76% | 15% | 43% |
| 15-30d | 85% | 1.14% | 13% | 44% |
| 1-3m | 94% | 1.59% | 12% | 44% |

Figures are for the pooled test rows. Returns are right-skewed: the gap between mean and
median is as large as the whole round-trip cost, or larger. A classifier rewarded for
beating the median is not trained to find the right tail that beats the mean after
costs. The frozen labels were not changed.

## 3. Data-quality audit

Covers development data, before 2026-04-29.

| Finding | Count |
| --- | --- |
| Probable unadjusted splits or bonuses (ratio matches and the level persists) | 65 events in 65 symbols; 44 in eligible equities |
| Possible unadjusted actions (ratio matches only) | 13 |
| Unexplained moves of 20% or more | 260; 247 in eligible equities |
| Most common factors | 1:10 split (30), 1:2 split (15), 1:3 bonus (12), 1:5 split (9) |
| Stale-close runs of 5 or more sessions | 297 runs, 2,914 days, 68 symbols |
| Volume at 50× or more of its trailing median | 4,203 sessions |
| Missing sessions inside a symbol's span | 11,335, in 162 symbols |
| Zero-volume days; OHLC inconsistencies | 0; 0 |

- **Distorted labels.** Test rows whose label window contains a quarantined event average a
  gross return of −44% to −61%. Unflagged rows average about 0%. These are split
  artefacts, not losses.
- **Models pick them.** Such rows enter the LightGBM top decile 30 times at 1-7d and up to
  191 times at 1-3m.
- **Effect of excluding them** (diagnostic only):

  | Horizon | Top-decile gross excess | Without quarantined windows |
  | --- | --- | --- |
  | 1-7d | +0.33% | +0.38% |
  | 7-15d | +0.20% | +0.27% |
  | 15-30d | −0.07% | −0.01% |
  | 1-3m | −0.19% | −0.04% |

- **Hidden sessions.** The observed-union axis cannot reveal a session that is missing for
  every symbol.

## 4. Features

| Finding | Evidence |
| --- | --- |
| LightGBM importance is stable | Mean Spearman correlation between folds of 0.74–0.80. Leading features: log turnover, distance to the 200-day average, ATR %, 126-day momentum, distance to the 52-week high. |
| Redundancy | 18 feature pairs with \|ρ\| ≥ 0.85; for example close-to-SMA50 and MACD %, RSI and %B, 126-day momentum and close-to-SMA200 |
| Drift | 24 raw-level features have PSI above 0.25 against fold-1 training data in at least one fold; ATR % reaches 0.94. Same-date percentile features have no drift by construction. |
| Dead features | Candle-shape features (wicks, body, gap, close location, 1-day momentum) have zero gain in every fold at 15-30d and 1-3m |
| Most consistent 1-7d ICs | Negative for range %, ATR % and volatility (low volatility wins); negative for 1- and 5-day momentum (reversal); positive for log turnover (liquid names win). Each has the same sign in 5–7 of 7 folds. |

## 5. Hypotheses

Ten pre-registered, signed signals were tested over 4 horizons, overall and within 6 regime
groups: 280 tests in all. None survives Bonferroni correction with at least 20 independent
dates. The strongest uncorrected leads are below; with 280 tests, each could be noise.

| Lead | Horizon and regime | Independent dates | Mean IC | Raw p | Bonferroni p |
| --- | --- | --- | --- | --- | --- |
| Near 52-week high | 7-15d, up-trend | 25 | +0.062 | 0.0004 | 0.11 |
| Illiquidity (pre-registered sign); the IC is negative, so liquid names win | 1-7d | 109 | −0.035 | 0.0004 | 0.11 |
| Volume-confirmed 5-day move (reverses) | 1-7d | 109 | −0.024 | 0.0005 | 0.13 |
| Low volatility | 1-7d, low-volatility regime | 43 | +0.068 | 0.0007 | 0.21 |
| Short-term reversal | 1-7d | 109 | +0.030 | 0.0009 | 0.26 |

None of the tested signals has a top decile that clears costs. Relative strength (12-1) and
volatility contraction show no reliable effect. Every test also used the walk-forward test
spans. Only the locked holdout, looked at once, could confirm a lead selected here.

## Top five improvements

| Rank | Improvement | Evidence | Expected impact | Effort | Leakage risk |
| --- | --- | --- | --- | --- | --- |
| 1 | **Cost- and turnover-aware portfolio evaluation**: hold names while they stay in the buffer, and charge costs on actual trades | The 1-7d gross edge of +0.33% is entirely consumed by the full-turnover cost assumption; actual turnover is 53% | High: decides whether the 1-7d skill is investable at all | Low | Low. Any chosen holding rule is a new trial. |
| 2 | **A label aligned with net return**: excess over the equal-weight mean after costs, or a ranking or regression objective focused on the top tail | The mean beats the median on 85–94% of dates; 12–17% of positives lose after costs; skill sits in the bottom tail | High | Medium: a new versioned label contract; the frozen labels stay | Medium. The label window must keep purge and embargo, and every variant is a trial. |
| 3 | **Corporate-action catalog and quarantine** (the M3.6 corporate-actions download) | 65 probable unadjusted actions; flagged windows average −44% to −61%; excluding them adds 0.04–0.15% per period | Medium–high at 15-30d and 1-3m | Medium: manual NSE export plus M3.6 | Low, as long as ex-dates are point-in-time |
| 4 | **Feature hygiene**: replace drifting raw levels with same-date percentiles, drop redundant and dead features | 18 pairs with \|ρ\| ≥ 0.85; 24 drifting features; dead candle features at long horizons | Medium: robustness rather than return | Low | Low, provided percentiles stay within the same date |
| 5 | **A survivorship-free universe**: point-in-time listings and delisted securities' prices | Today's-master universe; M3.6 membership blocked | High for validity, unknown for return | High: licensing and data volume | Low, if constituents are point-in-time |

## New data sources

These need licence checks. Nothing was downloaded.

| Source | Value | Licensing and access | Survivorship |
| --- | --- | --- | --- |
| NSE daily bhavcopy with delivery data | Official end-of-day prices, delivery %, all traded securities including later-delisted ones | NSE Terms of Use prohibit automated collection. Manual downloads are allowed for personal, non-commercial use, which is impractical for about 1,000 daily files. A licensed NSE data feed is the compliant bulk route. | Fixes survivorship if the whole span is covered |
| NSE corporate actions, symbol changes, delisted lists | Ex-dates and ratios, aliases, membership | Manual download (M3.6 workflow) | Needed for point-in-time membership |
| Historical index constituents | Point-in-time liquid universe, breadth | NSE Indices publications; full history may need a licence | Survivorship-free only if historical lists are used |
| Licensed fundamentals and ownership (an academic or commercial vendor) | Value, quality and ownership signals with announcement dates | Paid or academic licence; check redistribution and point-in-time stamping | Depends on the vendor including dead companies |
| Shoonya broker API (current source) | Current prices only | Personal broker terms | Today's master only: survivorship-biased |
