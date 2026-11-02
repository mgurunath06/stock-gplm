# M3.5 research-only daily training

M3.5 trains and evaluates daily cross-sectional models on a manifest-verified M3.1
readiness run and the M3.2 research run built from it. It runs on local CPU, reads no raw
file, places no order and writes nothing to Oracle. Minute data is not required.
Results for the first real run are in the
[evidence report](m35-evidence-report-2026-11-02.md).

```bash
pip install -e '.[m35]'          # numpy, pandas, scikit-learn, lightgbm; see requirements-m35.lock
.venv/bin/stock-gplm-train \
  --readiness-run reports/m3/<m31-id> --research-run reports/m32/<m32-id> \
  --policy config/m35/daily-training-policy.json --output-root reports/m35
```

## Gates and modes

The run rechecks the hash of every M3.1 and M3.2 output it uses. It also requires the
M3.2 run to bind that exact M3.1 run, and it refuses an output root inside the raw tree
or inside either upstream run. It rebuilds each stored walk-forward plan, requires an
exact match with the stored plan, and verifies every purge and embargo boundary again.

| Required daily gate | Source | Can M3.5 satisfy it? |
| --- | --- | --- |
| `raw_candle_acceptance` | M3.1 | Only as `PASS_SCOPED_TO_ELIGIBLE_UNIVERSE`, when every M3.1 issue is a non-accepted segment of an excluded symbol |
| `authoritative_calendar` | M3.2 | No |
| `point_in_time_membership` | M3.2 | No |
| `alias_history` | M3.2 | No |
| `corporate_action_coverage` | M3.2 | No |
| `minute_data` | M3.2 | Not required for daily-only training |

A horizon also needs at least three walk-forward folds.

| Mode | When | Models | Locked holdout |
| --- | --- | --- | --- |
| `MODEL_TRAINING` | Every required gate passes | Heuristic, logistic, LightGBM | Scored once per M3.2 run and horizon |
| `EXPLORATORY_BASELINE_ONLY` | Any gate blocked | Heuristic, logistic | Never scored |

LightGBM may not appear in the exploratory model list; the policy loader refuses it.

## Universe

Static eligibility uses data quality only, never returns:

- the symbol has validated candles and no non-accepted segment;
- it has at least 252 observations.

Row eligibility at as-of `t` is causal:

- the median of `close x volume` over the trailing 20 observations is at least ₹5 crore;
- the close is at least ₹20;
- 20-session volatility is at least 0.3%, which removes near-constant money-market units;
- no `LARGE_CLOSE_MOVE` flag falls within the trailing 252 observations, because an
  unadjusted corporate action would corrupt the lookback features.

The universe is current-master symbols only, so it carries survivorship bias until
listing history exists. The master marks ETFs as `EQ`, so index and sector ETFs stay
in the universe. They are not removed by inference.

## Features, target and costs

The 41 model features are the 32 scale-free M3.2 columns, the MACD line and histogram
divided by the close, log trailing turnover, and the same-date percentile of six columns. Rupee
price-level columns are excluded. The target is 1 when the gross return from the next
open to the bucket's last close exceeds the same-date median among eligible rows.

The training partition alone drops rows whose label window contains a flagged move.
Validation, test and holdout keep every row, so evaluation is never filtered on future
information.

Costs come from `config/m35/daily-training-policy.json`. They model a delivery round trip
with brokerage, STT, exchange, SEBI, stamp, GST and 10 bps of slippage on each side,
about 49 bps in total. These rates are assumptions to verify against current schedules.

## Evaluation

For each fold the model fits on train, is calibrated on validation, and is scored on test.
Calibration uses Platt scaling by default; isotonic remains selectable, but on the first real
run its sparse top bins were overconfident out of sample.

- Ordering metrics use the raw score: AUC, mean daily rank IC, and top-decile return.
- Probability metrics use the calibrated value: log loss, Brier, ECE and reliability bins,
  each compared with the training base rate.
- The benchmark is the same-date equal-weight eligible mean, held without cost. The top
  decile pays the full round-trip cost.
- Because 7-day and longer labels overlap on consecutive dates, a non-overlapping
  subsample is reported as well.

The final model trains on everything purged before the last test span, is calibrated on
that span, and feeds the ranking. In training mode it is then scored on the locked holdout.

## Trial budget and holdout

Every model trial is appended to `<output-root>/trial-ledger.jsonl` before any fit. A run
that would exceed the per-horizon budget, 20 by default, is refused. A holdout is reserved
in `holdout-ledger.jsonl` before it is scored. Any later run on the same M3.2 run and horizon
reports `ALREADY_CONSUMED_BY_RUN <id>` instead of a second score.

## Outputs

| File | Content |
| --- | --- |
| `gates.json` | Gate statuses, blockers and mode per horizon |
| `universe.csv`, `universe-summary.json` | Static eligibility per symbol and row exclusion counts |
| `metrics.json`, `fold-metrics.csv` | Walk-forward and holdout metrics, with reliability tables |
| `models/<h>/` | Logistic coefficients and scaler as JSON, LightGBM model text, calibration JSON; never pickle |
| `ranking-<h>.csv` | Ranking for the latest session (see below) |
| `trials.json` | Trials this run reserved, numbered by the root's ledger |
| `run-manifest.json` | Input hashes, policy, library versions, statuses and output hashes |

Each ranking row carries:

- the price date and the unadjusted close;
- the raw score and the calibrated probability;
- the walk-forward observed rate and row count in that probability bin;
- `probability_support`.

The support value is `SUPPORTED` only when the bin has at least 1,000 out-of-sample rows
and the observed rate there stayed within 0.05 of the predicted rate. Every row is
labelled research-only.

The run ID binds the inputs, the policy, the library versions and `M35_VERSION`, and
contains no timestamp. Rerunning into a fresh root therefore reproduces every model,
metric and ranking byte for byte. Only `trials.json` differs, because its numbering
comes from the ledger already in the root.

## LLM assistance

The optional [LLM interface](llm-assistance.md) is advisory. Neither this package nor its
tests import it.
