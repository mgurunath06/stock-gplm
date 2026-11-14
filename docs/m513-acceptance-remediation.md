# M5.1.3 acceptance remediation (2026-11-14)

Branch: `fix/m513-acceptance-remediation`, from `fix/m512-critical-remediation`
at `60ca1988a53636c5868632e97eb55e67838a0d10`.

Input: the M5.1.2 acceptance audit (NO-GO). That audit was a self-review by the
same agent that wrote M5.1.2, and this remediation was written by that agent too.
Neither is independent. Independent review is still required.

Scope: the audit's NO-GO findings only. No push, merge, deployment, real-data
research, protected-holdout scoring, trust-root installation, migration
execution or paper/live trading. M6 is not started.

**Status: NO-GO pending independent review.** Strict real-data research is
**BLOCKED**, and paper/live trading is **BLOCKED**.

## 1. Root cause to fix traceability

| Audit finding | Root cause | Fix | Evidence |
| --- | --- | --- | --- |
| H: caller controls the cutoff outside strict mode | `load_holdout_boundary` accepted any self-consistent M3.2 run, and `HoldoutBoundary` was a plain dataclass that callers could build | The trust root now names the one governed split plan: run id, exact SHA-256 and cutoff ([authority.py:144](../src/stock_gplm/m5/authority.py), [:331-342](../src/stock_gplm/m5/authority.py)). `load_holdout_boundary` needs the sealed policy and rejects any other plan, run id or cutoff ([evidence.py:138-165](../src/stock_gplm/m5/evidence.py)). `HoldoutBoundary` carries a process-local `holdout` seal ([evidence.py:113-135](../src/stock_gplm/m5/evidence.py)) | `test_H_caller_written_2099_split_plan_cannot_authorize_protected_reads`, `test_H_rewriting_the_governed_runs_plan_or_identity_is_rejected`, `test_H_trust_root_cutoff_must_equal_the_plans_cutoff` |
| H: same boundary in every mode and entry point | Diagnostic mode, discovery and caller-supplied backtests had no governance check | Adapter rejects an ungoverned boundary in every mode ([adapter.py:298](../src/stock_gplm/m5/adapter.py)). Discovery needs a governed boundary ([discovery.py:38](../src/stock_gplm/m5/discovery.py)). The CLI loads it only through the trust root ([cli.py:93](../src/stock_gplm/m5/cli.py)). Studio reaches the same adapter and evidence loader. `build_run` takes the cutoff from the trust root and refuses any dataset claiming another ([runner.py:350-357](../src/stock_gplm/m5/runner.py)). With no trust root there is no boundary, so nothing opens and no run builds | `test_H_hand_built_or_edited_boundaries_are_never_governed`, `test_H_without_any_governed_authority_nothing_opens_or_runs`, `test_H_cli_discover_uses_only_the_governed_boundary` |
| H: exact cutoff day untested (mutation H4 survived) | No test segment ended on the cutoff day | Code unchanged (`end >= boundary` was already correct); tests added | `test_H_exact_cutoff_day_boundary[...]` (cutoff−1 opened; cutoff and cutoff+1 not opened) |
| H: `build_run` guard untested (mutation H7 survived) | The guard ran after simulation and nothing reached it | The guard now runs before any signal is evaluated ([runner.py:350-357](../src/stock_gplm/m5/runner.py)). Tests isolate it from the earlier dataset-model check | `test_H_build_run_guard_refuses_before_any_evaluation[...]`, `test_H_dataset_construction_refuses_protected_bars` |
| H: alternative file-opening paths not instrumented | The spy covered `open`, `Path.open`, `os.open`, gzip and JSON, but not `io.open`, `gzip.open`, `Path.read_bytes` or `Path.read_text` | `WideSpy` adds those four paths | used in every H test above |
| H: hard-coded `protected_rows_scored_or_trained: 0` | Self-reported constant | Removed from v4 runs. `holdout_access` now holds only values `load_run` re-derives from stored decisions and the equity curve: windows, latest opened end, latest released bar, latest evaluated session. The one exception is the governed authority hashes, which are cross-checked against each decision's evidence sources and against current governance on replay ([runner.py:74-116](../src/stock_gplm/m5/runner.py), [run_store.py:268](../src/stock_gplm/m5/run_store.py)) | `test_RUN_v4_holdout_evidence_is_rederived_not_trusted[...]`, `test_RUN_v4_released_bar_at_cutoff_fails_rederivation_even_if_consistently_forged` |
| C: ancestor checks untested (mutation C5 survived) | Tests stopped the ancestor walk at the anchor directory | Each ancestor level is tested for mode, ownership and symlink. One test runs the real `/` chain above a user-owned anchor | `test_C_every_ancestor_must_not_be_group_or_other_writable[0-3]`, `..._owned_by_the_administrative_account[0-3]`, `test_C_a_symlinked_ancestor_is_rejected[1-2]`, `test_C_real_filesystem_ancestors_up_to_root_are_enforced` |
| C: link resistance and revocation | Already implemented; retained | Unchanged ([authority.py:277-282](../src/stock_gplm/m5/authority.py), [:328](../src/stock_gplm/m5/authority.py)); a test was added for a symlinked trust-root file | `test_C_symlinked_trust_root_file_is_rejected` plus the M5.1.2 tests |
| C: operational independence overclaimed | The M5.1.2 doc presented OS ownership as independent | Code-level and deployment-level enforcement are separated in §2. No independence is claimed on this host | §2 |
| Run contract changed without a version bump | `holdout_access` changed shape under the same `…-run.v3` string | `…-run.v4` / `…-run-manifest.v4` ([runner.py:47-51](../src/stock_gplm/m5/runner.py)). v2 and v3 stay integrity-readable as `PRIOR_CONTRACT_VERIFIED` ([run_store.py:76](../src/stock_gplm/m5/run_store.py), [:184](../src/stock_gplm/m5/run_store.py)). They are never replayed ([:324](../src/stock_gplm/m5/run_store.py)) or compared ([:376](../src/stock_gplm/m5/run_store.py)). Each contract keeps its own report wording, and an unknown contract has none ([runner.py:496-513](../src/stock_gplm/m5/runner.py)). v1 stays `LEGACY_UNVERIFIED` | `test_RUN_*`, `test_E_immutable_v2_run_remains_integrity_readable` |
| Migration readiness | Prose only | A testable, I/O-free plan contract and validator ([migration_spec.py](../src/stock_gplm/m5/migration_spec.py)); see `docs/m513-partition-migration-spec.md` | `tests/test_m513_migration_spec.py` |

## 2. Trust enforcement: code level vs deployment level

**Code level (what the code guarantees, and what tests verify).** No research
input (API argument, CLI flag, Studio request, evidence-paths entry or M3.2 run
directory) can create authority, approve a registration, or choose or move the
holdout cutoff. Authority comes only from:

- a trust root at a fixed path;
- owned by the configured administrative uid, with every ancestor directory
  checked and no symlinks followed;
- loaded by a process whose effective uid is not that administrative uid;
- whose sealed policy and holdout boundary only this process can issue.

Tests exercise this with the test user standing in for the administrator. One
test checks the real `/` ancestor chain, and one checks rejection of a
user-owned anchor when uid 0 is required.

**Deployment level (not established on this host).** The guarantee only means
something if the research account cannot act as the administrative account. On
this host it can:

- the user is in the `sudo` group;
- the kernel is WSL, where `wsl.exe -u root` normally gives root without a
  password;
- the research account owns the source tree and could edit the fixed anchor.

So there is **no operational independence here**. Installing a trust root on
this host would be a procedural control only. No trust root has been installed
or fabricated.

**Operating on a properly isolated host (requirements, not yet met).**

1. Research runs under a dedicated account with no `sudo`, no WSL/hypervisor
   root path, no membership in groups that own `/etc/stock-gplm`, and no use of
   `newuidmap`/`newgidmap` (no `/etc/subuid` range).
2. That account cannot write the source tree it executes. Install code
   root-owned, or run it from a read-only image, so `_ANCHOR` cannot be edited.
   Accepted code identities are recorded and compared with each run's
   `code.source_sha256`.
3. A separate administrator account owns `/etc/stock-gplm` and runs no research.
4. Reviewer approval: a named reviewer signs off on an approval record outside
   the research account, by a change-controlled process such as a signed commit
   to an administrator-owned repository. The administrator installs a new
   policy and trust-root generation that list the approval, the reviewer
   identity and the governed M3.2 split plan. Reviewer identities in the policy
   are labels, not signatures. If cryptographic reviewer authentication is
   needed, a later milestone should add pinned reviewer public keys to the
   trust root.
5. Rotation and compromise recovery follow `docs/m512-critical-remediation.md`.
   A new governed split plan means a new trust-root generation. All earlier
   assessments then go stale and earlier runs no longer replay
   (`test_RUN_governed_holdout_rotation_invalidates_replay`).

Until such a host exists and an independent party verifies it, strict research
stays **BLOCKED**.

## 3. Behaviour changes

- **No trust root installed (every host today):** evidence loads without a
  holdout boundary. Every assessment, diagnostic included, is `REJECTED` with
  `HOLDOUT_BOUNDARY_UNVERIFIED` before any segment is considered. Discovery
  verification and backtests refuse to run. This is deliberately stricter than
  M5.1.2, where diagnostic research ran on a caller-chosen cutoff.
- **A split plan that exists but isn't the governed one** is a load error, not a
  silent `None`.
- **Caller-supplied diagnostic datasets** still run (non-investable), but only
  against the governed cutoff.
- **Discovery without `--verify`** inspects only names and sidecar completeness,
  so it needs no boundary.
- **New runs use contract v4.** M5.1.1/M5.1.2 v2/v3 runs read back as
  `PRIOR_CONTRACT_VERIFIED`; re-run them to make them comparable.

## 4. Tests

| Suite | Result |
| --- | --- |
| `tests/test_m513_remediation.py` | 45 passed |
| `tests/test_m513_migration_spec.py` | 24 passed |
| Full regression (`PYTHONPATH=src …/.venv/bin/python -m pytest -ra -p no:cacheprovider`) | **870 passed, 2 skipped, 0 failed**, CPython 3.12.16, pytest 8.4.2 |

**Genuine-data tests skipped:**

- `tests/test_m22_master_validation.py:95`: no local real Shoonya NSE
  instrument master.
- `tests/test_m2_quality.py:272`: no local accepted RELIANCE segment.

They are not replaced by synthetic substitutes, and no real-data acceptance is
claimed.

**Mutation testing.** Each control was disabled in turn on a scratch copy, and
28/28 mutations were killed, including the three M5.1.2 survivors (H4
exact-cutoff, H7 `build_run` guard, C5 ancestor walk). Two corrections along the
way:

- the first S3 mutation produced a syntax error, which is not a valid kill, so
  it was rewritten as a syntactically valid mutation;
- V5 (the released-bar re-check) first survived, so
  `test_RUN_v4_released_bar_at_cutoff_fails_rederivation_even_if_consistently_forged`
  was added to kill it.

**Protected data and reports untouched.** Read-only checks:

- the four real legacy runs in the main checkout's `reports/m5/runs` load as
  `LEGACY_UNVERIFIED`, and the `reports/m5` tree was byte-identical before and
  after;
- no raw data, holdout dataset, other worktree or `/etc` path was written.

## 5. Remaining limitations

- No operational independence on this host (§2). Code-level enforcement is
  tested with synthetic ownership.
- No governed split plan, approval or trust root exists. Every real M5 request
  is `REJECTED` until an administrator installs a trust root on an isolated host.
- Same-interpreter code can still patch seals or the anchor.
- Publisher mislabeling inside a declared pre-cutoff window is detected, not
  prevented (unchanged from M5.1.2).
- The pre-M5.1.2 real `discover --verify` report
  (`reports/m5/discovery-verified-2026-11-14.json`) already decoded protected
  real segments. That historical exposure cannot be undone. It produced
  disposition counts only.
- The calendar-year 2025 segment still blocks real requests overlapping 2025
  until the specified migration is authorized and executed.

## 6. Decisions

- **M5.1.3 code acceptance: NO-GO pending independent review.** The code-level
  findings are addressed and tested, but no independent party has reviewed them.
- **Strict real-data research: BLOCKED.** It needs governed authority on an
  isolated host, genuine approvals and completed migration evidence.
- **Paper/live trading: BLOCKED.** Out of scope; no broker route exists.
