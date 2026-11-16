# M5.9B handover — calendar registration, approval controls and fixed-model LIVE forecasting

Date 2026-11-16 (Claude). Local only: **not pushed, merged or deployed**.

| Item | Value |
| --- | --- |
| Branch | `feature/m55-gplm-app` |
| Starting commit | `3e1169b` (M5.9A docs) |
| Commits | `fbb13cf` canonical gate, status mapping, schema v3, LIVE DB guard · `79b9463` calendar/universe/model/strategy registries · `97b06eb` fixed-model execution, rolling via gate, CLI · `0c921f6` verifier + tests · docs commit (`git log -1`) |
| Untouched pre-existing files | `.git.wsl-backup`, `Stock GPLM _ Product Requirements Document.md`, `config/app/gplm.windows.json` (untracked; left as found) |
| Tests executed | 8 of the 10-case budget, all passed (`docs/m59b-test-budget.md`) |

## 1. Implemented control framework

* Canonical LIVE decision `contracts.live_decision` (eligible, status, reason codes, evidence ids,
  MODEL_ONLY / STRATEGY_TRIGGERED); `AuditDB` refuses `mode='LIVE'` without it.
* Schema v3 (additive, append-only): calendar, model-artifact/revocation, strategy-lifecycle and universe
  registries; derived states; hash-chained events.
* Calendar registry (owner CSV, strict coverage, explicit activation) feeding weekly/daily/freshness logic.
* Immutable model artifacts (hash-derived id, read-only store), explicit approval with evidence and optional
  expiry, final revocation.
* Fixed approved-model execution (`fixed_model.py`, `gplm model predict`): verify → infer → gate → store; no
  fitting. Rolling refits stay SHADOW at most.
* Strategy lifecycle on the canonical `strategies` store; USER_INPUT / ML_GENERATED origin mapping.
* Live-alert universe with approved supplements.
* Verifier extension (read-only) and the M5.9A compliance addendum.

Files — new: `src/stock_gplm/app/governance.py`, `fixed_model.py`, `tests/test_m59b_governance.py`,
`docs/m59b-governance.md`, `docs/m59b-acceptance.md`, `docs/m59b-test-budget.md`,
`docs/m59a-compliance-addendum.md`, this file. Modified: `contracts.py`, `db.py`, `session_calendar.py`,
`daily.py`, `m58_prediction.py` (calendar source only), `rolling.py`, `cli.py`, `m59a_verify.py`,
`tests/test_m59a_prediction_contracts.py`, `tests/test_m59a_live_freshness.py`,
`docs/m59a-prediction-contracts.md` (pointer).

## 2. Synthetic verification (what was actually run)

One command, 8 explicit node IDs in `tests/test_m59b_governance.py`: `8 passed in 0.55s`. Fixtures: a
3-week synthetic calendar, a 2-feature logistic artifact, a 4-bar in-memory lane. Not executed: M5.9A tests
(four statically edited for intended contract changes), rolling-engine tests (statically reviewed), CLI
end-to-end, anything on real data.

## 3. Owner actions still required

1. Run the full suite (`docs/m59b-acceptance.md` command 1) — it is the evidence for the four edited
   M5.9A tests and for the rolling-engine regression tests.
2. Back up the database, then register and activate a real NSE calendar file (command 2).
3. Register and activate the current Nifty 200 file; add supplements you want (command 3).
4. Register a candidate artifact from a saved model, inspect it (command 4); approve only with real evidence.
5. Run the verifier (command 6) and the one-stock smoke (command 7).
6. Decide whether `AI_GENERATED` strategies should count as `ML_GENERATED/AI_ASSISTED` (current mapping) or
   as user input; and whether an uncalibrated fixed-model probability is acceptable for `LIVE` (today the
   gate requires a probability, not a calibrated one).

## 4. Real evidence not yet registered

No calendar, universe, model artifact, model approval, strategy lifecycle event or supplement exists in
any owner database. Until they do, **no output can be `LIVE`**; today every latest-bar output remains
`SHADOW` / `NO_TRADE` / `UNAVAILABLE`.

## 5. Known limitations

* Fixed-model execution is `1d` only; intraday fixed models are not supported.
* An expired approval cannot be renewed for identical bytes (M5.9A table key); re-save and re-register.
* Partial sessions: intraday grid and weekly target labels still assume 15:30 (pre-existing, `targets.py`).
* The rolling engine can issue a strategy-driven `LIVE` only for an APPROVED library strategy; its
  probability is the strategy's raw (uncalibrated) win rate as the rolling engine records it, not a model output.
* Read-only CLI queries on a pre-v3 database report that a migration is needed instead of listing.
* Approver identity is an owner-supplied string (local app, no authentication).
* Pre-existing, unchanged: `test_m54_unified.py` `'4h'` failures; dashboard calls `live_forecasts` without
  a session; M5.8 intraday training labels positional (M5.9A handover items 4–7).

## 6. Later product phases still pending

Live-alert delivery and notifications, cloud data integration, broker integration, intraday signal
lifecycle, corporate-action integration (M3.9-B hook, see M5.9A handover), cross-stock ranking, shared
sector/regime models, point-in-time universes, BSE/ETF expansion, portfolio optimisation, bulk strategy
evaluation and the full 13 August benchmark.

## 7. Exact next step

Owner: commands 1, 6 and (after backup) 2–3 in `docs/m59b-acceptance.md`. Developer, next milestone:
a bounded `gplm model export` that freezes the latest owner-approved rolling fit per lane into a candidate
artifact, plus CLI-level smoke tests within a new budget.
