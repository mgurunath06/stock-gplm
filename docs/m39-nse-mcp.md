# M3.9 NSE MCP reference validation

M3.9 is a read-only, reference-only integration with NSE's two official Model Context
Protocol endpoints. It discovers the current tools dynamically and can make one explicitly
enabled, bounded historical request. It is not an alternative collector and is not an input
to M3.1, M3.2, M3.5, M3.7, M3.8, a feature store, labels, a backtest or production trading.

The [live evidence report](m39-evidence-report-2026-11-14.md) records the initial result.

## Endpoints and protocol

Only these HTTPS hosts and paths are accepted:

| Name | URL | Intended use |
| --- | --- | --- |
| `bhavcopy` | `https://mcp.nseindia.in/bhavcopy/cm/mcp` | Historical end-of-day reference data |
| `market` | `https://mcp.nseindia.in/cmmkt/mcp` | Delayed cash-market reference data |

`NSEMCPClient` implements the bounded MCP Streamable HTTP subset used here:

- `initialize` and `notifications/initialized`;
- paginated `tools/list`, including descriptions and JSON input schemas;
- `tools/call` only after an explicit sample opt-in;
- session IDs and protocol-version headers;
- configured connection/read deadlines, bounded two-MiB responses, at most five configured
  retries, exponential backoff, JSON-RPC error handling, and JSON/SSE decoding.

The core project remains dependency-free. The official Python SDK was not added because this
integration needs only three wire methods and the repository requires minimal dependencies.
Offline tests inject a transport and never contact NSE.

## Usage

Discovery is the default operating mode. It initializes each endpoint and lists tools but calls
no data tool:

```bash
.venv/bin/python -m stock_gplm.m39.cli discover \
  --endpoint both --output-root reports/m39_nse_mcp
```

Review the discovered schema before constructing a sample. The current `get_stock_history`
schema uses `symbol`, `months`, and `endDate`; M3.9 requires `months` to be exactly one. The
declared interval must be at most 32 inclusive calendar days, use only `RELIANCE`, `INFY`, or
`TCS`, and end before 2026-04-29.

```bash
.venv/bin/python -m stock_gplm.m39.cli sample \
  --allow-sample-fetch --include-corporate-actions \
  --symbol INFY --start 2025-02-28 --end 2025-03-31 \
  --tool get_stock_history \
  --arguments-json '{"symbol":"INFY","months":1,"endDate":"2025-03-31"}' \
  --shoonya-segment data/raw/shoonya/NSE/INFY-EQ/2025-01-01_2025-12-31.json.gz \
  --output-root reports/m39_nse_mcp
```

A corporate-action retry does not need to repeat the historical-price call:

```bash
.venv/bin/python -m stock_gplm.m39.cli corporate-actions \
  --allow-sample-fetch --symbol INFY \
  --start 2025-03-01 --end 2025-03-31 \
  --output-root reports/m39_nse_mcp --retries 0
```

The command records the discovered input schema, exact bounded arguments, HTTP status,
content type, JSON-RPC error object, MCP `isError`, application error classification and
coverage status. It never retains the raw response. `--retries 0` guarantees one tool call.

The Shoonya segment and its publication sidecars are opened only for reading and must validate
as `ShoonyaDailyV1`. The filename must end before the locked holdout; that check happens before
the file is opened. The explicit symbol mapping is `NSE SYMBOL` to `SYMBOL-EQ`. A segment that
could contain 2026-04-29 or later is refused even when the requested interval is earlier.

## Isolation and reports

Reports are non-overwriting directories containing `summary.json` and `report.md`. The report
records execution time, environment, branch/SHA, initialization, server identity, every tool
name and input schema, sample counts, request failures, tolerances, differences and licensing
limits. Raw MCP envelopes and large output payloads are never written.

Output paths containing `raw`, `dataset`, `datasets`, `features`, `feature-store`, `training`,
`models`, `backtests`, `m3`, `m35`, `m37`, or `m38` are rejected after symlinks are resolved.
This is a second boundary in addition to the absence of all M3.9 imports from training and
research modules. Those loaders also reject the M3.9 report contract because they require exact,
manifest-bound M3.1/M3.2 contracts.

Comparison reports preserve:

- exact trading-date alignment, missing dates, and duplicates;
- per-field NSE/Shoonya OHLC differences and configurable absolute/relative tolerances;
- exact volume differences and tolerance;
- source identity, timestamps when supplied, and an explicit `UNADJUSTED` price basis;
- corporate-action flags with `UNKNOWN` adjustment status and no invented factor.

No price is adjusted and no missing observation is filled. A discrepancy is labelled an
observation, never an outage, split, bonus, error, or completeness conclusion without separate
evidence.

## Terms and unresolved questions

The official [NSE MCP page](https://www.nseindia.in/nse-mcp) describes MCP access for AI
applications, developers and research. It also says the data is for information and learning,
not real-time trading, investment advice, or commercial products. Its disclaimer says MCP access
does not grant permission to commercially exploit the data or train/fine-tune AI systems, models,
algorithms, or third-party applications with it, and remains subject to NSE's Terms of Use.

Accordingly, M3.9 is isolated from M3.6's manually registered authoritative files and from all
model data. Before any wider integration, a person must resolve at least redistribution,
retention, derived-report publication, request-volume, commercial research, and trading-use
rights with NSE. This code does not scrape, automate a browser, use NSE website cookies, call an
unofficial API, evade a rate limit, or bypass access control.

> **2026-11-20 (owner):** the reference-only restriction is withdrawn. NSE MCP is the project's NSE data source; `stock_gplm.app.nse_history` uses it for long daily history. `m39/policy.py` limits apply only to the old M3.9 CLI.

