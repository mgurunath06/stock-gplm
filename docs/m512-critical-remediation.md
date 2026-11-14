# M5.1.2 critical remediation — findings C and H (2026-11-14)

Branch: `fix/m512-critical-remediation`, baseline
`fix/m511-strict-authorization` at `51a1c1ea26d54ed2ed5575f0cfa8d50f3d0078a9`.

Scope: findings C (independent approval authority) and H (holdout isolation)
only, with their tests and documentation. No production, paper or live trading,
no strict real-data research, no protected-holdout scoring or inspection, no
raw-data or prior-result changes, no push, merge or deployment.

**Status: NO-GO** until an independent acceptance review confirms that both
defects are closed.

> **Superseded in part by M5.1.3** (`docs/m513-acceptance-remediation.md`). The
> M5.1.2 acceptance audit found the holdout cutoff was still caller-controlled
> outside strict mode, three controls were untested, the run contract changed
> without a version bump, and OS ownership was overstated as operational
> independence. On this WSL/sudo host the trust root is a procedural control only.
> The migration below is replaced by `docs/m513-partition-migration-spec.md`.

## H — holdout isolation

### Root cause

The M5.1.1 validator `holdout_validation.validate_daily_for_development`
decompressed and JSON-parsed the whole straddling payload, decoded every row's
structure and read its `time` field, and only then decided whether the row was
before the cutoff. Protected rows therefore went through gzip, JSON parsing and
`ShoonyaDailyV1.decode_structure`. Rows that failed to decode were silently
skipped (`continue`) on the theory that they *might* be protected, so malformed
development rows could disappear without an integrity failure. The run then
recorded `protected_rows_decoded_during_source_validation: 0`, a self-reported
counter that the code did not support.

The defect is structural. A Shoonya daily segment is one gzip-compressed JSON
array, so a record's date cannot be known without decoding the record. Isolation
cannot be built on dates read from inside a protected payload.

### Remediation

Partitioning now uses only metadata that is checked **before** a payload is
opened (`m5/holdout_validation.py`):

1. The immutable filename window `START_END.json.gz` must parse.
2. The `.meta.json` sidecar must exist, and its `from`, `to` and `symbol` must
   equal the filename window and directory exactly. A missing or inconsistent
   value fails closed as `partition_metadata` → `METADATA_MISMATCH`.
3. A segment is opened only when that verified window ends strictly before the
   cutoff. Any other segment, straddling or protected, fails as
   `holdout_partition_required`, and no payload byte is read.
4. Opened segments run through the unchanged M1/M2 `validate_daily`. A malformed
   row or a row dated outside the declared window quarantines the whole segment.
   The adapter's `SEGMENT_QUARANTINED` failure lists every row-level issue with
   its index. Nothing is skipped.

Other paths:

- `discover --verify` previously ran `validate_daily` on every segment,
  including protected ones. It now requires a hash-verified M3.2 run
  (`--m32-run`) and reports `NOT_OPENED_LOCKED_HOLDOUT` for protected segments.
- The run's `holdout_access` no longer carries decode counters. It records
  `partition_policy` and `opened_segment_windows`. `run_store.load_run`
  re-derives that list from the stored eligibility decisions and rejects a run
  whose list differs or reaches the cutoff. `build_run` refuses to build such a
  run at all.

### Why this design

The compressed format cannot support row-level isolation, so the prompt's
fail-closed option applies: the unit of isolation is the physical file. The
filename and the sidecar are written separately from the payload, and both must
agree before opening. Content inside an opened file is then checked against the
declared window, so a mislabeled file is detected rather than trusted.

### Compatible migration (proposed, not executed)

The real Shoonya raw tree uses calendar-year segments, e.g.
`2025-01-01_2025-12-31`. That segment straddles the real cutoff 2025-10-21, so
M5 now rejects any request overlapping 2025 until the data is republished. The
proposed migration keeps every existing immutable file:

1. The M1 publisher (not M5, and not a research process) writes a **new** raw
   root version with segments split at the cutoff, e.g.
   `2025-01-01_2025-10-20` and `2025-10-21_2025-12-31`. Each segment gets its own
   `.meta.json` (`from`/`to`) and `.done` manifest. The split is produced either
   by re-collecting with cutoff-aligned windows or by a publisher-side split
   whose protected output goes only into the protected segment.
2. The old root stays untouched as prior acceptance evidence.
3. M5 is pointed at the new root. Protected segments are never opened by M5.

The publisher-side split necessarily decodes protected rows once, inside the
ingestion boundary. Re-collecting with cutoff-aligned windows avoids even that.

### Residual risk (H)

- Isolation is guaranteed for payloads that validated metadata declares
  protected. If a publisher puts a protected-date row inside a pre-cutoff window,
  that row is decoded while the inconsistency is detected and the segment is
  rejected. It is never released.
- Filename and sidecar metadata come from the same publisher. They are checked
  for agreement but are not independently attested.
- Code inside the interpreter can bypass any of this (unchanged from M5.1.1).

## C — independent approval authority

### Root cause

M5.1.1 accepted `trust_policy_path` and `trust_policy_sha256` from the same
caller (`load_reference_evidence`, CLI `--trust-policy`/`--trust-policy-sha256`,
Studio). The "out-of-band" hash was supplied by the party it was meant to
constrain, so a research caller could write a PRODUCTION registration, a
matching policy and its checksum, and approve itself. The approving
`ApprovalTrustPolicy` was also a plain dataclass that any caller could
construct.

### Remediation

- Every caller-facing policy/checksum input is removed (`evidence.py`, `cli.py`,
  `adapter.py` revalidation, `v1/studio.py`). `load_trust_policy()` takes no
  arguments.
- The only authority is the administrative trust root
  `/etc/stock-gplm/m5-approval-trust-root.json`
  (`stock-gplm.m512-approval-trust-root.v1`). Its location is a fixed constant
  (`authority._ANCHOR`), not a research input.
- The loader opens the trust root and policy with `O_NOFOLLOW` and checks the
  opened descriptor: regular file, owned by uid 0, not group/other-writable. The
  directory and every ancestor up to `/` must be uid-0-owned real directories
  that are not group/other-writable.
- It refuses to run when the research process's effective uid is the
  administrative uid. A process that can rewrite its own trust root has no
  independent authority.
- The trust root binds itself (`root_id` = canonical hash), must be `ACTIVE`,
  pins the exact policy SHA-256, names the policy file in its own directory, lists
  the reviewers allowed to approve, and lists revoked policy hashes. A policy
  reviewer outside the root's list fails the load.
- `load_trust_policy` is the single holder of a new `trust_policy` issuance seal.
  `ApprovalTrustPolicy.approves()` returns `False` unless the seal verifies, so
  caller-constructed, edited or seal-copied policy objects never approve.
- Evidence provenance records `administrative_trust_root` and
  `approval_trust_policy` hashes. Identical reloads give identical evidence
  digests, and rotation changes them.

Absence of a trust root (the default, and the state of every host today) yields
`trust_policy = None`, so strict research stays `BLOCKED`.

### Why this design

The separately governed party is the OS administrative account, and the
research account cannot write uid-0-owned, non-writable files. Using it needs no
new dependency, no private key and no secret in the repository. Signature-based
reviewer keys would add a cryptographic dependency, and reviewer keys would still
need a governed place to be pinned. That pinning is exactly what this trust root
provides. Reviewer signatures can be layered on later by adding reviewer public
keys to the trust root.

### Trust-root initialization (administrator, not the research account)

```bash
sudo install -d -o root -g root -m 0755 /etc/stock-gplm
sudo install -o root -g root -m 0644 approval-trust-policy.json /etc/stock-gplm/
sha256sum /etc/stock-gplm/approval-trust-policy.json   # -> policy_sha256
```

Write `m5-approval-trust-root.json` with exactly these fields:
`contract`, `schema_version` (1), `generation` (1), `status` (`ACTIVE`),
`policy_file` (`approval-trust-policy.json`), `policy_sha256`,
`trusted_reviewers`, `revoked_policy_sha256` (`[]`), `initialized_at_utc`, and
`root_id`. `root_id` is the SHA-256 of the other fields serialized as JSON with
sorted keys, `(",", ":")` separators, `ensure_ascii=False`. Compute it with a
reviewed standalone snippet rather than by importing research code as root.
Install it with `sudo install -o root -g root -m 0644`. Run research as an
ordinary user. The policy's `policy_id` is defined in
`docs/m511-evidence-authority.md`.

### Rotation

1. Write the new policy under a **new file name** and install it root-owned.
2. Write a new trust root with `generation + 1`, the new `policy_file` and
   `policy_sha256`. Add the old policy hash to `revoked_policy_sha256` when it
   must never return.
3. Replace the trust root atomically: install to a temporary name in
   `/etc/stock-gplm`, then `mv`.

Effect: evidence digests change, so assessments issued under the old root fail
`revalidate()` as stale. Replay of earlier runs under the new root reports a
different run identity. Earlier runs remain integrity-verifiable with
`load_run`, but they cannot be re-ranked until they are replayed under the
current authority.

### Compromise recovery

- **Suspected policy or reviewer compromise:** set `status` to `REVOKED`
  (recompute `root_id`). Every strict load then fails closed. Then rotate as
  above, removing the reviewer from `trusted_reviewers` and listing the
  compromised policy hash as revoked.
- **Administrative (root) account compromise:** the trust root cannot defend
  against its own administrator. Rebuild the host or administrative account,
  re-initialize the trust root from reviewed sources at a new generation,
  re-review every approval, and treat runs made since the suspected compromise as
  unverified until they are re-run under the new root.
- **Research account compromise:** the research account cannot alter the trust
  root, but it could alter code, evidence or raw data in its own workspace. That
  remains outside this local boundary, as in M5.1.1.

### Residual risk (C)

- The anchor is OS file ownership. Anyone with root or `sudo` is the authority.
  On a single-user workstation (including WSL) where the researcher can `sudo`,
  separation is procedural, not technical.
- There is no persistent anti-rollback state. Re-installing an older root needs
  root, and `revoked_policy_sha256` blocks known-bad policies.
- Tests exercise the loader with the test user as a stand-in administrator.
  Real uid-0 ownership is exercised only negatively: a user-owned anchor is
  rejected when uid 0 is required. No real trust root has been installed or
  tested.
- Same-interpreter code can patch `_ANCHOR` or the seal closure (the boundary
  is unchanged from M5.1.1).
- No genuine production approval exists. Strict real-data research remains
  blocked for the reasons listed in `docs/milestones/m511-completion.md`.

## Tests

`tests/test_m512_remediation.py` (synthetic only):

| Area | Tests |
| --- | --- |
| Protected content reaching a decoder | `test_H_protected_content_reaches_no_decoder_across_every_m5_path`. Instrumented `open`/`Path.open`/`os.open`, `daily._read`, gzip, `json.loads`, `daily._strict_json_loads` and `ShoonyaDailyV1` decoders track sentinel-marked protected payloads across strict and diagnostic assessment, revalidation, backtest, replay and verified discovery. `test_H_spy_detects_…` proves the instrumentation is not vacuous. |
| Straddling segments | `test_H_straddling_segment_fails_closed_without_being_opened` |
| Missing/inconsistent partition metadata | `test_H_missing_or_inconsistent_partition_metadata_is_rejected_before_open` (6 cases), `test_H_missing_partition_sidecar_and_bad_filename_fail_closed` |
| Malformed development rows | `test_H_malformed_or_unassignable_development_rows_raise_explicit_integrity_failures` (schema, value, out-of-window date; strict and diagnostic) |
| Discovery | `test_H_discovery_verification_requires_the_hash_verified_holdout` |
| Run evidence | `test_H_run_store_rederives_partition_evidence` |
| Caller-generated registrations/policies | `test_C_research_interfaces_accept_no_policy_or_checksum`, `test_C_caller_generated_production_registration_and_matching_policy_are_not_authority`, `test_C_caller_constructed_or_edited_policy_objects_never_approve` |
| Unauthorized replacement | `test_C_modified_or_replaced_policy_is_rejected`, `test_C_writable_or_symlinked_governed_files_are_rejected`, `test_C_user_owned_trust_root_is_rejected_when_root_ownership_is_required`, `test_C_research_running_as_the_administrative_account_is_refused` |
| Trust-root mismatch / revocation | `test_C_trust_root_mismatch_is_rejected`, `test_C_revoked_root_unauthorized_reviewer_and_escaping_policy_are_rejected`, `test_C_revoked_policy_hash_cannot_be_reinstated` |
| Forged / untrusted registrations | `test_C_forged_registration_content_is_rejected`, `test_C_synthetic_or_untrusted_reviewer_registrations_stay_unapproved` |
| Positive path and provenance/replay | `test_C_governed_trust_root_is_the_only_path_to_approval`, `test_C_replay_and_provenance_follow_the_governed_authority` |

`tests/conftest.py` points the anchor at an empty directory for every test, so a
real `/etc/stock-gplm` on a test host never influences results.

Mutation check: disabling each new control in turn (opening reaching-cutoff
segments, skipping the metadata pre-check, decoding in discovery, ignoring the
policy seal, allowing the administrative euid, skipping ownership, skipping the
policy pin) makes at least one of these tests fail.

## Completion matrix

| Finding | Status | Evidence | Residual |
| --- | --- | --- | --- |
| C independent approval authority | **IMPLEMENTED — awaiting independent acceptance** | Caller inputs removed; OS-governed, hash-bound trust root; sealed policy; 19 C tests | Root/sudo is the authority; no real trust root installed |
| H holdout isolation | **IMPLEMENTED — awaiting independent acceptance** | Metadata-verified pre-open partitioning; unopened rejection; explicit row failures; decoder-instrumented tests; re-derived run evidence | Real calendar-year segments need republishing; publisher mislabeling is detected, not prevented |
| A, B, D, E, F, G (M5.1.1) | Retained | Full regression suite passes | Unchanged |

Decisions: **M5.1.2: NO-GO pending independent acceptance.** Strict real-data
research: **BLOCKED**. Paper/live: **BLOCKED**.
