# Stock GPLM project rules

Rewritten by the owner's instruction on 2026-11-20. Older milestone restrictions (locked holdout, M3.x
trial ledgers and gates, M5.2 tiers, "reference-only" NSE MCP, licence-approval gates, inferred-fact bans)
are withdrawn. Their history stays in git and the old docs.

## Basis
- **Personal, non-commercial research.** Not for sale or redistribution. No self-imposed constraint
  beyond the rules below.
- The owner decides train/test splits. Current decision (2026-11-20): no holdout; train and validate on data
  up to 2026-08-13, test on data after that, then walk-forward.

## Data
- NSE data comes from NSE's official MCP endpoints (`stock_gplm.m39.client`), never by scraping the NSE website.
  Shoonya (via Oracle) supplies minute and daily data.
- Raw provider files, metadata and publication markers are never edited. Derived data goes in separate
  folders (for example `data/derived/...`).
- Never fill, fake or silently repair candles. Record rejected records and where every dataset came from.
- Oracle is staging. After a file is verified on the PC (byte count + SHA-256), its cloud copy may be deleted.
  Keep the receipts so retired files are not downloaded again.
- Bound compressed/decompressed input; never run provider text as code.

## Research quality
- Realistic costs and slippage, chronological splits, out-of-sample tests, and recorded provenance.
- Never fabricate performance, and never claim checks that were not run.
- LLM output is a hypothesis, backtested like any other. It cannot change results, gates or risk rules.
  Keys come only from environment variables or a secret store.

## Engineering
- Python 3.12, minimal dependencies, typed code, docs in `docs/` updated alongside the code.
- **Testing:** Claude/Codex run only basic, small, targeted tests. Complete testing is written as a
  test program that the owner executes, not done by spending tokens.

## Working agreement (owner, 2026-11-15)
- Claude/Codex: develop and deliver programs. Owner: runs heavy research, training and backtesting locally.
- Strategies: configurable, extensible and AI-discoverable (declarative library, no Python per strategy).
- Priority: NSE cash-equity stocks first; F&O, options, ETFs and index-relative work deferred.
- Execution: checkpointed, resumable, no unnecessary reprocessing. Long jobs self-start; agents never poll.
- Responses: short and direct unless details are requested.
