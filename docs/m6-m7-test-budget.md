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
