# M3.5 evidence report — 2026-11-02

This run trained no gated model. Every horizon is `BLOCKED_EXPLORATORY_BASELINES_ONLY`, so
only the heuristic and logistic baselines ran. LightGBM was not fitted on real data, and
the locked holdout was not scored.

| Item | Value |
| --- | --- |
| Run | `reports/m35/7a8a796fe04151846a86e57caaadf354db49c22be9a8743db7cf98800bade3fc` |
| M3.1 / M3.2 inputs | `045e4ba0…` / `26e41d00…`, both manifest-verified |
| Data | 1,938,336 daily feature rows, 2,701 symbols, 979 observed sessions, 2022-10-31 to 2026-10-29 |
| Environment | Python 3.12.16, numpy 2.5.4, pandas 2.3.3, scikit-learn 1.9.1, lightgbm 4.7.0; local CPU |
| Reproducibility | A rerun into a fresh root gave byte-identical models, metrics and rankings; only `trials.json` differs, by its ledger numbering |
| Trials used per horizon | 6 of 20: 3 runs of 2 baselines (below) |

## Blockers

| Gate | Status |
| --- | --- |
| `raw_candle_acceptance` | `PASS_SCOPED_TO_ELIGIBLE_UNIVERSE`: the only M3.1 issues were 9 quarantined segments of 6 symbols, all excluded |
| `authoritative_calendar` | BLOCKED: sessions are the observed union |
| `point_in_time_membership` | BLOCKED: no listing history |
| `alias_history` | BLOCKED: no alias history |
| `corporate_action_coverage` | BLOCKED: no corporate-action catalog; prices are unadjusted |
| `minute_data` | Not required for daily-only training |

## Universe

The static universe has 2,200 eligible symbols. The rest were excluded:

| Reason | Symbols |
| --- | --- |
| Fewer than 252 observations | 495 |
| No validated candles | 14 |
| A non-accepted segment | 6 |

Row filters left 732,886 eligible rows:

| Rows excluded | Reason |
| --- | --- |
| 1,109,492 | Below the liquidity, price or volatility floor |
| 40,305 | A flagged move within 252 trailing observations |

834 symbols are eligible on 2026-10-29. ETFs that the master marks `EQ`, such as
`NIFTYBEES-EQ` and `MON100-EQ`, remain in the universe.

## Walk-forward results

The figures below cover out-of-sample test folds from 2024-03-20 to 2026-02-01, with the
holdout untouched. Labels are benchmark-relative, and the base rate is 0.50.
"Top-decile net" is the mean per-date excess over the equal-weight benchmark after a
0.49% round trip.

| Horizon | Model | AUC | Rank IC | Log loss (base 0.6931) | ECE | Top-decile net | Folds > 0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1-7d | heuristic momentum | 0.504 | 0.001 | 0.6934 | 0.013 | −0.66% | 0/7 |
| 1-7d | logistic | 0.533 | 0.056 | 0.6925 | 0.018 | −0.32% | 2/7 |
| 7-15d | heuristic momentum | 0.504 | 0.001 | 0.6938 | 0.014 | −0.84% | 2/7 |
| 7-15d | logistic | 0.534 | 0.060 | 0.6930 | 0.023 | −0.25% | 2/7 |
| 15-30d | heuristic momentum | 0.500 | −0.007 | 0.6943 | 0.030 | −1.26% | 2/6 |
| 15-30d | logistic | 0.530 | 0.056 | 0.6937 | 0.038 | −0.07% | 3/6 |

The logistic baseline has small, consistent rank skill: AUC ≈ 0.53 and IC ≈ 0.06. It does
not beat the benchmark after costs at any horizon, and at 15-30d its log loss is worse
than the base rate. Fold results swing by regime; for 15-30d the non-overlapping means
ranged from −2.7% to +3.2%. Nothing here shows a tradable edge.

## Calibration

The first exploratory run used isotonic calibration. Its top bins were overconfident out
of sample: at 15-30d, predicted 0.66–0.72 against an observed 0.41–0.42. Platt scaling
replaced it. That decision used test-fold evidence, so it is recorded as extra trials.
The earlier runs `ed8f4718…` (isotonic) and `31cd1f07…` (Platt, without the support
column) are kept.

On 2026-10-29 the highest raw scores lie beyond nearly all walk-forward evidence, so their
probabilities are extrapolations.

| Horizon | Rows with `SUPPORTED` probability | First supported rank | Supported probability range |
| --- | --- | --- | --- |
| 1-7d | 660 of 834 | 94 | 0.40–0.60 |
| 7-15d | 680 of 834 | 2 | 0.40–0.69 |
| 15-30d | 436 of 834 | 157 | 0.40–0.60 |

## Ranking

The three `ranking-<h>.csv` files rank the 834 eligible symbols as of 2026-10-29, using
unadjusted closes. Each row is labelled `EXPLORATORY_BASELINE_GATES_BLOCKED` and research
only. The ranking comes from a baseline that loses to its benchmark after costs. It is
neither a recommendation nor a signal.

## Next action

Supply checksum-bound M2.1 evidence, in priority order:

1. An NSE trading calendar.
2. A corporate-action catalog with split, bonus and rights ratios.
3. Listing and delisting history.
4. Alias history.

Then rebuild M3.2 with that evidence and rerun M3.5. LightGBM training and the single
holdout evaluation start automatically once the gates pass. Also verify the cost
schedule, and decide whether ETFs belong in the stock universe.
