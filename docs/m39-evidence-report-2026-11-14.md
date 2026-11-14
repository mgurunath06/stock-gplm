# M3.9 NSE MCP live evidence — 2026-11-14

## Result

M3.9's initial proof of concept passed endpoint discovery and a small historical comparison.
This is live connectivity evidence and reference-data compatibility evidence, not permission for
production use, historical completeness, corporate-action coverage, or training readiness.

The work began from clean commit `aea2fcb5e15989e0c7dbdb0376f5876e32abbbde` on
`feature/m38-signal-discovery` and was developed on `feature/m39-nse-mcp`. The workspace had 550
tests before the change.

## Endpoint discovery

Both official endpoints initialized independently with MCP protocol `2025-06-18`, no account or
API key, and dynamic `tools/list` responses:

| Endpoint | Server identity | Initialization | Tools |
| --- | --- | --- | ---: |
| `bhavcopy` | `nse-bhavcopy-redis-mcp` 1.0.0 | PASS | 21 |
| `market` | `cm-market-mcp` 1.0.0 | PASS | 15 |

The 21 bhavcopy tools included `get_stock_history`, `get_corporate_actions`,
`nse_lookup_symbol`, `search_symbols`, index history and bounded market statistics. The 15 market
tools covered delayed stock, market-status and index queries. No delayed/live data tool was
called; that endpoint was tested through initialization and schema discovery only.

## Controlled historical comparison

The canonical comparison used 2025-02-28 through 2025-03-31. That range is entirely before the
2026-04-29 locked holdout. Each NSE request used discovered arguments `symbol`, `months=1`, and
`endDate=2025-03-31`; no pagination, bulk request, year traversal, or full-market extraction was
performed.

Each immutable Shoonya input was its accepted `2025-01-01_2025-12-31.json.gz` triplet under
`data/raw/shoonya/NSE/<SYMBOL>-EQ/`. Its filename excludes the holdout before any bytes are opened,
and the existing `ShoonyaDailyV1` publication/content validator passed. Inputs were not modified.

Default price tolerances were max(INR 0.01 absolute, 0.01% relative); volume tolerance was zero.

| Symbol | NSE rows | Shoonya rows | Aligned | Missing either side | Mismatched dates |
| --- | ---: | ---: | ---: | ---: | ---: |
| RELIANCE | 20 | 20 | 20 | 0 | 0 |
| INFY | 20 | 20 | 20 | 0 | 0 |
| TCS | 20 | 20 | 20 | 0 | 0 |

This says only that the permitted observations aligned within those tolerances for that small
window. It does not establish historical completeness, correctness outside the sample, an
adjusted return series, or a reliable model input.

## Corporate actions

`get_corporate_actions` was discovered with required `symbol`, `fromDate`, and `toDate` fields and
was called once for the same window and each of the three symbols. All three calls returned a
temporary failure saying corporate actions could not be fetched and that this did not mean none
existed. The result is therefore `FAILED` / coverage `UNKNOWN`, not an empty action catalog.

After implementation review, one additional corporate-action-only retry was made for INFY from
2025-03-01 through 2025-03-31, with transport retries disabled. It did not repeat a price query.
The bounded report is
`reports/m39_nse_mcp/20261114T050456-6e74b23fdba9/` and records:

- HTTP status `200`, content type `text/event-stream;charset=UTF-8`;
- JSON-RPC error `null` and MCP `isError=false`;
- no protocol or application error code, only the application payload text saying corporate
  actions could not be fetched and to try again;
- exact discovered schema: required string fields `symbol`, `fromDate`, `toDate`, with additional
  properties forbidden;
- classification `TRANSIENT_SERVER_DATA_FETCH_FAILURE`, inferred from the server's “try again”
  wording, with coverage still `UNKNOWN`.

This is not a protocol, authentication, explicit permission, or licence error. It is an
application-level data-fetch failure returned inside a successful MCP response. The underlying
server cause is not exposed, so “transient” remains an evidence-labelled inference rather than a
confirmed root cause. M3.9-B remains blocked.

## Retention and limits

The ignored local `reports/m39_nse_mcp/` tree contains non-overwriting Markdown and JSON reports,
including tool schemas and comparison summaries. Raw MCP payloads were not retained. No NSE data
entered M3.1/M3.2 research datasets, model features, labels, training, ranking, backtesting, or the
trial ledger. No locked-holdout segment was opened or scored.

Recommendation: keep M3.9 discovery-only and reference-only. Do not widen instrument/date scope or
connect it downstream until NSE licensing, retention, redistribution, derived-output and trading
use have been reviewed explicitly.

Final verification: `.venv/bin/python -m pytest` passed all 565 tests in 32.86 seconds, including
15 synthetic M3.9 tests. No automated test depends on live NSE connectivity.
