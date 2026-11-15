# M5.5 native Windows compatibility repair (2026-11-15)

The application uses the standard-library `stock_gplm.locking` adapter: POSIX
`flock` on Linux, synchronous `LockFileEx`/`UnlockFileEx` on Windows. Platform
imports are conditional. Windows locks a byte beyond the owner metadata so
read-only publication probes and owner diagnostics do not conflict with mandatory
byte-range locking. No extra dependency, PyTorch or CUDA installation is needed.

Direct fcntl imports were found in M2 inventory, M2.4 sync, M2.5 security,
M3 readiness/minute publication, and M5 historical adapter. All now use the same
adapter; corresponding lock tests use it too. M5.5 imports the historical modules
through its research dependencies even when it does not execute those workflows.
Older Oracle/authentication/trust-root workflows still require their documented
POSIX environment; this repair does not weaken their permissions or trust roots.

The application writer lock now retains one file identity permanently, with
kernel-held ownership rather than PID probing and unlinking a stale lock. This
removes stale-owner races (and Windows process-handle/PID hazards). Contenders
fail immediately by default, or wait up to `timeout_seconds`; process termination
releases the kernel lock. Metadata from an interrupted owner is reported through
the existing audit event after acquiring exclusive ownership. Never manually
delete an active lock file. Stop all older GPLM processes before upgrading: old
versions use a different application writer-lock protocol.

Copied strategy paths resolve by recorded strategy identity under the current
state home, with the existing SHA-256 check unchanged. Copied model checkpoints
resolve under the current models directory and must match their recorded model
hash. Missing checkpoint models fail rather than silently restarting training.
Database rows and historical checkpoint payloads are not rewritten. The shared
locking implementation is included in the application code provenance hash.

`windows/gpu-verify.cmd` no longer invokes `audit --run` with an empty argument.
It verifies both the temporary replay database and the configured application
database, and stops on discovery, replay or audit failure. The batch control flow
was tested with a synthetic launcher; the full expensive GPU discovery/replay
workflow was not rerun because CUDA was already verified by the owner.

## Executed checks

Use the existing Windows virtual environment and source tree:

```powershell
$env:PYTHONPATH="$PWD\src"
.venv\Scripts\python.exe -m pytest tests/test_windows_locking.py tests/test_app_gplm.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q --continue-on-collection-errors
windows\gplm.cmd daily --stocks RELIANCE --device auto
windows\gplm.cmd audit --verify
windows\gplm.cmd daily --stocks RELIANCE --device auto
windows\gplm.cmd audit --verify
```

Focused checks: **23 passed, 1 skipped** (the CPU-only refusal test skips when
CUDA is available). Synthetic process tests cover lock contention, bounded wait,
process death, read-only publication probes, and four concurrent writers whose
60 audit events retain a valid hash chain. Synthetic relocation tests reject
changed strategy bytes; application tests cover resume and idempotency.

The initial full-suite invocation stopped at collection. It revealed missing
`scikit-learn` and POSIX-only test assumptions. The synthetic administrator fixture
and a POSIX-only ownership test's skip condition were made Windows-safe so the
rest of the suite could execute. No production trust-root rules changed.
The continued full-suite result and remaining blockers are recorded below.

Both authoritative daily executions and both subsequent audit verifications
exited **0** on native Windows using **NVIDIA GeForce RTX 5070**, torch
**2.11.0+cu128**, CUDA **12.8**. Latest completed session: **2026-11-13**.
Each daily execution reported zero new predictions, evaluations and model fits;
SQLite counts stayed at **10,237 predictions**, **9,314 evaluations**,
**3,213 models**, **0 API calls**. The final audit verified **1,392 events** and
SQLite `quick_check=ok`. Runs and operational audit events append normally.
Market files, historical predictions/evaluations/models, research artifacts and
prior audit events were not edited or removed. Normal daily reports and backups
were produced by the authorized command.

Exact captured output is under `reports/windows-compat/` (local evidence, outside
raw directories). `execution.json` records each exact command, exit code and
before/after counts. `daily-verified-1.txt`, `daily-verified-2.txt`,
`audit-verified-1.txt`, and `audit-final.txt` are authoritative command logs;
`targeted-tests.txt` is the focused test log. Earlier exploratory PowerShell logs
include PowerShell's stderr wrappers; authoritative logs capture native CMD
stdout/stderr directly. No remote sync, credential access, broker call, data
repair, push or merge was performed.

The repository started with an empty Git index (all tracked files appeared
staged for deletion while working files were untracked). The focused commit uses
an independent index based on HEAD and leaves that pre-existing index unchanged.

Final full-suite result: **294 failed, 622 passed, 4 skipped, 11 errors**
(83.07 seconds), captured in `reports/windows-compat/full-suite-final.txt`.
Remaining blockers include missing `sklearn` (three collection errors), legacy
WSL-only destination contracts, POSIX administrative ownership/permission checks,
and Windows denial of POSIX directory-fsync operations. These affect older
M2.x/M3.x/M5 governance workflows, not the passing M5.5 daily and audit path.
No Linux runtime was available for execution in this session; the POSIX branch
continues to delegate to the existing standard-library `fcntl.flock` behavior.
The original pre-repair audit head at event 936 remains byte-identical within the
verified chain (`reports/windows-compat/audit-prefix.json`).
