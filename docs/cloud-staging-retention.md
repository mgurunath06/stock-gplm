# Cloud staging retention

Owner directive, 20 November 2026: verified delivery to PC permits removal of
the matching Oracle staging triplet. No separate backup is a prerequisite.

The cloud SQLite ledger `~/gplm-live/transfer-ledger.sqlite` retains each relative
path and all three file hashes/sizes, PC destination, and timestamped events.
The PC also retains `var/runs/cloud-transfer-audit.jsonl`. Keep both ledgers.
Publication validation checks marker status, payload size/hash and JSON metadata;
it establishes transfer integrity, not market-data correctness or authenticity.

Events: CLOUD_DOWNLOAD_STARTED, CLOUD_DOWNLOAD_COMPLETED (or FAILED),
CLOUD_PUBLICATION_VERIFIED, PC_TRANSFER_STARTED, PC_TRANSFER_COMPLETED, PC_VERIFIED, DELETE_INTENT,
CLOUD_DELETED. Historical download timestamps remain unknown. An existing PC
copy is rehashed before acknowledgement; its original transfer time is unknown.

The retirement worker covers published minute and daily NSE equity triplets.
It reads each local component back and compares it with the cloud manifest.
It flushes its PC receipt before sending acknowledgement. Oracle independently
rehashes existing components, durably commits the receipt before deletion,
and keeps receipts as logical coverage for its downloader. Partial deletion
after interruption is resumable. Conflicting or corrupt files remain on cloud.
No raw file is overwritten. Live stream files are outside this policy.

The cloud filler retries each missing `(symbol, session)` independently with
backoff. A failure never blocks later sessions for that stock. After five
failed attempts the exact item is recorded as `UNAVAILABLE`, including the
last error and attempt timestamp, and is no longer requested automatically.
`universe_progress.json` reports `downloaded`, `unavailable`, and `pending`
totals separately; `resolved` means downloaded or explicitly unavailable and
does not relabel a missing provider file as downloaded. A manual reset may
retry an unavailable item later.

Run the PC worker with `python -m stock_gplm.app.cloud_retention` (PYTHONPATH=src).
Deploy transfer_ledger.py alongside cloud_sync.py and restart the filler before
starting retirement; the old filler does not recognize archived receipts.
