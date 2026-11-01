# Architecture and research boundaries

The active workspace is `/home/mgurunath/projects/stock-gplm` in WSL2, superseding
an earlier Windows project location. Python 3.12.16 is already in `.venv`.

M1 implements `stock_gplm.ingestion` (publication verification, safe parsing,
the versioned `ShoonyaDailyV1` adapter, quality reporting) and `stock_gplm.cli`.
It has no runtime third-party dependencies. Publication integrity and OHLCV
content quality have separate statuses. Valid V1 records are normalized in memory
to ascending trading-date order; no curated dataset is written in M1. Unknown
record shapes have disposition `QUARANTINE_UNVERIFIED_SCHEMA`.

Module boundaries and current state:

| Module | Responsibility |
| --- | --- |
| instruments | M2 foundation: point-in-time symbol, ISIN/token, aliases, membership provenance |
| normalization | M2 quality decisions exist; no curated dataset writer yet |
| features | Causal features and regime/relative-strength inputs |
| labels | Horizon-specific outcomes and overlap intervals |
| training | Reproducible quantitative baselines and model comparisons |
| inference | Empirical calibration and structured predictions |
| backtesting | Chronological evaluation with costs, liquidity and slippage |
| risk | Portfolio constraints and exit conditions |
| experiments | Configuration, seeds, trial budgets and locked holdouts |
| evaluation | Benchmark-relative metrics and evidence-based reporting |

Models may eventually include boosting, temporal, neural, cross-sectional and
multi-horizon approaches; no method is presumed predictive. No LLM-generated
stock tips. Confidence must be a calibrated probability of a defined outcome.
Instrument survivorship, leakage, overlapping labels, purging and embargo,
point-in-time inputs, transaction costs and realistic holdout discipline must be
addressed before performance claims. Research artifacts stay local.

M2 now provides versioned local inventory, explicit NSE calendar entries,
corporate-action metadata, cross-segment checks, missing-date classifications,
and requested-window completeness reports. The current RELIANCE report remains
quarantined because authoritative listing membership, calendar, and corporate
action coverage have not been supplied. A future synchronization design should
stage files, verify checksums, publish atomically, and preserve source sidecars;
executing synchronization remains outside current authorization.

M2.1 adds a reference-data boundary in `stock_gplm.m2.reference_data`: supplied
payloads are verified against versioned local manifests before normalization.
`stock_gplm.m2.coverage` reports identifier, calendar, membership, alias and
corporate-action evidence per symbol and requested horizon. Importing a current
master never creates historical membership, and no importer downloads data.

M2.2 adds `stock_gplm.m2.master_validation`, a strict adapter for the observed
real Shoonya NSE master ZIP. Its source-file/schema/identifier decision is
separate from reference readiness: the real file passes structural validation,
but lacks ISIN and temporal provenance and therefore cannot establish historical
membership or complete point-in-time reference coverage.

M2.3 adds an incremental inventory over the complete M2.2 EQ reconciliation set.
It streams segment, interval, per-symbol, and exception JSONL; binds each run to
input and output checksums; refuses overwrite; and keeps publication integrity,
candle content, historical completeness, and training readiness as separate
gates. Listing history and provider outages enter only through versioned M2.1
manifest contracts.

M2.4 adds a pull-only synchronization boundary using the fixed Windows OpenSSH
`sne-cloud` profile. It discovers committed Oracle markers, creates immutable
plans, resumes bounded staging downloads, verifies source stability and payload
hashes, publishes with the completion marker last, and keeps source, transfer,
content, historical and training decisions independent. M2.3 refuses to finalize
while M2.4 holds its publication lock.

M2.5 adds an Oracle-native authentication boundary for the existing read-only
collector. WSL builds and tests the package but is not a runtime dependency.
Oracle owns protected credential input, cached OAuth sessions, headless
Selenium/TOTP renewal, audit state and proposed scheduling. The login command
requires an explicit authorization flag, and all service/timer templates remain
disabled pending deployment and scheduling approval.
