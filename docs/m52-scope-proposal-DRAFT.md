# M5.2 controlled handover: scope and plan

> **T1/T2 APPROVED 2026-11-14. T3 NOT AUTHORIZED. T4 PROHIBITED.**
> The controlled-handover instruction authorized metadata inventory, byte-level
> integrity and marker/metadata validation. It did not authorize candle decoding,
> development-data preparation, protected-holdout operations, import execution or
> training. The dated rule in `AGENTS.md` is authoritative.

## 1. Proposed `AGENTS.md` scope rule

The following rule was approved and added to `AGENTS.md` on 2026-11-14:

```markdown
- M5.2 controlled handover (authorized by the user on 2026-11-14) has four
  separately governed tiers. T1 metadata-only inventory is authorized. T2
  byte-level integrity plus metadata and publication-marker validation is
  authorized. T1 or T2 authorization never implies T3 or T4 authorization.
  T3 governed development-data preparation is **NOT AUTHORIZED**. T4 protected
  holdout operations are **PROHIBITED**. Because the independently governed
  holdout cutoff is unavailable, M5.2 must not decompress or decode any candle
  payload. It may use read-only commands over the existing Oracle minute tree and
  collection summary solely to capture hashes and provenance; it must not rerun a
  collector or download candle files. Local and Oracle raw files, metadata and
  markers remain immutable. Reconciliation work stops at an import and quarantine
  plan unless the user separately authorizes execution. No T1/T2 result authorizes
  data relocation, derived datasets, features, labels, splits, eligibility,
  training, scoring, rankings, trading, trust-root changes or holdout access.
```

### 1a. Authorization tiers

Each tier needs its own explicit, dated approval. Finishing one tier, or passing
its gates, never authorizes the next. T1 and T2 never imply T3 or T4.

| Tier | Allowed | Not allowed | Outputs |
| --- | --- | --- | --- |
| **T1 Metadata-only inventory** | `stat`, directory listing, names, sizes, mtimes, modes, link and mount checks, file counts, path comparison | Reading any file's bytes, including hashing; decompressing; parsing | Path and size inventories, collision lists |
| **T2 Byte-level integrity and metadata/marker validation** | Reading raw bytes to hash or byte-compare; checking marker and metadata contracts | Decompressing or decoding candles while the governed cutoff is unavailable; building derived datasets, features, labels, splits or eligibility; moving or repairing raw files | Per-file hash manifests, transfer comparison, metadata/marker exceptions |
| **T3 Governed research-data preparation** | Dataset manifests, point-in-time universes, eligibility (`m35.policy`, `m5.adapter`), mapping onto `m3.splits`/`m3.fusion`, development partitions only | Model fits, new ledger trials, rankings, anything in T4 | `dataset_manifest.v1.json`, `eligibility.json`, compatibility report |
| **T4 Protected holdout operations** | Nothing under M5.2. Trust-root installation, partition migration (`m5.migration_spec`) and holdout reading or scoring each need separate approval outside M5.2 | Everything | None |

Under the approved scope, only T1/T2 portions of plan steps 1, 3 and 8 may run.
The candle-content portion of step 2 is blocked. Steps 4–7 require T3 or broader
authority and must not run.

The rule is deliberately narrow:

- It allows reading payloads for audit only within T2 limits. It does not allow
  training. A model fit
  would be a new trial and needs its own approval (M5.3 or later).
- It permits only read-only Oracle commands needed to capture the existing
  collection summary and SHA-256 manifest. It forbids collection reruns and candle
  downloads.
- It keeps the existing rules on `ShoonyaMinuteV1`/`ShoonyaDailyV1` and on not
  inferring facts.

## 2. Facts established without reading payloads (2026-11-14)

The first pass ran only directory existence checks and `stat`. The Decision 3
preflight below added a file inventory and a byte-equality check of colliding
paths. No candle file was decompressed or parsed.

| Item | Observation | Consequence |
| --- | --- | --- |
| `data/raw/shoonya/NSE/` | Exists (daily raw root) | Bound by the M3.1 readiness manifest |
| `data/raw/shoonya-minute/NSE/` | Exists, mtime 2026-11-01, mode `drwxr-xr-x` | The documented M3.4 minute raw root (`docs/m34-minute-ingestion-fusion.md`) |
| `data/raw/shoonya/NSE/minute/` | Exists, mtime **2026-11-14 11:35**, mode `drwxrwxr-x` (group-writable) | A **second** minute location, **inside the daily root**. Unknown whether a transfer reached it completely |
| `m3.minute_run.confirm_daily_publication` | Enumerates the daily root with `rglob("*")`, and any file not bound by the manifest goes into `unbound_files` | If `NSE/minute/` holds any file, daily publication is `NOT_CONFIRMED`, which blocks the M3.4/M3.5/M5 paths that depend on it |
| Oracle summary: 2,715 attempted / 2,690 succeeded / 25 failed | **User-reported**, not verified | Needs the log registered as an evidence file |

**Decision 3 preflight, 2026-11-14 16:34–16:37 UTC.** This was a T1 inventory plus one
byte-equality check of colliding paths with `cmp -s`, which doesn't decompress,
parse or print anything:

- Both paths are real directories on the same ext4 device, with no symlinks,
  hard links, special files or separate mounts.
- Source `shoonya/NSE/minute/`: 81,330 files (27,110 complete triplets), 2,690
  symbol directories, 184,661,597 bytes, day files 2026-01-01 to 2026-11-13. No
  zero-byte or partial files. Newest mtime is 11:35:47 UTC, with no writer seen
  5 hours later.
- Target `shoonya-minute/NSE/`: 636 files (212 triplets, RELIANCE-EQ and TCS-EQ)
  plus `.stock-gplm-m34.lock`. These were published by M3.4 import run
  `5130e07e…`.
- All 636 target files exist at the same relative path in the source with
  identical bytes. There are no conflicts. 80,694 source files are new.

**Blocking prerequisite P0.** The user must decide where the new minute data
belongs before any audit. Options:

- (a) Leave it in place and run M5.2 against it as an unpublished staging area.
  Daily confirmation stays broken until it is moved.
- (b) The user moves it out of the daily root. M5.2 then imports it into a new,
  versioned minute raw root through `m3.minute_publish`, hash-bound and read-only.

Recommendation: (b). M5.2 must not move raw files itself, so the user performs
the move or authorizes it explicitly.

## 3. Execution plan

Each step writes a report and a manifest under `reports/m52/<run-id>/`, outside
every raw root. Reports never overwrite earlier reports.

| # | Deliverable | Method (reuse first) | Output |
| --- | --- | --- | --- |
| 1 | Transfer completeness, without downloading anything | Compare the local inventory (name, size, SHA-256) against a **source manifest the user supplies**, made on Oracle with `sha256sum` or the collector's own manifest. Files that match are skipped. Missing or mismatched files are listed, never fetched. Reuse the `m3.minute_publish` source-hash checks. | `transfer_inventory.json`, `transfer_diff.json`; status `COMPLETE` / `INCOMPLETE` / `UNVERIFIABLE` (no source manifest) |
| 2 | Candle integrity | Run the `m3.minute` `ShoonyaMinuteV1` adapter and its quality checks per segment. Check timestamps (IST, monotonic after reversal), duplicate bars, OHLC invariants, negative `intv` (quarantine), and session coverage against the **registered** NSE session and holiday evidence from M3.6 only. Daily data reuses `ingestion.daily.validate_daily`. | Per-segment `ACCEPTED` / `QUARANTINED` / `REJECTED`, with rejected records listed and nothing repaired |
| 3 | The 25 failed symbols | Parse the registered summary log, read-only and never evaluated as code. Classify each symbol: delisted, renamed (alias evidence), suspended, token not in the current master, provider error or `UNKNOWN`. | `failed_symbols.json` and a **recovery plan**: per symbol, the evidence needed and a proposed user-run retrieval. No retrieval in M5.2 |
| 4 | Corporate actions, identity, evidence gaps | Reuse `m39b.detect_suspicious_discontinuities`. Without licensed, registered facts, adjustments stay `UNKNOWN` and are not applied. Use the M2.1 master for current identifiers only, and M3.6/M3.9 registered sources for history. | `evidence_gaps.json` listing each symbol and date range with its missing fact type |
| 5 | Provenance and dataset manifests | Canonical, checksum-bound manifest per dataset version: code commit, lockfile hashes, input hashes, row counts, exclusion lists. Follow the M5 `provenance` / `run_store` conventions. | `dataset_manifest.v1.json` plus its SHA-256 |
| 6 | Training eligibility without the holdout | Reuse `m35.policy` gates and `m5.adapter` eligibility. Segments whose window **straddles the governed cutoff** stay rejected unopened until the `m5.migration_spec` migration is done (separate approval). Universe is point in time, ETFs excluded. | `eligibility.json`: per symbol and horizon `ELIGIBLE` / `BLOCKED(reason)`; the overall verdict stays `NOT_READY` unless every gate passes |
| 7 | Reuse M3 training infrastructure | Map eligible outputs onto the `m35.data` / `m3.splits` / `m3.fusion` inputs. Training stays out of scope. | An interface compatibility report. No fit |
| 8 | Strategy definitions | Confirm `v1.registry` / `v1.schema` artifacts are versioned and hash-bound, and that M5.2 changes none of them. | A check in the run report |

## 4. Acceptance gates for M5.2

M5.2 closes only when every gate below is met and an independent review gives GO.

1. **G0 Scope.** The approved rule is in `AGENTS.md` in a dated commit, and P0 is
   decided.
2. **G1 Raw immutability.** Every raw file has the same SHA-256 before and after
   the run, and the run made no writes under any raw root (checked with a
   pre/post inventory).
3. **G2 Completeness.** The transfer status is `COMPLETE`, or every gap is listed.
   `UNVERIFIABLE` is allowed only when stated explicitly.
4. **G3 Integrity.** Every segment has a decision. Nothing is filled or repaired,
   and rejected and quarantined records are visible.
5. **G4 Failed symbols.** All 25 are classified, each with a recovery action or
   `UNKNOWN` plus the evidence still needed.
6. **G5 Evidence gaps.** Corporate-action and identity gaps are listed, and no
   adjustment is applied without registered facts.
7. **G6 Manifests.** Dataset manifests rebuild byte-identical from the same inputs.
8. **G7 Holdout.** No protected-partition byte was read. The trial ledger is
   byte-identical before and after. No trust root is installed.
9. **G8 Tests.** New code has synthetic tests only. The full suite passes with
   `PYTHONPATH=src`. Real-data results are reported separately from synthetic
   evidence.
10. **G9 No side effects.** No network calls, Oracle access, trades, rankings,
    model fits, merges, pushes or M6 work.

## 5. Out of scope, each needing separate approval

- Installing the operational trust root.
- Protected-partition migration.
- Recovering the 25 symbols.
- Any model fit (M5.3 or later).
- Scoring the holdout.
- Paper or live trading.
- M6.

## 6. Open decisions for the user

1. Separately authorize or reject import execution after reviewing this handover.
2. Choose the final disposition of `data/raw/shoonya/NSE/minute/`; no relocation is
   authorized by this scope.
3. Decide whether and when T3 governed development-data preparation may begin.
