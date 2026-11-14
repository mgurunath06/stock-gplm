# Partition migration specification (M5.1.3), not executed

Contract: `stock-gplm.m513-partition-migration-plan.v1`, validated by
`stock_gplm.m5.migration_spec.validate_migration_plan`. The validator reads no
data. It checks a plan document against the governed holdout. Tests:
`tests/test_m513_migration_spec.py`.

## Why

Real Shoonya daily segments are calendar-year files. M5 rejects any segment
whose declared window reaches the governed cutoff, without opening it. The
calendar-year segment containing the cutoff (2025, for the real cutoff
2025-10-21) blocks every real request overlapping that year. The fix is a new,
separately published raw-root version split at the cutoff. Nothing is repaired
in place.

## Plan fields and rules

| Field | Rule |
| --- | --- |
| `governed_holdout` | Must equal the trust root's governed holdout (run id, split-plan SHA-256, cutoff). The cutoff comes only from administrative governance. |
| `sources[]` | Each existing segment: `symbol`, `file` (= `START_END.json.gz`), `window`, and the SHA-256 of payload, `.done` and `.meta.json`. No duplicates. |
| `source_root` | Absolute path plus `inventory_sha256`, the canonical hash of the sorted source list. It is the lineage anchor and is never written. |
| `target_root` | A new absolute version directory: not equal to, inside, or containing the source root. |
| `targets[]` | Exactly `plan_partitions(sources, cutoff)`, in a deterministic order independent of input order. |
| `publication` | `{"method": "STAGED_VERSION_ATOMIC_RENAME", "writes_source_root": false}`. |
| `validation` | The required post-publication steps, listed exactly and in order (below). |
| `rollback` | `{"method": "KEEP_SOURCE_ROOT_AND_REPOINT_READERS", "source_root_retained": true}`. |
| `protected_decoding` | Either `RECOLLECT_CUTOFF_ALIGNED_WINDOWS` with `decodes_protected_rows: false`, or `PUBLISHER_SPLIT` with `decodes_protected_rows: true` plus an explicit authorization (below). |
| `plan_id` | Canonical hash of every other field. |

## Deterministic partitioning rules

For governed cutoff `C`:

- a window ending before `C` becomes a `DEVELOPMENT` byte copy;
- a window starting on or after `C` becomes a `PROTECTED` byte copy;
- any other window, including one ending exactly on `C`, splits into
  `[start, C−1]` `DEVELOPMENT` and `[C, end]` `PROTECTED`, derivation
  `SPLIT_AT_CUTOFF`.

Every target records the identity of its source segment.

## Protected-data decoding authorization

- **`RECOLLECT_CUTOFF_ALIGNED_WINDOWS` (preferred):** the publisher re-collects
  from the provider with windows ending at `C−1` and starting at `C`. No
  existing protected bytes are decoded. Re-collected content can differ from the
  original, so lineage records the new hashes and the original remains the
  record of what was first published.
- **`PUBLISHER_SPLIT`:** the publisher decodes each straddling source once,
  inside the ingestion boundary, writes the two sides, and keeps or analyses
  nothing else. It is valid only with `authorization = {authorized_by,
  authorization_record_sha256, authorized_at_utc, scope:
  "SPLIT_ONLY_NO_RETENTION_OR_ANALYSIS_OF_DECODED_PROTECTED_ROWS"}`. The
  authorization record itself is a document a person signs outside the research
  account. M5 never performs this split.

## Publication, validation and rollback procedure (for the future operator)

1. Re-hash every source segment and compare it with `sources[]`. Abort on any
   difference.
2. Build the whole target version in a staging directory beside the target and
   write each target's payload, `.meta.json` (`from`/`to` = target window) and
   `.done`.
3. Make the version visible with one atomic `rename` of the staging directory to
   `target_root`. Never write into `source_root`.
4. Required validation, in order:
   1. `VERIFY_SOURCE_HASHES_UNCHANGED_BEFORE_AND_AFTER`
   2. `VERIFY_TARGET_SIDECAR_WINDOWS_EQUAL_FILENAMES`
   3. `VERIFY_DEVELOPMENT_TARGETS_END_BEFORE_CUTOFF`
   4. `VERIFY_DEVELOPMENT_ROWS_WITHIN_DECLARED_WINDOWS_WITH_M5_VALIDATOR`
   5. `VERIFY_PROTECTED_TARGETS_NOT_OPENED_BY_M5`
   6. `RECORD_TARGET_PAYLOAD_DONE_META_SHA256`
5. Rollback: the source root is untouched, so readers are repointed to it and
   the target version is set aside. Nothing needs restoring.

## Status

Specification and validator only. No plan has been written for the real data, no
authorization exists, and nothing was migrated.
