# M3.7 evidence report — 2026-11-02

Every figure here is `EXPLORATORY_BIASED`; see the reasons below. LightGBM was trained and
evaluated on four daily horizons. Its ranking skill is small but consistent, and about the
same as logistic regression. After the assumed costs, no model beat the equal-weight
benchmark on any horizon. Nothing shows a tradable edge, and no model was promoted.

## Run

| Item | Value |
| --- | --- |
| Experiment | `reports/m37/316b5edcfa136dcf30436aa0972e9e3a6248fd9ae0e2b7dc6e8253fd5d893cff` |
| Inputs | M3.1 `045e4ba0…`, M3.2 `26e41d00…`, both manifest-verified; empirical session axis |
| Environment | Local CPU; Python 3.12, LightGBM 4.7.0; 1 min 52 s wall time, 3.3 GB peak memory |
| Universe | 1,977 eligible equities; 223 otherwise-eligible symbols set aside as ETFs (321 matched the ETF heuristic overall) |
| Round-trip cost | 0.4932% (assumed, unverified) |
| Locked holdout | 2026-04-29 to 2026-10-29: not accessed, 0 rows read |
| Rankings, promotion, gates | None written; never promoted; gates unchanged |

## Coverage

| Horizon | Folds | Labelled eligible rows | Symbols | As-of span | Pooled test rows | Test dates |
| --- | --- | --- | --- | --- | --- | --- |
| 1-7d | 7 | 546,328 | 1,447 | 2022-11-29 to 2026-01-07 | 350,501 | 441 |
| 7-15d | 7 | 558,268 | 1,449 | 2022-11-29 to 2026-02-01 | 350,494 | 441 |
| 15-30d | 6 | 533,481 | 1,444 | 2022-11-29 to 2025-12-12 | 304,036 | 378 |
| 1-3m | 5 | 535,715 | 1,443 | 2022-11-29 to 2025-12-17 | 251,671 | 315 |

The other horizons were not trained:

- **3-6m and 6-12m:** their stored split plans have no folds; at least three are required.
- **Intraday:** disabled, because no valid minute data exists.

LightGBM fold train sizes grew from about 129,000 rows to 424,000 (1-7d). Early stopping
on validation log loss chose 1 to 57 trees.

## Out-of-sample results

All figures are pooled over the walk-forward test folds.

- The base rate of beating the same-date median is 0.4997.
- "Top-10% precision" is the share of top-decile rows that beat the median.
- Portfolio figures use non-overlapping top-decile holding periods. Each period pays the
  round-trip cost and is compared with the same-date equal-weight benchmark, which is
  held without cost.

| Horizon | Model | AUC | Rank IC | Log loss (base 0.6931) | ECE | Top-10% precision | Periods | Net / period | Excess / period | Cumulative net (benchmark) | Max DD | Max DD vs benchmark |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1-7d | Momentum | 0.503 | −0.002 | 0.6934 | 0.000 | 0.482 | 63 | −0.53% | −0.70% | −31.9% (+6.8%) | −44.0% | −37.4% |
| 1-7d | Logistic | 0.530 | 0.058 | 0.6923 | 0.007 | 0.544 | 63 | −0.06% | −0.24% | −7.6% (+6.8%) | −30.2% | −20.0% |
| 1-7d | LightGBM | 0.529 | 0.058 | 0.6922 | 0.002 | 0.544 | 63 | +0.02% | −0.16% | −3.9% (+6.8%) | −29.3% | −13.9% |
| 7-15d | Momentum | 0.503 | −0.002 | 0.6938 | 0.009 | 0.479 | 30 | −1.06% | −1.12% | −30.1% (−1.0%) | −40.5% | −29.5% |
| 7-15d | Logistic | 0.530 | 0.060 | 0.6928 | 0.011 | 0.545 | 30 | −0.28% | −0.35% | −9.8% (−1.0%) | −22.3% | −15.9% |
| 7-15d | LightGBM | 0.527 | 0.055 | 0.6924 | 0.002 | 0.530 | 30 | −0.23% | −0.30% | −9.1% (−1.0%) | −26.5% | −16.9% |
| 15-30d | Momentum | 0.499 | −0.011 | 0.6941 | 0.012 | 0.467 | 13 | −1.22% | −1.57% | −17.6% (+1.7%) | −30.7% | −19.0% |
| 15-30d | Logistic | 0.527 | 0.053 | 0.6935 | 0.009 | 0.536 | 13 | −0.25% | −0.60% | −5.1% (+1.7%) | −24.1% | −14.6% |
| 15-30d | LightGBM | 0.532 | 0.062 | 0.6918 | 0.006 | 0.516 | 13 | −0.22% | −0.57% | −4.7% (+1.7%) | −22.8% | −10.5% |
| 1-3m | Momentum | 0.493 | −0.019 | 0.6965 | 0.026 | 0.446 | 5 | −3.88% | −1.94% | −22.7% (−13.6%) | −28.3% | −16.5% |
| 1-3m | Logistic | 0.519 | 0.041 | 0.6983 | 0.045 | 0.523 | 5 | −3.75% | −1.81% | −21.7% (−13.6%) | −28.2% | −13.6% |
| 1-3m | LightGBM | 0.530 | 0.068 | 0.6920 | 0.008 | 0.545 | 5 | −2.62% | −0.68% | −16.0% (−13.6%) | −23.9% | −7.8% |

Rank IC is the mean per fold. Precision at calibrated probability above 0.5 ranged from
0.52 to 0.53 for LightGBM. Per-fold values are in `fold-metrics.csv`, and the
period-by-period equity curves are in `portfolio/`.

## Reading the results

- **Ranking skill:** LightGBM and logistic regression show similar, small ranking skill
  (AUC 0.53, rank IC about 0.06). The pre-registered 126-day momentum heuristic shows
  none.
- **What LightGBM adds:** LightGBM improves calibration and log loss slightly at every
  horizon. It ranks no better than logistic at 1-7d and 7-15d, and slightly better at
  15-30d (AUC 0.532 against 0.527) and 1-3m (0.530 against 0.519). At 1-3m the logistic
  model is also poorly calibrated (ECE 0.045).
- **Shallow trees:** early stopping kept very few trees, which is consistent with a weak
  signal and leaves little for the trees to learn.
- **After costs:** every model trails the benchmark per period. At most 3 test folds per
  horizon had positive mean excess return: 2 or 3 for LightGBM and logistic, 0 or 1 for
  momentum.
- **Sample size:** the 15-30d horizon has 13 independent periods and 1-3m has only 5.
  Their portfolio figures are close to anecdotal.
- **Bias direction:** survivorship and unadjusted corporate actions can push results in
  either direction. These numbers do not describe any deployable strategy.

## Trial budget

The shared ledger is `reports/m35/trial-ledger.jsonl`. It had 18 entries before this run
and 30 after. Its backup is `trial-ledger.before-m37.jsonl`, kept in the session
scratchpad.

| Horizon | Before (M3.5) | This run | Total | Budget |
| --- | --- | --- | --- | --- |
| 1-7d | 6 | 3 (trials 7–9) | 9 | 20 |
| 7-15d | 6 | 3 (trials 7–9) | 9 | 20 |
| 15-30d | 6 | 3 (trials 7–9) | 9 | 20 |
| 1-3m | 0 | 3 (trials 1–3) | 3 | 20 |

Each entry records `pipeline: m37` and experiment ID `316b5edc…`. One trial is one model on
one horizon; there was no hyperparameter search.

## Biases and remaining blockers

- **Survivorship:** the universe is today's Shoonya master, so securities delisted in the
  span are absent.
- **Corporate actions:** there is no catalog, and prices are unadjusted. Only flagged
  moves of 20% or more are filtered.
- **Listing, alias and calendar evidence:** none exists. The session axis is the empirical
  union of observed sessions. M3.6 lists the files needed.
- **ETF separation:** ETFs are identified by a symbol-name heuristic, not by an NSE list.
- **Costs:** the cost schedule is an unverified assumption.
- **Untrained horizons:** 3-6m and 6-12m have too few folds for the walk-forward plans.
- **Intraday:** needs valid minute data.
- **Gated training:** M3.5 still needs every reference gate to pass. Its holdout stays
  locked until the promotion criteria pass on gated data.
