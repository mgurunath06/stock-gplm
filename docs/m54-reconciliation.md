# M5.3 reconciliation and M5.4 canonical architecture (2026-11-15)

## Inputs compared

| | Claude MVP | Codex M5.3 |
| --- | --- | --- |
| Location | `stock-gplm-claude`, branch `feature/claude-five-stock-mvp` (`fee5744`, `7ecdcb0`, `76ae301`), committed | main checkout `stock-gplm`, branch `feature/m53-research-mvp` at `38973e6`, **uncommitted** (`src/stock_gplm/m53/`, `config/m53/`, v1 edits) |
| Package | `stock_gplm.mvp` (data, experiment, research, cli) | `stock_gplm.m53` (cli, recency, features) |
| Data loader | `mvp.data.load_symbol` with `pin_holdout`: verifies split plan against the M3.2 manifest and pinned hashes, opens only segments whose declared end is before the cutoff, quarantines bad symbols, checks overlap disagreement | `_load_daily`: same filename rule and pin checks; raises on the first rejected segment (no per-symbol quarantine) |
| Evaluation window | 2023-11-01 to 2024-12-31, a single window | train 2022-10-31 to 2023-06-30, validation 2023-07-01 to 2023-12-31, test 2024-01-01 to 2024-12-31 |
| Strategies | 5 V1 strategies × 3 horizons | 2 V1 strategies × 3 horizons × 5 symbols, simulated per symbol |
| Execution | V1 portfolio simulator: next-open fills, cash-bounded, up to 5 positions | V1 simulator, one instrument per portfolio |
| Costs | strategy commission/slippage plus 12.41 / 10.91 bps statutory | same assumptions |
| Extras | rolling/half-life weight interfaces (unfitted) | validation-selected recency profile applied to test trades |
| V1 changes | adds RSI, MACD (`output` field), ATR, VOLATILITY | adds RSI, MACD_LINE / MACD_SIGNAL / MACD_HISTOGRAM (`fast_period` etc.) |
| Tests | 896 passed, 2 skipped | 884 passed (reported) |

**The returns are not comparable.** The two implementations use different windows, strategy sets and portfolio
constraints (5-position portfolio vs single-instrument). Neither result is evidence of edge. Both are
`DIAGNOSTIC_ONLY_NON_INVESTABLE`.

**Direct conflict.** Both branches extend `v1/schema.py` and `v1/evaluator.py` with incompatible RSI/MACD
schemas. Merging both would create two MACD dialects. Decision: keep the committed Claude V1 extension as the
V1 baseline, and treat Codex's MACD_* types as an alternative to reconcile when Codex commits. M5.4 does not
depend on either dialect, because it has its own causal feature registry and only uses V1 for `CostModel`.

## Why M5 rejects with `HOLDOUT_BOUNDARY_UNVERIFIED` while the diagnostic paths run

`stock_gplm.m5.evidence.load_reference_evidence` builds a holdout boundary only when an **OS-governed approval
trust root** (`/etc/stock-gplm/m5-approval-trust-root.json`, M5.1.2/M5.1.3) names the accepted split plan.
Without it, `holdout is None` and the adapter rejects every request
(`src/stock_gplm/m5/adapter.py`, "no administratively governed M3.2 split plan").

The MVP, M5.3 and M5.4 loaders verify the split plan **bytes** against the M3.2 run manifest and against hashes
pinned in versioned config. That proves consistency, not administrative authority: a process that can rewrite
`reports/m32` can rewrite both files. These paths are therefore restricted to:

- data whose segment files end before the pinned cutoff (2025-10-21), opened read-only;
- outputs labelled `DIAGNOSTIC_ONLY_NON_INVESTABLE`, never `STRICT_RESEARCH`, and never promoted.

They do not grant strict status, do not touch the trust root, and do not read the protected period. Installing a
governed trust root is a T4 action that needs the user's approval. It is not done here.

## Canonical architecture chosen

M5.4 (`stock_gplm.m54`) is the single research orchestrator for stock-behaviour discovery. It reuses:

| Capability | Canonical component | Status |
| --- | --- | --- |
| Raw daily validation | `ingestion.daily.validate_daily` | reused unchanged |
| Pinned pre-cutoff loading, quarantine | `mvp.data.pin_holdout`, `load_symbol` | reused unchanged |
| Execution costs | `v1.portfolio.CostModel` | reused |
| Declarative rule backtests (portfolio) | `v1` evaluator, portfolio simulator, metrics | kept for user strategies; M5.4 does not duplicate it |
| Indicator engine for discovery | `m54.indicators` (registry, ~50 indicators, scale-free causal features) | new; V1 indicators stay for V1 strategies |
| Hypothesis grammar and search | `m54.hypotheses` | new |
| Event study, statistics, baselines | `m54.evaluate` | new; BH correction in the spirit of M3.8 |
| Orchestration, checkpoints, resume | `m54.engine`, `m54.store` | new; replaces `mvp.experiment` and `m53.cli` for discovery |
| Bulk execution | `m54.bulk` | new, same engine per symbol |
| ML datasets | `m54.ml` (purged per segment, no fitting) | new; fitting remains M3.5/M3.7 |
| LLM providers | `llm.client` (extended), `llm.envfile`, `llm.registry` | extended, not duplicated |
| Recency weighting | `m53.recency` (Codex, uncommitted) | candidate to adopt later as a finding-level diagnostic |
| Minute features | `m53.features` (Codex, uncommitted) | unused; minute data needs separate authorization |

`mvp.experiment` and `m53.cli` stay in place as historical diagnostic runners. New discovery work goes through `gplm`.

## Worktrees

- `stock-gplm-claude` (`feature/claude-five-stock-mvp`): untouched.
- `stock-gplm` main checkout (Codex's uncommitted M5.3): untouched, and read only for data and the M3.2 pin.
- `stock-gplm-m45`: untouched.
- New integration worktree `stock-gplm-m54`, branch `feature/m54-discovery`, created from `76ae301`.

## Protected boundaries observed

The inner test segment of M5.4 (2024-08-01 to 2024-12-31) is pre-cutoff, but earlier diagnostics (the Claude MVP
and Codex M5.3) already evaluated rule strategies over 2024. The inner test therefore carries researcher-exposure
risk. This is recorded in the M5.4 documentation and is one reason no M5.4 result is called validated edge.
