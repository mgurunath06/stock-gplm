# M8.1 prototype + research automation test budget

Date 2026-11-20 (Claude). Cap: **10 executed cases**. Both test files were written by Codex (owner-run).
Cases run by explicit node ID only. Earlier milestones' tests are not rerun.

| # | Node ID | Covers | Cases |
| --- | --- | --- | --- |
| 1 | `tests/test_cloud_retention.py::test_path_escape_rejected` | cloud retention | 1 |
| 2 | `tests/test_cloud_retention.py::test_invalid_publication_never_listed` | cloud retention | 1 |
| 3 | `tests/test_cloud_retention.py::test_pc_requires_all_three_matching_files` | cloud retention | 1 |
| 4 | `tests/test_cloud_retention.py::test_pc_conflict_is_not_acknowledged` | cloud retention | 1 |
| 5 | `tests/test_library_4h_features.py::test_four_hour_feature_contract[4h:EMA(period=20).acceleration]` | 4h feature regex | 1 |
| 6 | `tests/test_library_4h_features.py::test_unknown_timeframe_still_rejected` | 4h feature regex | 1 |
| — | reserve (reruns after a fix) | | 4 |

Planned: 6. Reserve: 4. The other four 4h parametrized cases use the same regex path; they are left to the owner's full-suite run.

**No automated tests (owner smoke / already in use):** `evidence.py`, `setup_replay.py`, `strategy_scorecard.py`,
`stock_profile.py`, `strategy_llm.py`, `minute_gaps.py`, `universe_sync.py`, live-lab changes, Oracle
`cloud_sync.py`/`transfer_ledger.py`. All modules pass `py_compile`; they have been running live since 2026-11-20.

## Execution record

2026-11-20: one command with the 6 node IDs above, `6 passed in 0.82s`. Executed 6 of 10; reserve unused.

