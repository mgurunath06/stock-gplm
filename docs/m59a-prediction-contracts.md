# M5.9A prediction and output contracts

Status: implemented on `feature/m55-gplm-app` (2026-11-16). Code: `src/stock_gplm/app/contracts.py`,
`session_calendar.py`, `targets.py`; applied in `m58_prediction.py` (contract
`stock-gplm.m58-prediction.v4`) and `rolling.py` (engine `gplm-rolling.v2`). Output contract id:
`stock-gplm.prediction-output.m59a.v1`.

## 1. Status vocabulary (one meaning everywhere)

| Status | Meaning | Actionable? |
| --- | --- | --- |
| `RESEARCH` | Frozen research forecast (`gplm predict`, `prediction-walk-forward`). Scored only by a separate reveal. | Never |
| `REPLAY` | Produced on a historical bar during sequential replay. Evidence about the method, never a current opportunity. | Only as a simulated research trade (see §6) |
| `SHADOW` | Latest eligible completed bar, safe inputs, but no approved immutable model artifact (or an unapproved strategy/rule). Adaptive refits are always here. | Never |
| `LIVE` | Latest eligible completed bar **and** fresh, complete, non-quarantined data **and** a recorded approval of the immutable model artifact (plus approved strategy where one is involved) **and** an eligible universe **and** the full opportunity contract **and** every gate passed. | Yes (only status that can be) |
| `NO_TRADE` | Inputs and model acceptable, but an action gate or the opportunity contract failed. | Never |
| `UNAVAILABLE` | Nothing may be shown: `STALE_DATA`, `INCOMPLETE_SESSION`, `MISSING_EXPECTED_BAR`, `CALENDAR_UNKNOWN`, `QUARANTINED_INPUT`, `END_OF_SESSION`. Always carries a reason code. | Never |

Storage: `predictions.mode` is `HISTORICAL_FROZEN` (M5.8), `REPLAY`, `SHADOW` or `LIVE`;
`mode='LIVE'` is written only when the status is `LIVE`. The latest-bar outputs that fail the gate
are stored with `mode='SHADOW'` and `payload.status` ∈ {`SHADOW`, `NO_TRADE`, `UNAVAILABLE`} plus
`payload.status_reasons`.

Legacy mapping (rows are never rewritten):

| Legacy | Read as |
| --- | --- |
| `mode='HISTORICAL_FROZEN'` | `RESEARCH` |
| `mode='LIVE'` from engine `gplm-rolling.v1` (no `payload.status`) | `LEGACY_UNVERIFIED_LIVE` — never actionable in reports |
| `direction='UNCERTAIN'` | abstention (confidence), **not** a price class |
| M5.8 `actual_direction='FLAT'` (v1–v3) | `NEUTRAL` with a zero band |
| M5.8 contracts v1–v3 | excluded from v4 metrics; counted as `legacy_non_comparable_predictions` |

## 2. Movement vocabulary and the neutral band

Realized movement is `UP`, `DOWN` or `NEUTRAL` against a versioned band (`neutral-band.v1`):

* `UP` if gross > band; `DOWN` if gross < −band; `NEUTRAL` if −band ≤ gross ≤ band (the boundary is NEUTRAL).
* Config `prediction.neutral_band` = `{"mode": "cost_aware" | "fixed", "floor_bps": 0, "cost_multiplier": 1.0}`.
  Default `cost_aware`: band = max(floor, multiplier × configured round-trip cost). With zero costs the
  band is 0 (only an exactly flat move is NEUTRAL).
* The band is frozen into each prediction (`payload.neutral_band`) and the reveal uses the frozen band.
* NEUTRAL outcomes are not directional evidence: `correct` and `brier` are NULL, `up` is NULL; they
  are counted as `neutral_outcomes`. Models and calibration are fitted on UP/DOWN rows only
  (P(UP | not NEUTRAL)).
* Confidence is separate: `payload.confidence_status` = `CONFIDENT` | `ABSTAIN`; `UNCERTAIN` is only the
  abstention token.

## 3. Exact target-horizon contract

The target of an `h`-bar forecast is the `h`-th eligible completed bar after the decision bar,
derived (never trusted) in this order (`targets.derive_target`):

1. intraday session grid (09:15–15:30 IST, anchored at 09:15; a short final bucket is a real bar);
2. the registered session calendar (daily sessions, ISO weeks, next-session intraday bars);
3. research replay only: the observed sequence of eligible bars already in the data (existence only).

An explicit `--target` must equal the derived target exactly; otherwise `TARGET_HORIZON_MISMATCH`
is raised and nothing is frozen. Stored: `payload.target_contract` = decision bar, declared horizon,
derived target, derivation; the evaluation stores the reveal bar and `bars_held`. A reveal whose
eligible bars between decision and target ≠ horizon is recorded as `NOT_COMPARABLE` (never re-targeted).
Labels: daily `YYYY-MM-DD`; weekly and intraday ISO-8601 IST timestamp of the bar end.

## 4. Weekly bars

A weekly bar is final (`incomplete=False`, `week_status='COMPLETE'`) only when the registered calendar
lists every day of its ISO week and every listed session is present. Otherwise `PARTIAL` (week still
open), `MISSING_EXPECTED_BAR`, `CALENDAR_CONFLICT` or `CALENDAR_UNKNOWN`. Holiday-shortened weeks are
readable at the close of their real last session. Friday is never assumed. Without a calendar, weekly
bars are never decision/target bars (the legacy Friday rule survives only as the non-authoritative
`avail_ns` for higher-timeframe feature alignment).

## 5. Freshness

Per symbol, against the requested/as-of exchange session — never against other symbols in the batch
(`contracts.evaluate_freshness`, `daily.symbol_freshness`):

* expected session = `--as-of` (confirmed or moved to the previous session by the calendar), or, without
  `--as-of`, from the registered calendar and the IST clock; no calendar and no `--as-of` → `CALENDAR_UNKNOWN`.
* last session < expected → `STALE_DATA`; an `ERROR` data issue on the last session → `QUARANTINED_INPUT`;
  owner-asserted session without calendar confirmation → `CALENDAR_UNKNOWN` (never LIVE).
* data running past the as-of session is historical replay (`REPLAY`), not staleness.
* intraday: inside a session in progress, the latest completed grid bar is expected; an older latest
  bar is `STALE_DATA`. The current partial candle is never a decision bar.
* reports: `live_forecasts(session=...)` shows only outputs for that session; an older stored forecast is
  `UNAVAILABLE` / `STALE_DATA`.

## 6. Actionable opportunity contract

`actionable=1` requires: action, entry/trigger, invalidation, stop-loss, ≥1 target, horizon,
probability, expected return, model version (artifact id + sha256), strategy version where applicable,
timestamp and freshness status (`payload.opportunity`). `AuditDB.add_prediction` rejects an actionable
row that lacks any of them. Nothing is fabricated: M5.8 research forecasts have no stop/target/
invalidation and are therefore always `actionable=0`; the former trade gate is kept as
`payload.research_trade_gate_passed` (`RESEARCH_TRADE` in walk-forward tables). In `REPLAY`,
`actionable=1` means "would have passed every gate with a full opportunity contract at that historical
bar" (used for simulated trade evaluation); it is never shown as current.

## 7. Model approval (minimal, fail-closed)

> **M5.9B update:** approval, revocation, expiry, the artifact registry, calendar/universe registries, the
> strategy lifecycle and the canonical `live_decision` gate are specified in `docs/m59b-governance.md`.
> The text below describes the M5.9A state.

Schema v2 adds append-only `model_approvals(model_id, artifact_sha256, approved_by, approved_utc,
scope_json, evidence_json, run_id)`. `AuditDB.model_approval(id, sha)` returns an approval only when the
id, the digest and the stored artifact all match; a kind string such as `logit` is never approved.
`AuditDB.record_model_approval` requires an approver and evidence and is never called by the program;
there is no approval CLI yet. Rolling refits create a new artifact every step and are therefore `SHADOW`.
The static approved champion is a future lifecycle item (see the M5.9A handover).

## 8. Intraday end of session

`prediction.intraday_end_of_session`: `unavailable` (default) or `next_session_first_bar`. The rolling
engine supports only `unavailable` (its models are trained on in-session labels); the session's final
completed bar reports `UNAVAILABLE` / `END_OF_SESSION`. M5.8 `predict` honours `next_session_first_bar`
when the calendar (or, in research replay, observed data) establishes the next session.

## 9. Registered calendar configuration

```json
"calendar": {"payload": "<path to stock-gplm.nse-calendar.v1 payload>",
             "manifest": "<path to its checksum-bound manifest>"}
```

Imported with `stock_gplm.m2.reference_data.import_nse_calendar_v1`; a configured but invalid calendar
raises. Dates the calendar does not list are `UNKNOWN`.

## 10. Database and compatibility

* Schema v1 → v2 is additive (new table only), applied on open, recorded as `SCHEMA_MIGRATED`.
* No prediction or evaluation row is updated or deleted; the hash chain is unchanged.
* M5.8 v4 and rolling v2 have new natural keys / replay ids, so resumed jobs never mix contracts.
  Re-running `gplm daily` after upgrading starts a new v2 replay (the v1 replay and its rows remain).
