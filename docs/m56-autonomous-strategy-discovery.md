# M5.6 autonomous strategy discovery

M5.6 treats `config/strategies/reference-300-strategies.txt` as a minimum prior library. It is not a
candidate limit. The source is parsed as six categories of exactly fifty entries and bound to a
SHA-256. A versioned JSON manifest records every reference rule, its prose and pseudo-code, inferred
data dependencies, semantic timeframes, implementation state, and reviewed declarative definition
where one exists.

GPLM never evaluates catalogue text as code. A rule is executable only after an exact translation to
the existing `stock-gplm.strategy.v3` condition schema. Descriptive, ambiguous, or currently
unsupported formulas remain `BLOCKED_IMPLEMENTATION`. Strategies needing delivery, derivatives/OI,
options-chain, index, corporate-event, institutional-flow, free-float, calendar, or deal data remain
`BLOCKED_MISSING_DATA` until a versioned registered adapter provides that input. OHLCV is never used
as a fabricated substitute.

For every selected reference, stock, and timeframe the run writes one immutable coverage row. The
possible outcomes are `EVALUATED_VALIDATION`, `BLOCKED_TIMEFRAME`, `BLOCKED_MISSING_DATA`,
`BLOCKED_INSUFFICIENT_HISTORY`, or `BLOCKED_IMPLEMENTATION`. Therefore “300 represented” never means
“300 executed.”

## Validation and discovery

Executable reference rules use the production causal candle/feature lane and pessimistic trade
simulator: a close-of-bar signal fills at the next executable open, includes the configured costs and
slippage, disallows overlapping trades, exits intraday at session close, quarantines discontinuities,
and applies purge gaps around a chronological 60/20/20 split. Validation evidence is corrected over
all tests with Benjamini-Hochberg. Minimum trade count, positive net evidence, drawdown, and fold
stability gates must all pass before the final segment is read. A `(strategy, stock, timeframe,
regime, data fingerprint)` final segment is recorded at most once.

The open-ended discovery half invokes the existing M5.4/application search on each stock and
timeframe. It explores causal feature grids, parameter neighbours, interactions, cross-timeframe
confirmations, exit grids, and locally trained models. A second local method uses gradient boosting
for nonlinear feature selection and extracts positive decision-tree paths as ordinary declarative
strategies. Those paths receive the same cost-aware validation and final-segment gates. Discovered
rules enter the same library with lineage; no Python source change is needed to store a new rule.
Evolutionary and Bayesian method slots are present but disabled until a concrete local plugin is
registered; the application does not claim that a method ran merely because it appears in
configuration. External AI remains disabled.

Individual-stock discovery is always performed. Current-run candidates are also replayed on the
other selected stocks using validation data only, producing market-wide `EXPERIMENTAL` or `REJECTED`
evidence without touching final segments. Sector/group membership is never guessed: populate the
versioned `m56.stock_groups` configuration with provenance before group research can be reported as
configured.

Evidence uses the distinct classifications `REFERENCE`, `DISCOVERED`, `EXPERIMENTAL`, `VALIDATED`,
and `REJECTED`. Reference/discovered origin is retained separately from validation status. A
leaderboard has one stock/timeframe/regime row and reports `NO VALIDATED STRATEGY` when appropriate.
Predictions and evaluations remain append-only; M5.6 does not place orders or contact a broker.

## Native Windows commands

Run these from `H:\Guru\Learning\StockGLPM\app` in PowerShell. The first command is a fast catalogue
integrity check and does not read candles:

```powershell
.\windows\gplm.cmd m56 --stocks RELIANCE --manifest-only --mode reference --device cpu
```

Bounded end-to-end research (recommended first real run):

```powershell
.\windows\gplm.cmd m56 --stocks RELIANCE,HDFCBANK,ICICIBANK --timeframes 15m,60m,1d,1w --mode both --budget quick --device auto --resume
```

All locally eligible NSE equities and all seven timeframes:

```powershell
.\windows\gplm.cmd m56 --universe all --timeframes all --mode both --budget deep --device gpu --resume
```

Reference-only or discovery-only runs:

```powershell
.\windows\gplm.cmd m56 --stocks RELIANCE --timeframes 1d,1w --mode reference --budget standard --device cpu --resume
.\windows\gplm.cmd m56 --stocks RELIANCE --timeframes 5m,15m,60m,1d --mode discovery --budget deep --device gpu --resume
```

The manifest is under `GPLM_HOME\m56`; run reports are under `GPLM_HOME\reports\m56\<run-id>`.
Feature/candle caches and M5.4 stage checkpoints are immutable and automatically reused. `--fresh`
creates a new run record but does not bypass final-segment locks. Actual GPU attribution comes from
the existing CUDA backend and GPU-evidence runner; `--device gpu` fails instead of silently falling
back when CUDA is unavailable.

Large commands run entirely in the local Python process and consume no Codex/Claude tokens. They do
not download data, modify raw files, or reinstall dependencies.
