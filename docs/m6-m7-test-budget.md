# M5.10–M7.3 test budget (PRD v1 roadmap build)

Date 2026-11-19 (Claude). Cap: **10 executed cases**. Each test function in `tests/test_m6_m7_roadmap.py`
is one case (no parametrization). The file was written before anything ran. Cases run by explicit node ID only,
and earlier milestones' tests are not rerun.

| # | Node ID | Milestone | Cases |
| --- | --- | --- | --- |
| 1 | `tests/test_m6_m7_roadmap.py::test_cost_schedule_contract_note_and_lane_costs` | M6.0 | 1 |
| 2 | `tests/test_m6_m7_roadmap.py::test_monthly_bars_and_3m_lane` | M6.1 | 1 |
| 3 | `tests/test_m6_m7_roadmap.py::test_indicator_store_provenance_and_schema_v4` | M6.1 | 1 |
| 4 | `tests/test_m6_m7_roadmap.py::test_new_dsl_primitives_are_causal` | M6.2 | 1 |
| 5 | `tests/test_m6_m7_roadmap.py::test_horizon_labels_outcomes_and_pending` | M6.3 | 1 |
| 6 | `tests/test_m6_m7_roadmap.py::test_benchmark_freeze_before_reveal_and_reproducible` | M6.4 | 1 |
| 7 | `tests/test_m6_m7_roadmap.py::test_hybrid_fallback_and_controls` | M6.5 | 1 |
| 8 | `tests/test_m6_m7_roadmap.py::test_paper_ledger_forward_only_idempotent_and_lifecycle_gate` | M6.6 | 1 |
| 9 | `tests/test_m6_m7_roadmap.py::test_opportunity_rank_alert_lifecycle_risk_and_notify` | M7.0–M7.2 | 1 |
| — | reserve (one rerun after a fix) | | 1 |

Planned: 9. Reserve: 1. The execution record is below.

**Statically edited, not executed:** `tests/test_m59a_prediction_contracts.py` line 297 (schema version
`"3"` → `"4"`, additive v4). The owner's full-suite run covers it.

**Not covered by these cases (owner smoke):** `gplm model-export`, `gplm ops scan|post-close|intraday` on
real data, `gplm hybrid` and `gplm benchmark` on real data, webhook/email delivery.

## Execution record

2026-11-19: one command with the 9 node IDs above, `9 passed in 2.79s`. Executed 9 of 10; the reserve is unused.
Warnings were only a NumPy timedelta deprecation in the test fixture.

## Owner-requested acceptance run (2026-11-19, native Windows; the owner asked Claude to run it)

The owner told Claude to run the tests itself, which approved going past the 10-case cap.

* **Full suite, new branch:** 855 passed, 217 failed, 2 errors (205 s). The same files on the
  pre-roadmap commit `c691a16` give 215 failed and 2 errors. **Every one of those 215 failures and 2
  errors is pre-existing and environmental:** POSIX-only behaviour on native Windows (trust-directory
  permission checks: 153; `os.geteuid`/`os.fchmod`: 29; symlink privilege; relative cache paths; regex
  messages). They pass on Linux/WSL.
* **New failures:** two, both fixed:
  * `test_m58_feature_engine` pinned the old gap count. Updated to the M6.2 contract: 0 rows without
    expressions, 2 PARTIAL rows.
  * `test_verify_suite::test_writes_auditable_outputs` failed on a real bug that verify caught: a
    look-ahead in the pivot zig-zag reduction, plus the cost check crashing on the new `schedule` key.
    Fixed in `8ad2179`.
* **Pre-existing failure fixed as a by-product:** `test_verify_suite::test_engine_and_leakage_checks_pass`.
  String-argument primitives, including the older `dtw_distance`, are now exercised by explicit examples.
* **`gplm verify`:** PASS_WITH_TRACKED_GAPS. Sections 0–5 have no failures, and section 3 passes 21/21.
  Tracked calculation gaps: **16 → 2** (cup/wedge/flag patterns; Three Drives).
* **Real data (RELIANCE):** costs note matches a hand calculation. Coverage: 379 strategies, 312 runnable,
  67 blocked, 0 invalid. Benchmark run twice gives `reproduced: true` for frozen and adaptive. Horizons:
  988/986/984/982 mature labels; always-long is net-negative after costs. Model export: 5 lanes SKIPPED
  (all champions are the baseline). This run found the lane-key bug fixed in `206fc9c`.
  `ops pre-open` and `ops scan` work end to end (20 strategies, 3 signals → 3 NO_TRADE/UNAVAILABLE
  records with reasons). Database migrated to v4; `audit --verify` is ok (16,546 events), quick_check ok.
