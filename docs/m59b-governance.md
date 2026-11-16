# M5.9B — calendar registration, approval controls and fixed-model LIVE forecasting

Status: implemented on `feature/m55-gplm-app` (2026-11-16), local only. Code: `src/stock_gplm/app/governance.py`
(registries), `fixed_model.py` (fixed approved-model execution), `contracts.py` (`live_decision`,
`shared_status`), `session_calendar.py`, `db.py` (schema v3), `rolling.py`, `daily.py`, `cli.py`,
`m59a_verify.py`. Decision contract id: `stock-gplm.live-decision.m59b.v1`.

**This is a control framework, not operational readiness.** No real calendar, universe, model artifact,
approval or strategy evidence has been registered by the developer.

## 1. Storage (schema v3, additive)

A v1/v2 database is upgraded in place on the first writable open (`SCHEMA_MIGRATED` event). New tables
only; each has `no_update` / `no_delete` triggers; every write also appends a hash-chained `events` row.

| Table | Content |
| --- | --- |
| `calendar_registrations` | calendar_id, exchange, source filename, SHA-256, registered UTC, coverage start/end, schema, timezone, validated day table, registered_by |
| `calendar_events` | ACTIVATE / DEACTIVATE (actor, note, time) |
| `model_artifacts` | model_id, sha256, family, scope, timeframe, horizon, feature schema, training cutoff, neutral-band / cost-policy / code-contract versions, creation and registration time, stored path, evidence refs |
| `model_approvals` (M5.9A table, reused) | approval of (model_id, sha256): approver, time, scope + optional `expires_utc`, evidence refs; `approval_id = approval:<rowid>` |
| `model_revocations` | revocation of (model_id, sha256): actor, time, reason |
| `strategy_lifecycle` | per (strategy_id, version) stage events with actor, evidence refs, note |
| `universe_registrations` / `universe_events` | membership file (hash, as-of date, symbols) / ACTIVATE, DEACTIVATE |
| `universe_supplements` | ADD / REVOKE of one symbol with actor and reason |

Statuses (`ACTIVE`, `CANDIDATE`, `APPROVED`, `EXPIRED`, `REVOKED`, lifecycle stage) are **derived** from
the append-only rows, never stored as mutable columns.

## 2. Calendar registry

Input: an owner-supplied local CSV, never downloaded or scraped.

```
exchange,date,status,open,close,timezone,note
NSE,2026-03-09,OPEN,09:15,15:30,Asia/Kolkata,
NSE,2026-03-10,CLOSED,,,Asia/Kolkata,Holi
NSE,2026-03-14,CLOSED,,,Asia/Kolkata,weekend
NSE,2026-11-08,SPECIAL,18:00,19:00,Asia/Kolkata,Muhurat
```

Validation: one exchange and one timezone per file (NSE ⇒ `Asia/Kolkata`); `status` ∈ OPEN, SPECIAL,
PARTIAL, CLOSED; sessions need `open < close`; CLOSED rows have no times; **every date between the first
and last row must be listed exactly once** (gaps, duplicates and conflicting rows are errors). Weekends
and holidays must therefore be listed explicitly — nothing is inferred from weekdays. Coverage = first..last
date; any date outside it is `UNKNOWN`.

`calendar_id = cal-<exchange>-<sha256[:16]>`. Registration is idempotent per content and does not activate.
Activation is explicit and supersedes the exchange's previous active calendar; deactivation leaves no
calendar (no fallback).

Use: `load_registered_calendar(settings, db)` returns the active registry calendar (it alone carries a
`calendar_id`, which `LIVE` requires). The M5.9A M2 configuration (`"calendar": {payload, manifest}`) still
serves research when no registry calendar is active, but cannot yield `LIVE` (`CALENDAR_NOT_REGISTERED`).
Registered close times drive `expected_completed_session` (partial sessions close early). Weekly
completeness, daily targets and freshness all read the same object. Known simplifications: intraday grids
remain 09:15–15:30 and weekly target labels use 15:30 even for a partial final session (pre-existing in
`targets.py`); fixed-model execution is daily-only, so neither affects `LIVE`.

## 3. Model-artifact registry

An artifact is an existing `DirectionModel` file (`.npz`, loaded with `allow_pickle=False`) — e.g. a model
the owner's rolling run already saved (`--from-model-id` pre-fills path, horizon, timeframe, feature schema
and cutoff from the `models` row). Registration loads and schema-checks it (family `logit`/`mlp`, fitted
preprocessing, consistent parameters, declared horizon = stored horizon), copies the bytes to
`<home>/model-artifacts/<model_id>.npz` (read-only), and records metadata. `model_id = fm-<sha256[:24]>`:
any byte change is a different artifact. M5.9B accepts `timeframe = 1d` only.

States: `CANDIDATE` (registered) → `APPROVED` (explicit owner action with approver + ≥1 evidence reference,
optional expiry) → `EXPIRED` (derived, past expiry) / `REVOKED` (final). Approval is refused for a missing,
altered (hash), unreadable or schema-incompatible stored file, a revoked artifact, or without approver or
evidence. Nothing approves automatically. Known limitation: the M5.9A `model_approvals` key is
(model_id, sha256), so an expired approval cannot be renewed for the same bytes; a renewal needs a new
artifact (re-saved file) and a new approval.

## 4. Fixed approved-model execution (`fixed_model.py`)

`predict_fixed` / `gplm model predict`: re-hash and re-load the stored artifact; check feature schema
(= lane `feature_config_sha`), column range, timeframe, cost-policy version (= hash of the configured
`costs`), neutral-band version and symbol scope. A revoked, expired, altered or incompatible artifact is
**refused** (no forecast computed, `FIXED_MODEL_REFUSED` event). Otherwise it applies `DirectionModel.predict`
(pure NumPy) to the latest eligible completed bar: no fit, update or calibration (probability is the raw
artifact output, `calibration = NONE`); identical inputs → identical output. Stop/target come from the
configured ATR geometry (`prediction.stop_atr`/`target_atr`); action gates as in the rolling engine
(LONG only, `min_probability`, positive R-multiple, flagged bars). The output is classified by
`live_decision` and stored with `model_id`, `approval_id`, the decision and its evidence. An unapproved
`CANDIDATE` runs and is stored as `SHADOW`.

The adaptive rolling engine never uses this path. Its model outputs pass the gate with
`adaptive_model=True` and no model evidence, so they are at most `SHADOW`
(`ADAPTIVE_MODEL_NOT_LIVE`, `MODEL_NOT_APPROVED`), whatever approval rows exist.

## 5. Strategy lifecycle

`DRAFT → VALIDATED → BACKTESTED → PAPER_TESTED → APPROVED → RETIRED` on the canonical `strategies` store
(`library.Library`, the store the rolling engine evaluates); no new strategy registry. A version without
events is `DRAFT` (legacy rows are not rewritten). Transitions are strictly sequential; `RETIRED` is
reachable from any stage, needs a reason and is terminal. VALIDATED..APPROVED each require evidence
references; APPROVED also requires recorded evidence for the three preceding stages and a classified
origin. Approval is per version. No backtest or paper trade is run by these commands — they record
references to evidence produced elsewhere.

Origins (read-only mapping, stored `origin` unchanged):

| Stored origin | Top level | Subtype |
| --- | --- | --- |
| `REFERENCE`, `CATALOGUE_ORIGINAL` | `USER_INPUT` | `CATALOGUE` |
| `CATALOGUE_DERIVED` (or any user origin with a parent version) | `USER_INPUT` | `DERIVED` |
| `USER`, `USER_DEFINED`, `import:<file>` | `USER_INPUT` | `MANUALLY_AUTHORED` |
| `AI_GENERATED` | `ML_GENERATED` | `AI_ASSISTED` |
| `ML_DISCOVERED`, `DISCOVERED` | `ML_GENERATED` | `ML_DISCOVERED` |
| anything else | `UNCLASSIFIED` (cannot be approved) | stored value |

`AI_GENERATED` (LLM-proposed) is classed as machine-generated; this is a classification choice the owner
may revisit — it does not change any gate. Rolling-engine `RULE` sources (FDR-selected rules found on the
fly) have no lifecycle and therefore can never be `LIVE`.

## 6. Live-alert universe

Owner file: a CSV with a `Symbol` column (e.g. an index constituent list) or one symbol per line;
duplicates and invalid symbols are errors. `universe_id = uni-<name>-<sha256[:16]>`, membership as-of date
required, explicit activation (one active universe). Supplements: explicit `ADD` / `REVOKE` per symbol with
approver and reason. A symbol is eligible only when a universe is active and it is a member or an active
supplement. Research is never restricted by this check. Point-in-time history, BSE and ETFs are out of scope.

## 7. Canonical LIVE gate (`contracts.live_decision`)

One function decides; the fixed-model path and the rolling engine both call it, and `AuditDB.add_prediction`
refuses any `mode='LIVE'` row whose payload lacks an eligible `live_decision`. Order (most fundamental
failure decides the status):

1. `UNAVAILABLE` — freshness not FRESH (`STALE_DATA`, `QUARANTINED_INPUT`, `INCOMPLETE_SESSION`,
   `MISSING_EXPECTED_BAR`, `CALENDAR_UNKNOWN`), or decision/target session outside the calendar's coverage
   (`CALENDAR_UNKNOWN`), or no derivable target.
2. `REPLAY` — not the latest eligible completed bar (`NOT_LATEST_BAR`).
3. `SHADOW` — `CALENDAR_NOT_REGISTERED`, `ADAPTIVE_MODEL_NOT_LIVE`, `MODEL_NOT_APPROVED`, `MODEL_REVOKED`,
   `MODEL_EXPIRED`, `MODEL_HASH_MISMATCH`, `MODEL_SCHEMA_INCOMPATIBLE`, `STRATEGY_NOT_APPROVED`,
   `STRATEGY_RETIRED`.
4. `NO_TRADE` — `UNIVERSE_NOT_ELIGIBLE`, `OPPORTUNITY_INCOMPLETE:<fields>`, `VERSION_UNKNOWN:cost_policy|neutral_band`,
   `GATES_FAILED` (+ the blocker texts).
5. `LIVE` — none of the above.

Returned: `eligible`, `status`, `reasons`, `evidence` (`calendar_id`, `model_id`, `artifact_sha256`,
`approval_id`, `strategy`, `strategy_stage`, `strategy_lifecycle_seq`, `universe_id`, `universe_basis`,
`freshness`, `cost_policy_version`, `neutral_band_version`) and `prediction_class`:
`MODEL_ONLY` (informational model forecast; a model artifact is mandatory) or `STRATEGY_TRIGGERED`
(an approved strategy is mandatory; a declarative rule may have no model). `live_gate` (M5.9A) is now a
fail-closed wrapper.

Actionable contract (DB-enforced for `actionable=1`): action (a trade direction), entry/trigger,
invalidation, stop, ≥1 target, horizon, probability, expected return, model artifact/version, strategy
version when strategy-driven, decision timestamp, freshness status (`FRESH`, or `HISTORICAL_REPLAY` for
simulated replay trades). Missing values are never filled; the output becomes non-actionable with reasons.

## 8. Shared status vocabulary and legacy mapping

Statuses `RESEARCH`, `REPLAY`, `SHADOW`, `LIVE`, `NO_TRADE`, `UNAVAILABLE` keep the M5.9A meanings
(`docs/m59a-prediction-contracts.md` §1); movement outcomes stay `UP`, `DOWN`, `NEUTRAL` (disjoint set).
`contracts.shared_status(mode, payload)` reads any stored row without rewriting it:

| Stored | Read as |
| --- | --- |
| `payload.status` ∈ the six statuses | that status |
| `payload.status` anything else | `UNKNOWN_STATUS` |
| `mode='HISTORICAL_FROZEN'`, no status | `RESEARCH` |
| `mode='REPLAY'`, no status | `REPLAY` |
| `mode='LIVE'`, no status (rolling v1) | `LEGACY_UNVERIFIED_LIVE` (never actionable) |
| `mode='SHADOW'`, no status | `SHADOW` |
| any other mode | `UNKNOWN_STATUS` |

`direction='UNCERTAIN'` remains the abstention token and M5.8 v1–v3 `FLAT` reads as `NEUTRAL` (M5.9A §1).

## 9. Read-only verifier

`python -m stock_gplm.app.m59a_verify --db <gplm.sqlite>` (opens `mode=ro`, repairs nothing; exit 1 on any
violation). M5.9B violations, judged only for rows claiming the M5.9A+ contract: `stale_live`,
`live_without_canonical_decision`, `live_without_approved_model` (approval must predate the row),
`live_with_revoked_model` (revoked before the row), `live_with_altered_model` (stored file re-hashed),
`strategy_live_without_approval`, `live_outside_universe`, `live_outside_calendar`,
`actionable_missing_fields`, `unknown_status_rows`. Info: shared-status distribution,
`legacy_unverified_live`, contract-version distribution, altered artifacts, legacy actionable rows with
missing fields.

## 10. CLI

```
gplm calendar validate|register --file F | list | show|activate|deactivate --calendar-id ID [--by NAME] [--note T]
gplm universe register --file F --name nifty200 --as-of DATE | activate|deactivate --universe-id ID | list
gplm universe supplement-add|supplement-revoke --stock SYM --reason T | check --stock SYM
gplm model register (--file F | --from-model-id ID) [--scope-symbols A,B | --shared] [--evidence REF]...
gplm model list | inspect --model-id ID | approve --model-id ID --evidence REF... [--expires DATE] | revoke --model-id ID --reason T
gplm model predict --stock SYM --model-id ID [--as-of DATE]
gplm strategies lifecycle ID --version N | advance ID --version N --to STAGE --evidence REF... [--note T]
```

Owner identity: `--by NAME` or config `governance.approver`; never guessed. Writes take the writer lock and
create a run record; reads are lock-free (a pre-v3 database must first be migrated by any write command).
