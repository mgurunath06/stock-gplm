# M5.9A acceptance — prediction correctness and safe output contracts

Date 2026-11-16. Branch `feature/m55-gplm-app`. Starting commit `c587cf3`. Evidence class:
**synthetic unit/regression tests only**; no real-data run, training, replay or backtest was executed.
Contract reference: `docs/m59a-prediction-contracts.md`.

## Verified defect matrix

Each finding was checked against the code at `c587cf3` before any change.

| # | Reported finding | Confirmed? | Evidence at `c587cf3` | Resolution |
| --- | --- | --- | --- | --- |
| A | Weekly bars always complete | **Confirmed** | `m54/timeframes.py::weekly_bars` set `incomplete=False` for every week and assumed Friday 15:30 as week end. `m58.walk_forward` (non-1d) planned on `~incomplete`, so the open final week could be a decision or target bar and reveal matched it by `end_ns`. | Week is final only with registered calendar evidence (`week_status`: COMPLETE / PARTIAL / MISSING_EXPECTED_BAR / CALENDAR_CONFLICT / CALENDAR_UNKNOWN). Holiday weeks readable at their real last session. No calendar → weekly prediction suppressed (`CALENDAR_UNKNOWN`). |
| B | Horizon and target can disagree | **Confirmed** | `m58.predict` accepted any `--target` after the as-of (required CLI arg) and never related it to `--horizon`; reveal stored `bars_held=horizon` blindly. Rolling stored `target_session = sessions[t+1]` for every horizon and the live target as the "next weekday" (weekday calendar inference). | `targets.derive_target` (grid → calendar → observed eligible bars). Explicit target must match exactly (`TARGET_HORIZON_MISMATCH`, nothing frozen). Decision bar, horizon, derived target, derivation, reveal bar and bars held are stored; mismatched reveals are `NOT_COMPARABLE`. Weekday inference removed. |
| C | Neutral movement scored as DOWN / confused with uncertainty | **Confirmed** (partial in M5.8, full in rolling) | M5.8 v3: `FLAT` only for exactly zero, and Brier used `int(gross > 0)` (zero → DOWN). Rolling: `up = net > 0`, so any gross move ≤ cost was DOWN; `correct = (p≥0.5) == up`. No NEUTRAL class anywhere. | UP/DOWN/NEUTRAL with versioned cost-aware band frozen per prediction; NEUTRAL excluded from accuracy and Brier and counted separately; models/calibration on UP/DOWN rows; `UNCERTAIN` stays the abstention token only (`confidence_status`). M5.8 contract → v4; rolling → v2. |
| D | Staleness relative to batch; report ignores session | **Confirmed** | `daily.run_daily`: `stale = last_session != max(batch)` — a one-symbol run or an all-stale batch was never stale. `report.live_forecasts(session=...)` ignored `session`. | Absolute per-symbol freshness against the requested/as-of session (`--as-of` or registered calendar); `STALE_DATA`, `QUARANTINED_INPUT`, `CALENDAR_UNKNOWN`, intraday expected-bar check. Report honours `session`; older forecasts → `UNAVAILABLE`/`STALE_DATA`. `--as-of` on `daily`/`replay`, `--session` on `predictions`. |
| E | `LIVE` = merely the latest prediction | **Confirmed** | Rolling: `live = t == len(m) - 1` → `mode='LIVE'` for the last bar regardless of freshness; the "champion" is a model-kind string refitted every step; no approval record exists. | Shared fail-closed LIVE gate; append-only `model_approvals` (schema v2) keyed by artifact id + sha256; adaptive refits → `SHADOW` (`MODEL_NOT_APPROVED`); rules/strategies → `SHADOW` (`STRATEGY_NOT_APPROVED`). No approval is ever created by the program. |
| F | Actionable output without stop/target | **Confirmed** | M5.8 stored `actionable=int(tradable)` with `stop=None, target=None`. | Opportunity contract validated in `AuditDB.add_prediction` (actionable rows missing any mandatory field are rejected). M5.8 always `actionable=0`; trade gate kept as `research_trade_gate_passed` (`RESEARCH_TRADE`). Missing fields recorded in `payload.opportunity.missing`. |
| G | Intraday engine drops the final completed bar and then requires it for LIVE | **Confirmed** | Rolling `_steps` kept `idx < session_last[idx]`, where `session_last` comes from data presence: the latest completed bar of a session in progress was never a decision bar, and `live = t == len(m)-1` could therefore never hold for intraday lanes. | Decisions use the session grid: latest completed mid-session candle is a decision bar (target = next grid bar); the current partial candle never is; the session's final bar is explicitly `UNAVAILABLE`/`END_OF_SESSION` (policy `unavailable`); nothing is manufactured. |

## Acceptance criteria

| Criterion | Result | Test(s) |
| --- | --- | --- |
| Partial weekly candles cannot be scored as completed | Met | `test_weekly_ordinary_complete_partial_holiday_and_unknown`, `test_weekly_prediction_suppressed_without_registered_calendar` |
| Holiday-shortened weeks do not shift horizon targets | Met | `test_weekly_decision_and_target_align_across_holiday_week` |
| Target and horizon cannot disagree | Met | `test_horizon_one_cannot_accept_target_three_bars_later_daily_intraday_weekly`, `test_m58_rejects_mismatched_target_before_freezing_and_stores_contract`, `test_m58_reveal_records_reveal_bar_and_bars_held`, `test_multitimeframe_alignment_...` |
| Neutral distinct from uncertainty | Met | `test_neutral_band_boundaries` (6 cases), `test_neutral_is_not_down_and_uncertain_is_not_neutral`, `test_actual_movement_uses_cost_aware_neutral_band_and_neutral_is_not_scored` |
| Stale data cannot produce actionable or LIVE | Met | `test_freshness_is_absolute_one_mixed_all_stale_and_historical`, `test_daily_all_stale_suppresses_live_and_reports_stale` (CLI smoke), `test_report_uses_requested_session_and_never_shows_stale_or_unapproved_as_live` |
| LIVE requires approved immutable artifact and safe inputs | Met | `test_unapproved_adaptive_refit_cannot_become_live`, `test_unapproved_rolling_refit_on_fresh_latest_bar_is_shadow_not_live` |
| Actionable predictions cannot omit stop/target/invalidation | Met | `test_actionable_rows_require_the_full_opportunity_contract` |
| Latest completed intraday candle → correctly classified next-candle forecast | Met | `test_intraday_targets_mid_session_final_bar_and_next_session`, `test_latest_completed_mid_session_candle_is_a_decision_bar_and_current_partial_is_not`, `test_final_completed_session_candle_is_explicitly_unavailable` |
| Append-only evidence preserved and contract-versioned | Met | `test_schema_v1_database_is_migrated_additively`; v4/v2 identities; triggers unchanged |
| Focused regression tests pass | Met | see below |
| No bulk research or lengthy workload | Met | only synthetic fixtures; largest run ≈ 60 s |

## Tests executed (synthetic only)

```
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q tests/test_m59a_prediction_contracts.py \
  tests/test_m59a_live_freshness.py tests/test_m58_prediction.py tests/test_app_gplm.py \
  tests/test_m58_feature_engine.py tests/test_m58_stock_dna.py tests/test_m56.py \
  tests/test_m57_strategy_engine.py tests/test_ml_discovery.py
79 passed, 1 skipped (CUDA-only test), 60 s
```

Afterwards the owner verifier (`m59a_verify`) and its test were added: `test_m59a_prediction_contracts.py`
18 passed.

Baseline before changes: `test_m58_prediction.py` + `test_app_gplm.py` 32 passed, 1 skipped.
`tests/test_m54_unified.py` has **4 pre-existing failures** (`KeyError: '4h'` in `m54/unified.py:794`),
reproduced identically on an untouched worktree of `c587cf3`; not caused or fixed by M5.9A.
The full repository suite was **not** run.

Tests changed on purpose (contract changes, not weakened checks): latest-bar rows are now `SHADOW`
instead of `LIVE` (`test_app_gplm.py`); the 15m h=1 forecast with a next-day target is now rejected;
"executable trades" became "research-gate trades (not actionable)"; the v2 "direction ignores costs"
test was replaced by the cost-aware NEUTRAL test; intraday outcomes for sessions with truncated data
are `NOT_EVALUABLE` and never scored.

## Owner commands (run locally; not run by Claude)

PowerShell, from `H:\Guru\Learning\StockGLPM\app`. Set `$DB` to your GPLM database
(default home `H:\Guru\Learning\StockGLPM\state` → `$DB = "H:\Guru\Learning\StockGLPM\state\gplm.sqlite"`).

1. Full test suite

   ```powershell
   $env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m pytest -q
   ```

2. Bounded real-data smoke (one stock, daily lane, three steps, explicit session)

   ```powershell
   .\windows\gplm.cmd daily --stocks RELIANCE --device cpu --no-intraday --max-steps 3 --as-of 2026-11-13
   .\windows\gplm.cmd predictions --stocks RELIANCE --mode live --session 2026-11-13
   .\windows\gplm.cmd predict --stock RELIANCE --as-of 2026-11-10 --timeframe 1d --horizon 1
   ```

   Expect: no `LIVE` (no approved model; and without a registered calendar `CALENDAR_UNKNOWN`), statuses
   `SHADOW`/`UNAVAILABLE` with reasons. Upgrading starts a new `gplm-rolling.v2` replay; a full
   `gplm daily` without `--max-steps` re-processes the rolling window (heavy — run when convenient).

3. No stale `LIVE` outputs, and 4. horizon/target consistency (one read-only verifier)

   ```powershell
   $env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m stock_gplm.app.m59a_verify --db $DB
   ```

   Expect exit code 0 and every `violations` count 0: no v2 `LIVE` row that is not gate-passed, FRESH and
   complete; no actionable row without stop/target; no stored target contract that disagrees with
   horizon/target; no M5.8 v4 reveal scored with bars held ≠ horizon. `info` counts legacy rows (kept,
   never modified; legacy `LIVE` rows are reported as `LEGACY_UNVERIFIED_LIVE`).

5. Weekly holiday handling (needs a registered calendar in the config, section `calendar`)

   ```powershell
   .\windows\gplm.cmd predict --stock RELIANCE --as-of 2026-10-01 --timeframe 1w --horizon 1
   ```

   Pick an `--as-of` that is the last session of a holiday-shortened week. Expect `decision_session` = that
   session and `target_session` = the close of the next week's last listed session. Without the calendar
   section the command must fail with `CALENDAR_UNKNOWN` and freeze nothing.
