# M5.9A compliance addendum (written in M5.9B)

Date 2026-11-16. Read-only inspection of commits `277dceb`, `a90dfbb`, `3e1169b` (base `c587cf3`), their
contracts, migration, acceptance document and handover. No test was executed for this check.

## A. Actionable contract

| Field | Enforced at M5.9A? | Evidence | M5.9B |
| --- | --- | --- | --- |
| action | Partly: any non-empty `direction` passed (e.g. `AVOID`) | `contracts.validate_actionable_row` used `row.direction` | Must be LONG/SHORT/BUY/SELL |
| entry / trigger, invalidation, stop, ≥1 target, horizon, probability, expected return, model artifact/version, decision timestamp | Yes, for `actionable=1` rows | `AuditDB.add_prediction` → `validate_actionable_row` | unchanged |
| strategy version where applicable | Yes for `source_kind` STRATEGY/RULE | same | unchanged |
| freshness status | Partly: any non-empty string passed (e.g. `STALE_DATA`) | same | Must be `FRESH` (or `HISTORICAL_REPLAY` for simulated replay trades) |
| `LIVE` tied to a gate decision | **No**: `mode='LIVE'` was written by convention only | `rolling._classify` | DB refuses `mode='LIVE'` without an eligible canonical `live_decision` |

Missing values are never fabricated; the row stays non-actionable with reason codes
(`OPPORTUNITY_INCOMPLETE:<fields>`).

## B. LIVE approved-strategy gate

At M5.9A, strategy-driven outputs passed `strategy_approved=False` unconditionally: they could never be
`LIVE` (fail-closed) but there was no approval mechanism, and the model-only classification was implicit
(`source_kind='MODEL'`). M5.9B adds the strategy lifecycle (APPROVED required; RETIRED blocks), and every
decision is explicitly `MODEL_ONLY` or `STRATEGY_TRIGGERED`. A model-only output must carry an approved
artifact and is never presented as a strategy-triggered trade.

## C. LIVE eligible-universe gate

**Not implemented at M5.9A.** `daily.run_daily` passed `"universe_eligible": True` for every symbol and
`contracts.live_gate` defaulted `universe_eligible=True`. M5.9B adds the registered live-alert universe;
`daily` passes `governance.universe_membership(db, symbol)`, and the gate fails closed without an active
universe (`UNIVERSE_NOT_ELIGIBLE`). The `live_gate` wrapper now defaults to `False`.

## D. Shared status vocabulary

Documented once at M5.9A (`contracts.py` module docstring; `docs/m59a-prediction-contracts.md` §1), with
`UP`/`DOWN`/`NEUTRAL` defined separately (§2). Gap: the code-level legacy map (`LEGACY_STATUS_MAP`) covered
only `HISTORICAL_FROZEN`, `REPLAY` and `LIVE`; a status-less `SHADOW` row and unknown values had no defined
reading. M5.9B adds `contracts.shared_status` (table in `docs/m59b-governance.md` §8). No row was rewritten.

## E. M5.9A test accounting

Recorded commands and counts, derived from the acceptance document and from the test files at each commit
(`def test_` counts; the only parametrized test, `test_neutral_band_boundaries`, has 6 cases):

| Run | Selection | Cases executed | Why |
| --- | --- | --- | --- |
| Baseline (before changes) | `test_m58_prediction.py` (12) + `test_app_gplm.py` (21) | 33 (32 passed, 1 skipped) | regression baseline of the two files M5.9A would change |
| Main run (after changes) | 9 files: `test_m59a_prediction_contracts.py` (17 cases then), `test_m59a_live_freshness.py` (7), `test_m58_prediction.py` (12), `test_app_gplm.py` (21), `test_m58_feature_engine.py` (5), `test_m58_stock_dna.py` (3), `test_m56.py` (8), `test_m57_strategy_engine.py` (6), `test_ml_discovery.py` (1) | 80 (79 passed, 1 skipped) | new M5.9A tests plus modules importing the changed code |
| Verifier run | `test_m59a_prediction_contracts.py` (now 18 cases) | 18 | to execute the newly added verifier test |
| Pre-existing failure check | `test_m54_unified.py` (18 cases) on the branch and on an untouched `c587cf3` worktree | between 8 (only the 4 failing cases, twice) and 36 (whole file, twice); the record does not say which | `m54/timeframes.py` was changed; the 4 `KeyError: '4h'` failures were shown to predate M5.9A |

* **Overlap:** yes. The reported "79" and "18" overlap in 17 cases: the 18-case run re-executed the 17
  contract-file cases already in the 80-case run, plus 1 new verifier test. The 33 baseline cases are the
  same node IDs as 33 of the 80 (run before and after the change).
* **Exact total executed:** recorded executions = 33 + 80 + 18 + (8..36) = **139 to 167**; distinct test
  cases = 81 (the 80 + the verifier test) + 18 (`test_m54_unified.py`) = **99**.
* **Not determinable:** any intermediate development runs not written into the M5.9A record. The repository
  holds no log of them, so the true total may be higher than 139–167. This is stated rather than estimated.
