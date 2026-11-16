# M5.9A handover — prediction correctness and safe output contracts

Date 2026-11-16 (Claude). Local only: not pushed, merged or deployed.

| Item | Value |
| --- | --- |
| Branch | `feature/m55-gplm-app` |
| Starting commit | `c587cf3` |
| Commits | `277dceb` contracts, weekly completeness, exact targets, neutral band, M5.8 v4, DB v2 · `a90dfbb` freshness, safe LIVE, intraday latest bar, rolling v2, report session · docs/verifier commit (`git log -3`) |
| Untouched pre-existing files | `.git.wsl-backup`, `Stock GPLM _ Product Requirements Document.md`, `config/app/gplm.windows.json` (untracked; left as found) |

## Files changed

New: `src/stock_gplm/app/contracts.py`, `session_calendar.py`, `targets.py`, `m59a_verify.py`;
`tests/test_m59a_prediction_contracts.py`, `tests/test_m59a_live_freshness.py`;
`docs/m59a-prediction-contracts.md`, `docs/m59a-acceptance.md`, this file.

Modified: `src/stock_gplm/m54/timeframes.py` (weekly completeness), `src/stock_gplm/app/lanes.py`
(`all_bars(calendar=)`), `db.py` (schema v2, approvals, actionable validation), `m58_prediction.py` (v4),
`rolling.py` (v2), `daily.py` (freshness, `--as-of`), `report.py` (session, statuses), `cli.py`
(`predict --target` optional, `daily/replay --as-of`, `predictions --session`);
`tests/test_m58_prediction.py`, `tests/test_app_gplm.py` (intended contract changes);
`docs/m55-gplm-application.md` (LIVE/target wording).

## Tests executed (synthetic, focused)

79 passed, 1 skipped (CUDA) across the M5.9A, M5.8, app, feature-engine, Stock-DNA, M5.6, M5.7 and
ML-discovery modules, plus the verifier test (contracts file 18 passed). `tests/test_m54_unified.py`:
4 failures **pre-existing at `c587cf3`** (`KeyError: '4h'`, `m54/unified.py:794`), unrelated.
Full suite not run. Details and the defect matrix: `docs/m59a-acceptance.md`.

## Owner commands

See `docs/m59a-acceptance.md` §Owner commands: full suite, bounded real-data smoke, the read-only
verifier `python -m stock_gplm.app.m59a_verify --db <gplm.sqlite>` (stale LIVE + horizon/target), and the
weekly holiday check.

## Behaviour changes the owner will notice

* No output is `LIVE` until (a) a registered NSE calendar is configured (`"calendar": {"payload", "manifest"}`)
  or `--as-of` is given, (b) the data is fresh, and (c) an approval of the exact model artifact is recorded.
  There is no approval CLI yet, so today every latest-bar forecast is `SHADOW` / `NO_TRADE` / `UNAVAILABLE`.
* Without a calendar, weekly forecasts are refused (`CALENDAR_UNKNOWN`) and the latest daily bar's target
  is `UNAVAILABLE:CALENDAR_UNKNOWN` (historical replay bars still derive targets from observed data).
* Rolling engine v2 starts a new replay id; the next full `gplm daily` reprocesses the rolling window
  (heavy; owner-run). v1 rows remain and are reported as legacy.
* M5.8 walk-forward "Trade" column shows `RESEARCH_TRADE` (research gate) — never `TRADE`.
* Cost-aware NEUTRAL band: with the configured delivery costs (~39 bps round trip) small moves are NEUTRAL.

## Unresolved / deferred

1. **Model approval lifecycle**: only the fail-closed table and lookup exist. Needed: `gplm model approve
   --model-id --sha256 --evidence` with owner identity, a static approved champion artifact that is *not*
   refitted, and LIVE predictions issued from that frozen artifact (rolling refits stay research/SHADOW).
2. **Strategy approval**: library `active` is not approval; strategies/rules are always `SHADOW` at the latest bar.
3. **Registered calendar**: none is configured in this repository; obtaining it follows the M3.6 manual,
   browser-downloaded evidence rule (never scripted NSE downloads).
4. M5.8 intraday training labels (`_labelled_rows`) are positional and may cross sessions; targets are now
   grid-exact, but label/target alignment for intraday training should be made session-aware.
5. Rolling intraday `next_session_first_bar` is not supported (models trained on in-session labels only).
6. Dashboard calls `live_forecasts` without a session (shows newest stored outputs with their recorded status).
7. Pre-existing `test_m54_unified.py` `'4h'` KeyError.

## M3.9-B corporate-action integration point (not integrated)

`stock_gplm.m39b` is an in-memory, dependency-free library (`adjust_series`, `detect_suspicious_discontinuities`,
validated `CorporateActionEvent` with licence status). The application today only flags large open gaps in
`app/ingest.py` (`"corporate action UNKNOWN"`) and quarantines them through `m54.unified._blocked` →
`lanes.build_lane` (`blocked` → `no_signal` for `quarantine_bars`).

Safest bounded hook for the next milestone: in `lanes.build_lane`, accept an optional set of *registered,
VALIDATED* M3.9-B events for the symbol and (1) OR their ex-date bar indices into `blocked` (same
quarantine window), and (2) surface an unvalidated/UNKNOWN event within the quarantine window of the
latest bar as `QUARANTINED_INPUT` in `daily.symbol_freshness`. Adjusted prices (`adjust_series`) must be
a separate derived lane with its own fingerprint, never a rewrite of raw candles. This is not a trivial
canonical hook today (no event registry/loader exists in the app), so it was left out of M5.9A.

## Recommended next milestone

**M5.9B — Approved champion artifact and LIVE issuance**: approval CLI and evidence record; a static,
non-refitted approved model artifact per lane/horizon; LIVE issuance from it on the latest eligible bar;
registered calendar configuration and validation command; session-aware intraday training labels; then
the M3.9-B quarantine hook above.

## Exact next step

Owner: run `docs/m59a-acceptance.md` commands 1–3 locally. Developer: implement `gplm model approve`
against `AuditDB.record_model_approval`, then a frozen-champion predictor that reuses `contracts.live_gate`.
