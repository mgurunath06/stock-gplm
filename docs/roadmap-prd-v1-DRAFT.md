# Milestone path to PRD Version 1 — DRAFT

Date 2026-11-19 (Claude). Status: **DRAFT for owner approval**. **Build status 2026-11-19: M5.10–M7.3 are built
and synthetically tested on `feature/m6-prd-v1` (see `docs/handover/m6-m7-handover.md`). M8 is blocked on the
provider decision.** Source: `Stock GPLM _ Product Requirements
Document.md` (v1, commit `c691a16`), compared against branch `feature/m55-gplm-app` at M5.9B.

Rules that apply to every milestone (CLAUDE.md / AGENTS.md):

* Developer agents build, unit-test and run **bounded** smoke tests only. Each milestone has a test-budget
  ledger (`docs/<m>-test-budget.md`) and does not rerun earlier milestones.
* The owner runs the full suite, research, bulk backtests and training locally, using commands the
  milestone delivers.
* NSE cash equity only. F&O, options, ETFs, BSE and index-relative work stay deferred.
* Strategies stay declarative (no Python per strategy). Jobs are checkpointed and resumable.
* A milestone closes only with: an acceptance doc, a handover, and owner evidence for its gate.

## Phase 0 — Close M5.9 (in progress)

| ID | Scope | Owner gate |
| --- | --- | --- |
| **M5.10** | `gplm model export`: freeze the latest owner-approved rolling fit per lane into a candidate artifact. CLI smoke tests. | Full suite green. NSE calendar and Nifty 200 registered and active. `gpu-verify.cmd` JSON kept. Answers to the two open M5.9B decisions (AI_GENERATED origin; uncalibrated probability for LIVE). |

## Phase A — Make research results trustworthy (PRD §2, §3, §6)

| ID | Scope | Exit criteria |
| --- | --- | --- |
| **M6.0 Cost and fill model** | Indian cash-equity costs as config: brokerage, STT, exchange charges, SEBI fee, stamp duty, GST, DP charge; delivery vs intraday. Slippage and spread by liquidity bucket. Gap-through entries and stops. Same-candle stop and target → conservative or `INDETERMINATE`. Simulated vs actual fill provenance. | Cost table versioned and shown in every backtest. Unit tests on hand-computed contract notes. Existing caches invalidated through a contract-version bump. |
| **M6.1 Timeframes and indicator store** | Add 3m and monthly. One documented 4h anchor and partial-session policy (replaces the 15:30 assumption in `targets.py`). Persistent indicator store keyed by symbol, timeframe and timestamp, with parameters, input revision, calculation version and quality. Insufficient history is `UNAVAILABLE`, never 0. | Policy doc. Store round-trips. Partial weekly and monthly bars are never marked complete. |
| **M6.2 Strategy library coverage** | Close the 16 tracked primitive gaps (`config/verify/primitives.json`). Import all 300 reference strategies as declarative configs; anything ambiguous or prone to leakage is rejected with a reason. Bulk-evaluation command (owner runs it). | `gplm verify` exits 0. 300/300 imported or explicitly rejected. Owner bulk run report. |
| **M6.3 Horizons and targets** | The seven PRD horizon buckets (intraday … 6–12 months), each with a maturity rule, cost assumption and evaluation method. Targets: gross/net return, direction or no-trade, MFE/MAE, target/stop/time-exit outcome, holding period. Simple baselines per horizon. Stock-specific evidence kept separate from universe-wide evidence. | Horizon spec doc. Labels stay pending until mature (tested). Baselines recorded with sample sizes. |

## Phase B — Prove the process (PRD §4, §5, §6)

| ID | Scope | Exit criteria |
| --- | --- | --- |
| **M6.4 13 Aug 2026 benchmark harness** | Permanent, reproducible benchmark: next-candle forecasts for every eligible candle and timeframe, frozen before outcomes are revealed. Outcomes processed as event-time batches with a documented ordering. Frozen and adaptive runs reported side by side. Weekly and monthly targets scored only when they actually close. | Two runs produce identical outputs (same hashes). Leakage checks pass. Owner runs the full benchmark. |
| **M6.5 Hybrid models and checkpoints** | Shared base model conditioned on stock, sector, timeframe and regime. Stock-specific model or residual layer when evidence is sufficient, falling back to the shared model otherwise. Calibration layer. Checkpoint restore, rollback to a static approved fallback, manual disable and pause controls. | Calibration reported (Brier/ECE). Rollback tested. Fallback disclosed on every forecast. |
| **M6.6 Paper trading** | Forward paper ledger driven by approved and candidate strategies and models. Simulated fills stored separately from any future actual fills. Paper-trading evidence becomes required for the Paper Tested → Approved lifecycle step. | Lifecycle refuses approval without paper evidence. Ledger survives restart. |

**Owner research gate after Phase B:** set numeric approval thresholds (net expectancy, drawdown,
calibration, minimum samples) and the holdout dates. Phase C may proceed without this gate, but nothing it
produces can be LIVE until the gate is set.

## Phase C — Opportunities and alerts (PRD §7, §8)

| ID | Scope | Exit criteria |
| --- | --- | --- |
| **M7.0 Opportunity record and ranking** | Opportunity record with every PRD §7 field: action (buy/hold/exit/sell holding/no-trade), rank, horizon, versions, entry zone, stop and invalidation, targets, gross and net expectation, probability and confidence, holding period, regime, per-timeframe forecasts and indicator values (conflicts explained, never auto-suppressed), evidence, limitations, freshness, no-trade reason. Cross-stock ranking over the alert universe. | Explainability-coverage metric computed. Each no-trade or rejection carries a reason code. |
| **M7.1 Alert lifecycle and notifications** | States: candidate → armed → triggered / cancelled / expired → exit. Deduplication; original snapshots kept. Stale-data suppression, with catch-up that never replays old alerts as current. Notification channels (owner chooses: desktop, email, Telegram, other). | State-machine tests. Stale-feed suppression test. One channel delivered end to end. |
| **M7.2 Basic risk inputs** | Optional holdings, capital, position size, stock and sector exposure limits. Missing inputs are marked, never assumed to be zero exposure. Conflicting or excessive alerts suppressed. | Unit tests on limit checks. |
| **M7.3 Daily operation** | Post-close, pre-open and intra-session schedules (Windows Task Scheduler). Dashboard views: data quality, sync status, replay, strategy performance, model versions, alert history, indicator evidence, no-trade reasons. Accessibility requirements (text alongside colour, keyboard controls). | Owner runs one full paper session day. |

## Phase D — Cloud data (PRD §1) — blocked on owner decision

| ID | Scope | Blocker |
| --- | --- | --- |
| **M8.0 Provider and licence** | Choose the market-data provider (Shoonya is a candidate, not approved). Record entitlements, redistribution rights and rate limits. | Owner decision and licence. |
| **M8.1 Cloud collector and sync** | Cloud collects historical and live data. Authenticated, encrypted, resumable sync to the PC without duplicates (builds on M2.4 secure sync). Gap and staleness reporting. Corrections preserve original decision snapshots. | M8.0. |
| **M8.2 Live intraday loop** | Completed-candle processing from the live feed into M7.x. | M8.1 and M7.1. |

M8.0 can be decided at any time. Until then, Phase C runs on end-of-day and local minute data.

## Deferred (post-V1 or per CLAUDE.md)

Point-in-time index membership and survivorship-safe universes, BSE/ETF, F&O and options, index-relative
strategies, portfolio optimisation, broker integration or execution, automatic adaptive updates to LIVE.

## Critical path

`M5.10 → M6.0 → M6.3 → M6.4 → M6.6 → (owner thresholds) → M7.0 → M7.1 → M7.3`, with M6.1, M6.2 and M6.5
alongside, and M8.x gated on the provider decision. First LIVE alert = M7.1 complete + owner thresholds
+ an approved model or strategy with paper evidence.
