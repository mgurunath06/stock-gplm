# M3.7 EXPLORATORY_BIASED daily training

M3.7 fits LightGBM, logistic regression and a momentum heuristic on the existing local
Shoonya daily data, even though the M3.5 reference gates are blocked. Because of that,
every artefact is tagged `EXPLORATORY_BIASED`. Nothing here is promoted, ranked, traded
or scored on the locked holdout, and the M3.5 gates and policy are unchanged. Results of
the first real run are in the [evidence report](m37-evidence-report-2026-11-02.md).

```bash
.venv/bin/python -m stock_gplm.m37.cli \
  --readiness-run reports/m3/<m31-id> --research-run reports/m32/<m32-id> \
  --trial-ledger reports/m35/trial-ledger.jsonl --output-root reports/m37
```

The defaults are `--policy config/m37/exploratory-biased-policy.json` and
`--etf-heuristic config/m37/etf-symbol-heuristic.json`. `pip install -e .` also installs
the command as `stock-gplm-explore`.

## Why the results are biased

| Bias | Effect |
| --- | --- |
| Survivorship | The universe is today's Shoonya master; securities delisted in the span are absent |
| No corporate-action catalog | Prices are unadjusted; only flagged moves of 20% or more are filtered |
| No listing or alias history | Membership on each date and symbol renames are not established |
| Empirical session axis | Sessions are the observed union, not an NSE calendar |
| Heuristic ETF separation | ETFs are identified by symbol naming, not by an exchange list |
| Unverified costs | The 0.49% round trip is an assumption |

## What is reused unchanged

- the manifest checks on the M3.1 and M3.2 inputs;
- the M3.2 features and labels;
- the stored walk-forward plans, rebuilt and verified, with their purge and embargo;
- the M3.5 universe filters, the benchmark-relative target, the models and the Platt
  calibration fitted on the validation partition;
- the M3.5 metrics.

The model settings match M3.5 exactly. No hyperparameter was searched.

## Holdout isolation

For each horizon, every row dated after the last walk-forward test span is dropped before
any frame is built. Holdout labels are therefore never read, and holdout rows never reach
a model. A test poisons every holdout label and requires byte-identical metrics. Test
partitions still pass the M3.5 boundary check against the holdout start.

## Horizons

A daily horizon trains only when its stored plan has at least three folds. Each horizon is
trained independently. Intraday stays disabled until valid minute data exists.

## ETF separation

`config/m37/etf-symbol-heuristic.json` classes a symbol as ETF by naming conventions:

- `BEES` or `ETF`;
- index or asset stems with fund-house suffixes such as `ADD`, `BETA`, `CASE` or `AXIS`;
- fund-house index product names;
- a list of known companies that the patterns would otherwise catch.

The status is `HEURISTIC_UNVERIFIED`. ETFs are excluded from the equity universe and
listed in `etf-separated.csv`. No ETF model is trained. When the M3.6
`instrument_classification` component validates, it should replace the heuristic.

## Trial budget

Trials are reserved before any fit in the shared ledger (`reports/m35/trial-ledger.jsonl`).
Each trial is one model on one horizon. The budget stays at 20 per horizon and counts every
earlier M3.5 and M3.7 trial; the policy loader refuses any other budget. Each entry
records `pipeline: m37`, the `experiment_id`, both upstream run IDs and the next trial
number. A run that would exceed the budget is refused before anything is written.

## Metrics

| Metric | Meaning |
| --- | --- |
| AUC | Ranking of rows by raw score |
| Log loss, Brier, ECE | Calibrated probability quality, against the base rate |
| Precision (top 10%) | Share of top-decile rows that beat the same-date median |
| Precision at 0.5 | The same, among rows with calibrated probability above 0.5 |
| Portfolio | Non-overlapping top-decile holding periods over the pooled test folds |

Each metric is reported per fold, as a fold mean, and pooled over all out-of-sample test
rows. The portfolio figures are:

- net return per period after the full round trip;
- the same-date equal-weight benchmark;
- excess return;
- cumulative return;
- maximum drawdown, absolute and relative to the benchmark.

Sample sizes are reported with every metric.

## Outputs (`reports/m37/<experiment-id>/`)

| File | Content |
| --- | --- |
| `run-manifest.json` | Binding, bias reasons, trained and skipped horizons, trials, output hashes and safety flags |
| `models/<h>/<model>.fold<k>.*` | Weights from each horizon's last walk-forward fold, with calibration and importance; LightGBM text or JSON, never pickle |
| `metrics.json`, `fold-metrics.csv`, `portfolio/<h>.<model>.csv` | Evaluation |
| `feature-schema.json`, `dataset-fingerprints.json` | The 41 features, plus row hashes per horizon and per fold partition |
| `universe.csv`, `etf-separated.csv` | Static eligibility, instrument class and the separated ETFs |
| `gates-disclosure.json` | The unchanged M3.5 gate report, for the record |
| `config/` | Copies of the policy and the ETF heuristic |
| `trials.json` | Trials this run reserved |

The experiment ID hashes the inputs, the policy, the heuristic, the library versions and
`M37_VERSION`.
