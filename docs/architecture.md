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

M3 adds a local-only, checksum-bound derived-dataset pipeline over accepted M1
publications. It produces descriptive OHLCV and causal indicators only when
lookback suffices, refuses an active M2.4 publication lock, rechecks every input
hash before publication, and never writes inside the raw tree. Current-master
identity remains separate from point-in-time membership, calendar, alias, and
corporate-action evidence; missing evidence keeps historical coverage incomplete
and training readiness `NOT_READY`.

M3.2 reads a manifest-verified M3.1 run and adds causal features, separate label
contracts, per-horizon purged walk-forward plans and quality gates. It consumes M2
reference models, never reads raw files and performs no training. See
[m32-research-framework.md](m32-research-framework.md).

M3.4 adds `stock_gplm.m3.minute`, `minute_publish`, `minute_run` and `fusion`. Minute files live
in their own raw root, and fused rows read only prior-session daily values. Publication, content,
session policy, coverage, reconciliation and leakage are separate gates, and none can make
training ready.

M3.5 adds `stock_gplm.m35`, made up of `policy`, `data`, `models`, `metrics`, `run` and
`cli`. It reads manifest-verified M3.1/M3.2 outputs and reuses the M3.2 split plans,
purge and embargo checks, and label contracts. It fits LightGBM only when every required
daily gate passes. Its numeric dependencies are confined to the `m35` extra. The separate
`stock_gplm.llm` package is opt-in and advisory, and no training or validation module
imports it.

M3.9 adds `stock_gplm.m39` as a separate NSE MCP reference boundary. It allowlists only NSE's
two published HTTPS MCP paths, defaults to tool discovery, and requires explicit opt-in for one
bounded sample over RELIANCE, INFY, or TCS before the locked holdout. Comparisons read only an
accepted Shoonya yearly segment whose filename ends before the holdout. MCP data and reports
cannot enter raw, dataset, feature, training, model, or backtest paths, and no research/training
module imports M3.9. This boundary does not validate M3.6 evidence or any M3.2/M3.5 gate.

M3.9-B adds `stock_gplm.m39b` as a dependency-free, in-memory corporate-action
calculation boundary. Frozen canonical records carry explicit availability and
licence state; exact rational factors produce separate derived candles with
complete lineage. The package has no file loader, network client, CLI or training
import. It refuses non-raw inputs, unavailable facts, conflicting sources,
duplicates, rights and complex restructurings. Synthetic verification does not
correct historical data or make it training-ready.

V1 adds the separate `stock_gplm.v1` configuration-driven research boundary.
Versioned JSON or safe data-only YAML defines indicators, nested entry/exit
conditions, risk, sizing and costs. A typed allowlisted evaluator is shared by
signal evaluation and the local backtester; there is no per-strategy Python
logic. Immutable SQLite versions, backtests and non-routable paper intents carry
the exact canonical configuration SHA-256. The loopback-only Strategy Studio can
validate, save, clone, import/export, select and synthetically backtest these
definitions. Activation selects a local research definition only and cannot
enable broker orders. Existing M1-M3 evidence and locked-holdout gates remain
authoritative and independent.

M4 committed V1 as found (`0534cc8`) and remediated it: exit decisions fill at
the next open, stops that gap fill at the worse open, registry rows are
append-only, and the Studio validates Host/Origin. M3.9-B is integrated by
ancestry. M2.5-R is not merged; its calendar registration format is consumed
read-only.

M5 adds `stock_gplm.m5`, the only path from genuine historical files to strategy
research. `HistoricalDataAdapter` reuses `validate_daily`, the hash-bound M3.2
holdout, the M2.5-R calendar format, the M2.1 reference importers, the M2.2
master and M3.9-B events. It returns hash-identified `ELIGIBLE`, `BLOCKED`,
`REJECTED` or `DIAGNOSTIC_ALLOWED` decisions. Integrity failures reject in every
mode. Missing evidence blocks `STRICT_RESEARCH`; `DIAGNOSTIC_ONLY` results are
non-investable and cannot be ranked, promoted, paper traded or used for training.
`stock_gplm.v1.portfolio` and `stock_gplm.v1.metrics` add a generic portfolio
simulator and metrics that keep signal generation separate from allocation and
execution. Runs are immutable, content-addressed directories. See
[m5-historical-data-adapter.md](m5-historical-data-adapter.md).
