# Research recovery, 20 November 2026

Owner requested the entire cash-equity universe and GPU use. The feeder queues
the watchlist first, then all symbols from deploy/oracle/live/universe.txt.
Stocks wait for the configured local minute publication window to be present.
This completeness check alone does not establish provider-content quality.

Fixed strategy-library parsing of 4h feature prefixes already emitted by the
feature engine. Existing completed research lanes remain resumable.
The runtime feeder now explicitly requires CUDA for intraday screening, offers
local LLM discovery to all queued stocks, and leaves unsuccessful batches pending.
CPU evolution, sklearn tree fitting and NumPy backtests remain CPU operations;
CUDA screening is not neural-network training. GPU neural training requires the
separate application training workflow. Runtime feeder edits require its restart.
