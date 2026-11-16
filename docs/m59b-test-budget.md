# M5.9B test-budget ledger

Limit: **10 executed test cases** for the whole milestone (every execution counts, including reruns).
No M5.9A test is re-executed; no full suite, real data, replay, training or GPU job.

## Plan (written before any execution)

One file, `tests/test_m59b_governance.py`, eight plain test functions (no parametrization → one case each).
They are executed once, in one command, by explicit node ID. Two cases are held in reserve for reruns.

| # | Node ID | Covers | Cases |
| --- | --- | --- | --- |
| 1 | `tests/test_m59b_governance.py::test_calendar_registration_holiday_week_coverage_and_activation` | calendar validate/register/activate/deactivate, holiday-shortened week, partial-session close, coverage failure → `CALENDAR_UNKNOWN`, no weekday fallback | 1 |
| 2 | `tests/test_m59b_governance.py::test_model_registration_is_immutable_and_hash_bound` | artifact registration, hash-derived id, read-only store, rejection of unreadable/incompatible artifacts | 1 |
| 3 | `tests/test_m59b_governance.py::test_model_approval_revocation_and_expiry_are_explicit_and_append_only` | explicit approval with evidence, altered artifact refused, expiry, revocation final, append-only | 1 |
| 4 | `tests/test_m59b_governance.py::test_fixed_approved_model_never_retrains_and_passes_the_live_gate` | fixed model never fits, deterministic, complete passing LIVE gate, candidate → SHADOW, revocation blocks | 1 |
| 5 | `tests/test_m59b_governance.py::test_strategy_lifecycle_transitions_evidence_and_origin_mapping` | lifecycle transitions, evidence, retirement, origin mapping | 1 |
| 6 | `tests/test_m59b_governance.py::test_live_universe_registration_supplements_and_membership` | universe file, activation, supplements, membership | 1 |
| 7 | `tests/test_m59b_governance.py::test_canonical_live_gate_failure_table_status_mapping_and_db_guard` | table-driven gate failures (approval, stale, stop, universe, calendar, strategy, versions, ...), legacy status mapping, DB LIVE guard | 1 |
| 8 | `tests/test_m59b_governance.py::test_verifier_detects_unsafe_and_legacy_rows_without_modifying_them` | read-only verifier detection of every M5.9B violation class | 1 |

Command (8 cases):

```
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider <the 8 node IDs above>
```

A `--collect-only` check (executes nothing) confirms exactly 8 cases are collected first.

## Execution ledger

Collect-only check (2026-11-16): 8 collected, 0 executed.

| Execution # | Node ID | Reason | Result | Cumulative |
| --- | --- | --- | --- | --- |
| 1 | `...::test_calendar_registration_holiday_week_coverage_and_activation` | Phase 2 calendar registry | PASSED | 1 |
| 2 | `...::test_model_registration_is_immutable_and_hash_bound` | Phase 3 registration / immutable hash | PASSED | 2 |
| 3 | `...::test_model_approval_revocation_and_expiry_are_explicit_and_append_only` | Phase 3 approval / revocation | PASSED | 3 |
| 4 | `...::test_fixed_approved_model_never_retrains_and_passes_the_live_gate` | Phase 4 + complete passing LIVE gate | PASSED | 4 |
| 5 | `...::test_strategy_lifecycle_transitions_evidence_and_origin_mapping` | Phase 5 strategy lifecycle | PASSED | 5 |
| 6 | `...::test_live_universe_registration_supplements_and_membership` | Phase 6 universe | PASSED | 6 |
| 7 | `...::test_canonical_live_gate_failure_table_status_mapping_and_db_guard` | Phase 7 failure table + Phase 1 D mapping | PASSED | 7 |
| 8 | `...::test_verifier_detects_unsafe_and_legacy_rows_without_modifying_them` | Phase 8 verifier | PASSED | 8 |

Single command, `8 passed in 0.55s`. **Total executed in M5.9B: 8 of 10.** No rerun was needed; the two
reserve cases were not used. (`...` = `tests/test_m59b_governance.py`.)

## Not executed (static edits only)

Four M5.9A tests were edited because M5.9B intentionally changed contracts they assert. They were **not**
re-executed (the milestone forbids re-running M5.9A tests); the owner's full-suite run is their evidence:

* `tests/test_m59a_prediction_contracts.py`: `_row` default mode `LIVE` → `SHADOW` (the DB now refuses
  `LIVE` without a canonical decision); `test_unapproved_adaptive_refit_cannot_become_live` (the
  `live_gate` wrapper now fails closed: adaptive → SHADOW; LIVE needs calendar id + universe + fixed model);
  `test_schema_v1_database_is_migrated_additively` (expects schema `3`);
  `test_owner_verifier_flags_ungated_live_and_passes_clean_db` (legacy/tampered LIVE rows stored raw).
* `tests/test_m59a_live_freshness.py`: `_pred` stores simulated pre-guard `LIVE` rows raw.

Rolling-engine tests (`test_unapproved_rolling_refit_on_fresh_latest_bar_is_shadow_not_live`,
`test_app_gplm.py`) were reviewed statically: their assertions (SHADOW, `MODEL_NOT_APPROVED` among the
reasons, no `LIVE` rows) still hold under the new gate; they were not executed.
