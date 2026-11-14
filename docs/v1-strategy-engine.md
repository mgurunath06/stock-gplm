# V1 declarative strategy engine and Strategy Studio

V1 strategies are data, not Python modules. The package under `stock_gplm.v1`
validates, hashes, evaluates and versions a strict configuration document. The
engine is local and research-only. It has no broker client, credential access,
order-routing endpoint, remote synchronization, scheduler or Oracle integration.
Activating a strategy only selects one immutable configuration in a local SQLite
registry.

This is an additive V1 foundation. It does not replace or relax M1 through M3.9-B
data, evidence, locked-holdout, trial-budget or training gates.

## Configuration contract

`schema_version: 1` requires these sections:

| Section | Contract |
| --- | --- |
| `strategy` | ID, name, description, enabled state and `1d` timeframe |
| `universe` | NSE `EQ` only; ETFs are rejected; optional explicit symbols |
| `indicators` | Unique, typed `SMA`, `SMA_VOLUME`, `EMA`, `HIGHEST`, `LOWEST` or `PERCENT_CHANGE` definitions |
| `entry` | Typed condition tree |
| `exit` | Typed condition tree and maximum holding sessions |
| `risk` | Stop-loss and take-profit calculation |
| `position_sizing` | Fixed portfolio fraction definition |
| `trading_costs` | Named profile with commission and slippage basis points |

Conditions use `AND`, `OR`, `NOT`, `GT`, `GTE`, `LT`, `LTE`, `EQ`,
`CROSS_ABOVE` and `CROSS_BELOW`. Numeric operands can use `ADD`, `SUBTRACT`,
`MULTIPLY`, `DIVIDE`, `HIGHEST`, `LOWEST` and `PERCENT_CHANGE`. Operands refer
only to allowlisted OHLCV fields, declared indicators, finite constants and
non-positive session offsets. Positive offsets fail validation as future data.
Tree depth, node count, lookback, document size and holding periods are bounded.

The safe YAML codec supports the data-only subset emitted by the Studio. YAML
tags, object constructors, anchors, aliases, merge keys and block scalars are
rejected. JSON duplicate keys and non-finite values are rejected. The engine
never uses `eval`, `exec`, pickle or user-supplied Python expressions.

The default, fuller swing-breakout example is
[`config/v1/strategies/swing-breakout-v1.json`](../config/v1/strategies/swing-breakout-v1.json).
It includes long-term regime, base-location, base-change, breakout and volume
conditions; it is more complete than the abbreviated amendment example, but it
remains an illustrative research strategy rather than a performance claim. The
genuinely different mean-reversion demonstration is
[`config/v1/strategies/mean-reversion-v1.json`](../config/v1/strategies/mean-reversion-v1.json).
Both pass through the same validator, indicator calculator, expression evaluator
and backtester without strategy-specific Python logic.

## Causality and backtesting

Inputs must be unique, chronological, completed daily OHLCV sessions. Indicators
at index `t` read at most `t`; offsets can only read `t` or earlier. Rolling
windows are trailing windows. A signal calculated from the completed close at
`t` can enter no earlier than the open at `t+1`.

The single-instrument V1 backtester is deliberately small and conservative. Its
execution policy is `completed_close_signal_next_session_open_v2` (M4 replaced
v1, which filled exit-condition and holding-period exits at the same close that
produced the decision):

- it is long-only and processes one instrument at a time;
- entry signals, exit-condition signals and the maximum-holding rule are all
  decided at a completed close and execute at the next session open;
- percent stops and targets are resting orders measured from the observed fill
  open. Rules shared with the M5 portfolio simulator live in
  `stock_gplm.v1.execution`: an open through the stop fills at that worse open
  (`STOP_LOSS_GAP`); an open through the target fills at the target, never the
  better open (`TAKE_PROFIT_GAP`); if one bar touches both, the stop fills first;
- sequential trade returns are weighted by the configured fixed fraction;
- configured one-way commission and slippage are applied on entry and exit;
- a position still open after the last candle is marked at the final close as
  `END_OF_DATA_MARK`, which is a valuation, not a fill;
- the input data hash, exact strategy version and canonical configuration hash
  are stored with the result.

Candles passed to `run_backtest` are caller-supplied, so every result carries
`data_classification: CALLER_SUPPLIED_UNVERIFIED`. The `data_quality_eligible`
flag is an acknowledgement for synthetic engineering data, not evidence. The
Studio's pasted-candle facility is for synthetic demonstrations. It does not
turn pasted data into accepted M1/M2/M3 evidence, prove historical completeness
or make data training-ready. Historical research backtests must obtain
eligibility from manifest-verified local inputs rather than a UI claim.

## Immutable registry

`StrategyRegistry` creates a local SQLite database with append-only strategy
versions. Saving a changed configuration increments its version inside a
`BEGIN IMMEDIATE` transaction, so concurrent saves cannot allocate the same
version. Database triggers abort any `UPDATE` or `DELETE` of stored strategy
versions or backtest results. Recording an identical backtest again is a no-op;
a different payload under an existing result hash is rejected. Saving the exact
same ID and hash twice is rejected. The canonical JSON and SHA-256 configuration
hash never change. Activation is stored separately, so selecting a newer version
does not rewrite old definitions.

Backtest rows and local paper-intent rows reference the exact `(strategy_id,
version, config_hash)`. The paper evaluator consumes the same entry/exit trees:
it can record a BUY for a flat simulated state or a SELL for an open simulated
state from the latest completed session. A paper intent has the permanent status
`SIMULATED_NOT_ROUTABLE`; there is no method that can send it to a broker.

## Strategy Studio

Start the loopback-only dependency-free UI with a registry outside raw-data
directories:

```bash
.venv/bin/python -m stock_gplm.v1.studio \
  --registry /local/research/strategy-studio.sqlite3
```

Open `http://127.0.0.1:8765`. The Studio can import or edit YAML/JSON, validate,
save an immutable inactive version (the V1 draft workflow), clone, export,
activate/deactivate a local research configuration, show validation errors and
run a synthetic demonstration backtest. Definitions remain editable in the
structured document editor; the schema makes the indicator and nested rule
structure explicit. Invalid configurations are never saved or activated.

The server binds only to `127.0.0.1`/`localhost`, caps request sizes and stores no
credentials. Every request must carry a `Host` of `127.0.0.1:<port>` or
`localhost:<port>`, and any `Origin` header must match; other requests get 403.
This blocks DNS-rebinding and cross-site requests from browser pages. Registry
conflicts return 409. Do not expose the server through a public interface.

## Verification

The V1 tests cover two different configuration-only strategies, parameter-only
signal changes, nested Boolean rules, positive-offset rejection, invalid
operators and indicators, safe YAML semantic round-trips, immutable versions,
exact backtest/paper provenance, the data-quality gate and arbitrary-code
rejection:

```bash
.venv/bin/python -m pytest tests/test_v1_strategy_engine.py
.venv/bin/python -m pytest
```

These are synthetic engineering tests. They do not establish profitability,
real-data compatibility, market-data correctness, locked-holdout promotion or
live/paper broker readiness.

For a visible two-strategy demonstration, choose a new output directory outside
all raw-data trees:

```bash
.venv/bin/python -m stock_gplm.v1.demo \
  --output-dir /local/research/v1-demo-20261114
```

The command refuses to overwrite an existing directory. It creates a local
registry and `demo-report.json`. Its artificial 300-session fixture triggers the
swing breakout on one date and the mean-reversion rule on a different date; each
enters on the following session through the same evaluator and backtester. The
report is labelled `SYNTHETIC_ENGINEERING_DEMO_NOT_MARKET_EVIDENCE`.
