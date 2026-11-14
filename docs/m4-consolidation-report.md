# M4 consolidation report — 2026-11-14

M4 preserved the uncommitted V1 work, reviewed it, fixed the defects that bias
results or break integrity, and recorded the corporate-action and M2.5-R
integration decisions. The inventory and the defect list are in
[m4-integration-inventory.md](m4-integration-inventory.md).

## Git result

| Branch | Commit | Content |
| --- | --- | --- |
| `feature/v1-configurable-strategy-engine` | `0534cc8` | V1 exactly as found, parent `35bdc06` |
| `feature/m4-v1-consolidation` (worktree `/home/mgurunath/projects/stock-gplm-m45`) | this commit | M4 remediation, tests and reports, parent `0534cc8` |

The commits `feature/m39b-corporate-actions` (`35bdc06`), `feature/m39-nse-mcp`
(`df8324b`), `feature/m25r-cloud-recovery` (`dbba2b1`) and every other branch and
worktree are unchanged. Nothing was pushed, merged into `main`, stashed, reset or
force-checked-out.

## M4.2 V1 preservation

`git switch -c feature/v1-configurable-strategy-engine` kept the dirty working
tree and committed only the 17 V1 paths listed in the inventory. The commit was
made before any review edits, so the original V1 stays reproducible.

## Remediation (D1–D9)

| ID | Change | Evidence |
| --- | --- | --- |
| D1, D4 | Exit-condition and maximum-holding decisions are taken at the completed close and fill at the next open. The policy is now `completed_close_signal_next_session_open_v2`. | `test_exit_condition_fills_next_open_not_the_deciding_close`, `test_max_holding_exit_executes_at_next_open` |
| D2 | New `stock_gplm.v1.execution.resting_exit`: an open through the stop fills at the open; an open through the target is capped at the target; when both are touched, the stop fills first. | `test_gap_through_stop_fills_at_the_worse_open`, `test_gap_up_target_is_capped_and_both_touched_assumes_stop` |
| D3 | Stop/target levels are measured from the observed fill open. | covered by the gap tests |
| D5 | `run_backtest` results carry `data_classification: CALLER_SUPPLIED_UNVERIFIED`. Real-data eligibility is never accepted from a client (enforced in M5). | `test_studio_backtest_result_is_labelled_unverified` |
| D6 | `BEGIN IMMEDIATE` around version allocation. | `test_concurrent_saves_allocate_distinct_versions` |
| D7 | SQLite triggers abort `UPDATE`/`DELETE` on `strategy_versions` and `backtest_results`. | `test_registry_rows_are_append_only_and_backtests_idempotent` |
| D8 | Identical backtest re-records are idempotent; differing payloads under one hash are rejected; SQLite errors return HTTP 409. | same tests |
| D9 | The Studio requires a loopback `Host` and a matching `Origin` (if present), otherwise 403. | `test_studio_rejects_rebinding_hosts_and_cross_site_origins` |

Every one of the 8 new regression tests fails against the original V1 `0534cc8`
(checked in a temporary detached worktree; 8 failed). D10 (no portfolio
accounting) is handled by the M5 simulator. D11 is low severity and recorded only.

## M4.3 corporate-action integration decision

M3.9-B (`35bdc06`) is a direct child of the M3.9-A baseline `df8324b`, and V1 was
built on top of it. Integration is therefore plain ancestry: no cherry-pick,
merge or rebase was needed or performed.

Review against the mandate:

| Requirement | Finding |
| --- | --- |
| Raw OHLCV untouched | Yes. `adjust_series` returns a separate `AdjustmentResult` holding the raw candle inside each adjusted candle, and refuses non-`RAW_UNADJUSTED` input. |
| Explicit, reproducible adjustments | Yes. Exact `Fraction` factors, a single rounding to 8 decimals, and a content-hash `adjustment_id`. |
| Split/bonus treatment | Correct. Backward price factors are old/new and held/(held+bonus); volume factors are reciprocal. The ex-date candle is not adjusted. |
| Causal effective dates | Per call, yes: events with `available_at > knowledge_at` raise `LookaheadError`, and ex-dates after `knowledge_at` are ignored. |
| No future leakage into earlier decisions | **Not by itself.** Adjusting a whole series once with `knowledge_at` = the end of the backtest backward-adjusts early bars with later events. Level-based rules (such as `close > constant`) would then see information unavailable at the time. M5 must evaluate signals per as-of regime (re-adjusting with `knowledge_at` = each decision date), not on one end-of-period series. |
| Missing or ambiguous evidence | Refused (`UNKNOWN`, `QUARANTINED`, unlicensed, conflicting, duplicate, rights, mergers). |
| No mixing of adjusted and unadjusted | Enforced by the basis check. M5 executes and values trades in raw traded prices only. |

Decision: M3.9-B is kept unchanged. It is usable by M5 only through as-of regime
evaluation. Because no validated, licensed corporate-action events or coverage
catalogs exist locally, strict real-data backtests that need adjustment evidence
are blocked. Diagnostic runs stay raw-unadjusted, with discontinuities flagged by
`detect_suspicious_discontinuities`.

## M4.4 M2.5-R integration policy

M2.5-R is **not merged**. Its useful research-facing artifact is the calendar
registration format `stock-gplm.m25r-registered-calendar.v1`: a JSON document
with a canonical-hash `registration_id`, a manifest bound to a browser-downloaded
NSE document, and exactly one explicit status per date. M5 reads this format with
its own small, side-effect-free verifier. That verifier recomputes the canonical
hash and the no-gap invariant, so it cannot drift silently from the contract. M5
imports no M2.5/M2.5-R code (planner, OAuth, browser, deployment or collector),
and the recovery worktree stays isolated and unchanged. M2.5-R stays not approved
for operational activation.

## M4.5 validation

| Check | Command | Result |
| --- | --- | --- |
| Full suite including real-data regressions | `cd stock-gplm && PYTHONPATH=../stock-gplm-m45/src .venv/bin/python -m pytest ../stock-gplm-m45/tests` | 626 passed, 0 skipped |
| Baseline comparison | clean `35bdc06`: 597 passed + 2 skipped (no `data/`); V1: +19; M4: +8 | no regressions; no test removed or weakened. One V1 assertion changed from `…_v1` to `…_v2`, matching the deliberate policy change, and gained a classification assertion. |
| V1 + M4 focused | `pytest tests/test_v1_strategy_engine.py tests/test_m4_v1_remediation.py` | 27 passed |
| Compilation | `python -m compileall -q src` | PASS |
| Studio JavaScript syntax | `node --check` on the embedded script | PASS |
| Unsafe constructs | grep for `eval(`, `exec(`, `pickle`, `marshal`, `yaml.load`, `shell=True`, `__import__`, `os.system` in `src/stock_gplm/v1` | none |
| Whitespace | `git diff --check` | PASS |
| Data immutability | SHA-256 of all 31,928 files under `data/` and `reports/` vs pre-change baseline | unchanged |

## M4 acceptance

| Gate | Status | Evidence |
| --- | --- | --- |
| 1. V1 independently reviewed and committed | PASS | `0534cc8` (as found) and the M4 remediation commit |
| 2. Historical branches and worktrees intact | PASS | all branch heads unchanged; only new branches/worktree added |
| 3. Corporate-action integration decision recorded | PASS | section M4.3 |
| 4. M2.5-R operationally isolated | PASS | not merged; worktree untouched; no scheduling or authentication |
| 5. No unexplained test regressions | PASS | 626 passed; the one changed assertion is explained |
| 6. Raw data unchanged | PASS | hash baseline verified |
| 7. Reproducible integration report | PASS | this document and the inventory |
| 8. No deployment, collection or broker execution | PASS | no network operations, credentials, services or orders |
