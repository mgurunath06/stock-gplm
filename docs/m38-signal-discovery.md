# M3.8 signal discovery and data-quality audit

M3.8 explains why the M3.7 models' ranking skill does not survive costs, and it audits the
daily data. It is diagnostic only:

- it selects no new model and writes nothing to the trial ledger;
- it never reads the locked holdout;
- it never changes prices, labels or gates.

Findings are in the [evidence report](m38-evidence-report-2026-11-02.md).

```bash
.venv/bin/python -m stock_gplm.m38.cli \
  --readiness-run reports/m3/<m31-id> --research-run reports/m32/<m32-id> \
  --experiment reports/m37/<experiment-id> --trial-ledger reports/m35/trial-ledger.jsonl \
  --output-root reports/m38
```

## Boundaries

| Concern | Rule |
| --- | --- |
| Holdout | Prices, features and labels dated on or after the earliest trained holdout start (2026-04-29) are dropped before the panel is built. Each horizon's frame also stops at its last walk-forward test date. A test poisons every holdout label and requires byte-identical outputs. |
| Trials | Per-row out-of-sample scores come from replaying the trials already counted for the M3.7 experiment, with the same configuration. The run verifies that every replayed pooled AUC matches the stored one. The trial ledger's hash must be unchanged at the end, or the run fails. |
| Hypotheses | Ten signals are pre-registered with fixed signs and tested by rank IC. No model is fitted to them. Every test (signal × horizon × regime) goes into `hypothesis-ledger.csv`. |
| Significance | Student-t on non-overlapping dates, with Bonferroni correction across all tests. A result needs at least 20 independent dates to count. |
| Data | Suspected unadjusted corporate actions are listed in `quarantine-events.csv` and never corrected. |
| Scope | ETFs stay excluded as in M3.7. Gated training, the M3.5 gates and the labels are not touched. |

Promoting any hypothesis, label or portfolio rule into a model or into model selection is a
new trial against the shared 20-per-horizon budget.

## Outputs (`reports/m38/<run-id>/`)

| File | Content |
| --- | --- |
| `decomposition.json` | For each horizon and model: top-decile gross excess, actual turnover, cost at full and at actual turnover, breakeven cost, beta, top-vs-benchmark and benchmark-vs-bottom spreads, deciles, exposure tilts, returns by regime, effect of quarantined windows, and a label audit |
| `portfolio-periods.csv` | Every non-overlapping holding period behind the decomposition |
| `ohlcv-audit.json`, `quarantine-events.csv`, `symbol-hygiene.csv` | Large-move classification; stale runs, volume spikes, OHLC consistency and missing sessions per symbol |
| `feature-evaluation.csv` | For each feature and horizon: IC, t, sign consistency across folds, IC by regime, PSI drift |
| `feature-importance-by-fold.csv`, `feature-stability.json` | LightGBM gain per fold, its stability across folds, and highly correlated feature pairs |
| `hypothesis-ledger.csv` | Every hypothesis test, with raw and Bonferroni p-values |
| `regimes.csv` | Causal per-date breadth, trend and volatility regimes |
| `replay-check.json`, `run-manifest.json` | Replay verification, ledger hash, the trial count (0) and output hashes |

## Classifying large moves

A close-to-close move of 20% or more is classified as follows:

| Class | Rule |
| --- | --- |
| `PROBABLE_UNADJUSTED_CORPORATE_ACTION` | The ratio is within 3% of a split, bonus or consolidation factor, and the median price over the next 20 sessions stays at that factor relative to the previous 20 |
| `POSSIBLE_UNADJUSTED_CORPORATE_ACTION` | The ratio matches, but the price level does not persist |
| `UNEXPLAINED_LARGE_MOVE` | No factor matches |

These are flags for review, not corporate-action evidence. The M3.6 catalog remains the
authority.
