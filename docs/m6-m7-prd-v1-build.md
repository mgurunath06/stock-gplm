# PRD v1 build: M5.10 – M7.3 owner guide and acceptance

Date 2026-11-19 (Claude). Branch `feature/m6-prd-v1` (from `feature/m55-gplm-app` @ `c691a16`). Local only.
Roadmap: `docs/roadmap-prd-v1-DRAFT.md`. Every output remains **research decision support**: no order is ever
placed, and nothing becomes `LIVE` without the canonical M5.9B gate (registered calendar, active universe,
approved artifact or strategy, fresh data).

## What each milestone delivers

| ID | Module(s) | Command | Contract |
| --- | --- | --- | --- |
| M5.10 | `model_export.py` | `gplm model-export [--stocks …] [--approved-only] --by NAME` | `stock-gplm.model-export.m510.v1` |
| M6.0 | `costs.py`, `lanes.py`, `costs.schedule` in both configs | `gplm costs show`, `gplm costs note --qty --buy --sell [--profile] [--traded-value]` | `stock-gplm.cost-schedule.m60.v1` |
| M6.1 | `m54/timeframes.py` (`1mo`), `lanes.py` (`3m`), `indicator_store.py`, schema v4 | `gplm indicators snapshot|show` | `stock-gplm.indicator-store.m61.v1` |
| M6.2 | `dsl_structure.py`, `config/verify/primitives.json`, 4 catalogue entries unblocked | `gplm coverage`, `gplm library functions` | DSL contract unchanged (functions added only) |
| M6.3 | `horizons.py` | `gplm horizons list`, `gplm horizons labels --stock X --bucket d1_7` | `stock-gplm.horizons.m63.v1` |
| M6.4 | `benchmark.py` | `gplm benchmark --stock X [--timeframes …]` | `stock-gplm.benchmark-13aug.m64.v1` |
| M6.5 | `hybrid.py` | `gplm hybrid --stocks … --name N [--sectors FILE]`, `gplm controls …` | `stock-gplm.hybrid-model.m65.v1` |
| M6.6 | `paper.py`, `governance.advance_strategy(paper_min_trades=)` | `gplm paper enrol|run|summary --source strategy:ID@V` | `stock-gplm.paper-ledger.m66.v1` |
| M7.0 | `opportunity.py` | `gplm opportunities [--session D]` | `stock-gplm.opportunity.m70.v1` |
| M7.1 | `alerts.py` | `gplm alerts [--alert-id A]`, `gplm ops alerts` | `stock-gplm.alert-lifecycle.m71.v1` |
| M7.2 | `risk.py` | `--risk FILE` on `gplm ops …` (or config `risk.file`) | `stock-gplm.risk-inputs.m72.v1` |
| M7.3 | `ops.py`, dashboard tabs, `windows/schedule-ops.ps1` | `gplm ops pre-open|scan|post-close|intraday|alerts` | `stock-gplm.ops.m73.v1` |

CLI registration is in `cli_v1.py`, wired into `cli.build_parser`.

## Key decisions and how they work

* **Costs (M6.0).** `costs.schedule.enabled: true` compiles per-side rates from itemised charges: brokerage
  with cap, STT, exchange charge, SEBI fee, IPFT, stamp duty, GST and DP charge. Each lane's slippage
  bucket comes from the median traded value of its **discovery segment only**. The legacy flat-bps model
  applies when the schedule is absent or disabled. The cost section hash (`cost_policy_version`) changes,
  so earlier artifacts keep their own recorded policy. **Every rate is an ASSUMPTION.** Check it against
  your broker's contract note with `gplm costs note`. Fill conventions are unchanged: next-open entry,
  stop checked before target, gaps fill at the worse open.
* **Timeframes (M6.1).** Lanes now include `3m`, and `1mo` is always built from daily bars. A monthly bar
  is final only when a registered calendar proves every session is present, so a partial month is never a
  completed bar. 4h policy, unchanged and now documented: session-anchored at 09:15. That gives one 240-minute
  bar plus one 135-minute remainder bar, and missing minutes mark a bucket incomplete. Partial sessions
  still use the registered close for freshness, while weekly and intraday labels still assume 15:30
  (known limitation).
* **Indicator store (M6.1).** Each stored value records symbol, timeframe, bar, indicator, parameters,
  output, input revision, calculation version and status. Status is `OK`, `UNAVAILABLE` (stored as NULL,
  never 0) or `QUARANTINED`. Rows are append-only and idempotent. `ops scan` stores the decision bar's core
  indicators for every opportunity.
* **Primitives (M6.2).** New: `bos`, `order_block_high/low`, `vcp_contractions/depth`, `trendline`,
  `trendline_slope`, `pitchfork`, `pattern` / `pattern_level` (double top/bottom, inverse/head & shoulders,
  three triangle kinds, box), `harmonic` (Gartley, Bat, Butterfly, Crab), `pnf_direction/boxes/breakout`,
  `adf_stat` / `adf_critical`, `hmm_bull_prob`, `knn_analogue` and `meta_prob`. All are causal, and the
  test truncates the data to confirm it. Still tracked as gaps: cup-and-handle, wedges, flags, Three
  Drives and an ADF p-value surface, plus every DATA/DEFERRED row. Unblocked: C5-036 Gartley, C5-037 Bat,
  C5-050 P&F breakout and C9-311 HMM. Library state: 350 strategies, 0 invalid, 67 blocked (mostly on data
  or deferred scope).
* **Horizons (M6.3).** Seven buckets, each with a maturity rule, cost profile and evaluation method
  (`gplm horizons list`). Labels cover gross/net return, direction (cost-aware band, `NO_TRADE`), MFE/MAE,
  TARGET/STOP/TIME/gap/INDETERMINATE outcome and bars held. Status is `MATURE`, `PENDING` or `INVALID`;
  pending or invalid labels never train. Baselines are flat, always-long and base-rate Brier.
* **Benchmark (M6.4).** Covers the eligible cases defined in PRD §4. Event ordering is
  `(event_ns, REVEAL before FREEZE, timeframe, decision)`: outcomes maturing at the same instant are
  revealed as one batch, the adaptive model updates once, and only then are forecasts frozen. Frozen and
  adaptive variants run on the same inputs. A rerun must reproduce the stored result hash; this is
  recorded as `BENCHMARK_REPRODUCED` or `BENCHMARK_NOT_REPRODUCED`. Weekly and monthly targets stay
  `PENDING` until they are final.
* **Hybrid and controls (M6.5).** A shared logistic model is conditioned on timeframe, sector and regime.
  A shrunk per-stock residual is added only when the stock has at least `min_stock_rows` rows; otherwise
  the forecast falls back to the shared model and says so. Platt calibration is fitted on the time-ordered
  slice after training, and Brier and ECE are reported. Controls are append-only:
  `pause-updates`, `disable-alerts` and `rollback --target <APPROVED artifact>`. Rollback refuses any
  artifact that is not approved and hash-verified.
* **Paper (M6.6).** Paper trading is forward-only: only bars that complete after enrolment create signals.
  It is idempotent and restart-safe, with `fill_basis = PAPER`. When the caller passes `paper_min_trades`,
  `advance_strategy(..., "PAPER_TESTED")` requires enough closed paper trades and records the
  `paper:<source>:<n>` evidence reference.
* **Opportunity (M7.0).** Each record carries every PRD §7 field. Levels exist only when ATR is known.
  Timeframe conflicts lower confidence and are explained, never suppressed. Ranking score is
  `expected_net × Wilson-lower(p) / sqrt(holding)`, so thin evidence ranks lower. Status comes only from
  `contracts.live_decision`.
* **Alerts (M7.1).** Lifecycle: CANDIDATE → ARMED → TRIGGERED → EXIT, or CANCELLED / EXPIRED /
  SUPPRESSED. Alerts are deduplicated per symbol and source. An event is *current* only when its bar ended
  within `max_event_age_minutes`, so catch-up after an outage never notifies. Only LIVE alerts notify by
  default. Channels: `file` (outbox, on), plus `webhook` and `email`, which are off and use env vars only.
* **Risk (M7.2).** Optional owner JSON with capital, holdings, sectors and limits. Missing inputs are
  reported, never assumed to be zero exposure.
* **Ops (M7.3).** `pre-open` runs readiness checks. `scan`, `post-close` and `intraday` run the full
  pipeline and write a report under `<home>/reports/opportunities/`. The dashboard has new tabs:
  Opportunities, Alerts and Paper & benchmark. `windows/schedule-ops.ps1` registers the weekday tasks.

## Owner acceptance commands (Windows: `windows\gplm.cmd …`)

Back up first: `gplm audit --backup`. The first write migrates the database to schema v4 (additive only).

1. Full suite: `.venv\Scripts\python -m pytest`. This covers the statically edited M5.9A test (schema `"4"`)
   and every earlier milestone.
2. `gplm costs show` and `gplm costs note --qty 100 --buy 1000 --sell 1010`. Compare with one real
   contract note and edit `costs.schedule` where they differ.
3. `gplm coverage`, then `gplm verify`. The suite should report fewer tracked gaps than the 16 before M6.2.
4. `gplm benchmark --stock RELIANCE`. Run it twice; the second run must report `reproduced: true`.
5. `gplm horizons labels --stock RELIANCE --bucket d1_7`.
6. `gplm model-export --stock RELIANCE --by NAME`, then `gplm model inspect --model-id …`.
7. `gplm ops pre-open`, then `gplm ops scan --stock RELIANCE --max-strategies 20`. Expect `SHADOW` rows
   until calendar, universe and approvals exist. Then open `gplm dashboard --open`.
8. Optional research runs: `gplm hybrid --stocks … --name h1`;
   `gplm paper enrol --source strategy:C2-001@1 --by NAME`, then `gplm paper run …` daily.

## Limits (honest)

* No cloud or live feed (M8 is blocked on a provider decision). Intraday ops report stale data and
  suppress alerts until one exists.
* Hybrid and paper results are research evidence only. Numeric approval thresholds are still the owner's
  gate after Phase B.
* `ops scan` evaluates each strategy on the lane (optimise on discovery, read validation once), which is
  expensive across the whole library and universe. That is an owner run.
* Probabilities in opportunities are historical win rates and are labelled uncalibrated.
