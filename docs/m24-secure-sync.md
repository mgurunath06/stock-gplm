# M2.4 secure, resumable Oracle-to-local synchronization

M2.4 is a pull-only synchronization controller. It reads Oracle publications
through the fixed Windows OpenSSH alias `sne-cloud`, stages files outside the raw
tree, verifies their publication contract, and publishes new local files without
overwriting existing bytes. It does not call Shoonya, alter Oracle, fill candles,
or establish historical membership.

No Oracle connection or real transfer was used during implementation. The
transport was validated locally with `ssh.exe -G`, which expands configuration
without connecting.

## Transport validation

The current WSL host has Windows OpenSSH 9.5, `ssh.exe`, `sftp.exe`, SFTP batch
and `reget` support, WSL executable interop, and `wslpath`. The `sne-cloud` alias
resolves locally. Its base profile is interactive (`BatchMode no`, strict host
key behavior `ask`), so M2.4 overrides and verifies these effective settings on
every preflight:

- `BatchMode=yes`;
- `StrictHostKeyChecking=yes`;
- `ClearAllForwardings=yes`;
- agent, X11, port forwarding and TTY disabled;
- bounded connection and keepalive timeouts.

The application never reads or logs private keys. The transport configuration
must name `sne-cloud`; another host is rejected.

## Pinned scope

Production requests must bind all of the following:

- `NSE_symbols.txt.zip`;
- SHA-256 `129b0eb840d854bceb181da6f0dca67fc5f25a355f6f1325677b95f005f4aee9`;
- 2,715 EQ symbols;
- the user-supplied Oracle baseline of 1,584 complete and 1,131 partial symbols;
- exactly five named, non-overlapping collector windows;
- an optional `daily_sync_start`; when supplied, marker-committed one-day segments
  on or after that boundary are eligible without installing a schedule;
- explicit transfer, disk, retry and concurrency limits.

The repository does not contain the five collector-window dates. A production
request cannot be created until those ten boundaries are supplied. Tests use five
synthetic one-day windows and do not claim collector compatibility.

## Request and transport contracts

`stock-gplm.m24-sync-request.v1` contains the pinned master, destination, baseline,
five windows and limits. Production loading rejects another master or baseline.

The separate transport configuration contains exactly:

```json
{
  "schema_version": 1,
  "profile": "windows-openssh-sne-cloud",
  "host_alias": "sne-cloud",
  "remote_root": "/approved/oracle/publication/root",
  "ssh_executable": "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
  "sftp_executable": "/mnt/c/Windows/System32/OpenSSH/sftp.exe"
}
```

The remote root shown above is illustrative. The approved real path remains an
open input and must not be inferred.

## CLI

```bash
.venv/bin/python -m stock_gplm.m24.cli preflight \
  --request /local/m24-request.json \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --transport-config /local/m24-transport.json \
  --output reports/m24/preflight.json

.venv/bin/python -m stock_gplm.m24.cli discover \
  --request /local/m24-request.json \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --transport-config /local/m24-transport.json \
  --output-root reports/m24

.venv/bin/python -m stock_gplm.m24.cli plan \
  --request /local/m24-request.json \
  --catalog reports/m24/catalog-<id> \
  --output-root reports/m24

.venv/bin/python -m stock_gplm.m24.cli pull \
  --request /local/m24-request.json \
  --plan reports/m24/plan-<id> \
  --transport-config /local/m24-transport.json \
  --checkpoint-root /local/m24-checkpoints \
  --audit-root reports/m24 \
  --execute-plan <id>

.venv/bin/python -m stock_gplm.m24.cli resume \
  --request /local/m24-request.json \
  --run-id <id> \
  --transport-config /local/m24-transport.json \
  --checkpoint-root /local/m24-checkpoints \
  --audit-root reports/m24

.venv/bin/python -m stock_gplm.m24.cli validate \
  --request /local/m24-request.json \
  --run reports/m24/run-<id> \
  --output-root reports/m24

.venv/bin/python -m stock_gplm.m24.cli reconcile \
  --request /local/m24-request.json \
  --master data/reference/source/shoonya/NSE_symbols.txt.zip \
  --catalog reports/m24/catalog-<id> \
  --run reports/m24/run-<id> \
  --validation reports/m24/validation-<id> \
  --m23-summary reports/m23/<id>/inventory-summary.json \
  --output-root reports/m24
```

`pull` requires the exact checksum-derived plan ID. There is no unrestricted
"sync all" operation. No scheduler is installed; the same discover/plan/pull
sequence can later be invoked for daily windows by an independently approved
scheduler.

## Discovery and transfer

Discovery lists only `.json.gz.done` markers beneath master symbols. A source is
eligible only when the marker is `COMPLETE`, names the matching payload, provides
bounded SHA-256/byte/record claims, has a metadata sidecar, and matches one of the
five collector windows. Discovery downloads no candle payload.

The plan compares all three destination files:

- `TRANSFER`: no destination files exist;
- `RECOVER_PUBLICATION`: a matching subset exists after a crash;
- `SKIP_IDENTICAL`: all three immutable files match;
- `DESTINATION_CONFLICT`: an existing file differs.

Payload downloads resume from the durable checkpoint length. The full payload is
then rehashed and compared with the marker. Metadata and marker hashes must still
match discovery and are fetched again after the payload to detect source changes.
Only transient transfer errors retry; integrity, security, path, source-change,
disk and destination failures stop immediately.

## Crash-safe publication

M2.4 holds an advisory exclusive lock at `.stock-gplm-m24.lock`. The lock file is
a persistent coordination inode and is not deleted after a run; activity is
determined only by its kernel lock. The kernel releases that lock when a process
dies. M2.3 refuses an active lock and rechecks before finalizing its inventory.

Each staged file is copied to a hidden file in the destination directory, flushed,
made read-only and hard-linked exclusively to its final name. Existing matching
files are retained; differing files are never replaced. Publication order is:

1. compressed payload;
2. metadata sidecar;
3. `.done` marker.

The marker is therefore the commit point. Recovery verifies and completes a
matching subset. Hidden files left by a crash are reused only when their checksum
matches the plan.

## Reports and classifications

M2.4 writes checksum-bound catalogs, plans, execution audits, validation reports
and 2,715-by-five reconciliation JSONL. Reports and checkpoints are forbidden
inside the raw destination.

| Classification | Evidence boundary |
| --- | --- |
| Source | Marker and sidecars exist, agree and remain stable |
| Transfer | Exact bytes and SHA-256 arrived and were exclusively published |
| Publication | Existing M1 publication-integrity validator |
| Content | Existing `ShoonyaDailyV1` validator; independent quarantine |
| Historical | Imported only from an explicit M2.3 summary or `NOT_EVALUATED` |
| Training | Always `NOT_READY` |

The 1,584/1,131 result remains `USER_SUPPLIED_BASELINE` until a request with the
actual five windows is discovered and reconciled.

## Resource and security boundaries

- 16 MiB compressed payload, 64 KiB sidecars and 100,000 records;
- explicit maximum triplets and total bytes;
- explicit minimum free-space reserve checked at plan and pull time;
- chunks no larger than 1 MiB;
- at most five attempts and four parallel transfers; implementation defaults to
  sequential transfer;
- catalog row and line limits;
- strict symbol/window filenames, no absolute paths, traversal, control
  characters or symlinks;
- no shell evaluation of provider text;
- checkpoints, reports and staging outside raw data;
- source and final destination bytes never edited or overwritten.

## Remaining operational inputs

- Exact five collector-window names and date bounds.
- Approved Oracle publication root for the transport configuration.
- Approved maximum triplets, total bytes and disk reserve for a real run.
- Confirmation that Windows SFTP can access the chosen WSL checkpoint path through
  its translated UNC path.
- A future decision on scheduler, cadence, audit retention and checkpoint pruning.
