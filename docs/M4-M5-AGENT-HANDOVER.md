# M4/M5 agent handover — 2026-11-14

This is for an independent reviewer (for example Codex) continuing or auditing
the M4/M5 work. It depends only on this repository, git and the project `.venv`;
no agent-specific session, memory or tooling is needed. Read `AGENTS.md` first:
its rules apply unchanged.

## 1. Branches, commits and worktrees

| Ref | Commit | Parent | Content |
| --- | --- | --- | --- |
| `feature/m39b-corporate-actions` | `35bdc06` | `df8324b` (M3.9-A) | unchanged base |
| `feature/v1-configurable-strategy-engine` | `0534cc8` | `35bdc06` | V1 committed exactly as found (previously uncommitted) |
| `feature/m4-v1-consolidation` | `4822d54` | `0534cc8` | M4 remediation, tests, inventory and consolidation reports |
| `feature/m5-historical-adapter` | `658782b` (code), then the tip commit (evidence docs and this handover) | `4822d54` | M5 implementation; genuine-data evidence reports |
| `feature/m25r-cloud-recovery` | `dbba2b1` | — | untouched, not merged, not activated |

| Worktree | Branch |
| --- | --- |
| `/home/mgurunath/projects/stock-gplm` | `feature/v1-configurable-strategy-engine` (moved from `feature/m39b-corporate-actions` by a non-destructive `git switch -c`; clean) |
| `/home/mgurunath/projects/stock-gplm-m45` | `feature/m5-historical-adapter` (M4 and M5 were developed here) |
| `stock-gplm-m25`, `-m25-browser`, `-m25r`, `-m3`, `-m34` | untouched; `-m25-browser` (untracked `transfer/`) and `-m34` (uncommitted M3.4 work) were dirty before this session and were left exactly as found |

Nothing was pushed, merged into `main`, rebased, reset, stashed or deleted.

## 2. Changed files

M4 (`4822d54`):
- `src/stock_gplm/v1/execution.py` (new): shared next-open and resting stop/target rules
- `src/stock_gplm/v1/backtest.py`: v2 execution policy, `CALLER_SUPPLIED_UNVERIFIED` classification
- `src/stock_gplm/v1/registry.py`: `BEGIN IMMEDIATE`, append-only triggers, idempotent backtest records
- `src/stock_gplm/v1/studio.py`: Host/Origin validation, 409 on SQLite conflicts
- `src/stock_gplm/v1/demo.py`: policy constant
- `tests/test_m4_v1_remediation.py` (new); `tests/test_v1_strategy_engine.py` (one assertion updated `…_v1` → `…_v2`, plus one classification assertion)
- `docs/m4-integration-inventory.md`, `docs/m4-consolidation-report.md` (new); `docs/v1-strategy-engine.md`, `docs/README.md`

M5 (`658782b` and the tip):
- `src/stock_gplm/m5/{__init__,models,evidence,adapter,discovery,runner,studio_api,cli}.py` (new)
- `src/stock_gplm/v1/portfolio.py`, `src/stock_gplm/v1/metrics.py` (new); `registry.py` (`find`); `studio.py` (M5 panel and endpoints)
- `config/m5/backtest-settings.example.json`, `config/m5/evidence-paths.example.json` (new)
- `tests/m5_fixtures.py`, `tests/test_m5_historical_adapter.py`, `tests/test_m5_studio.py` (new)
- `pyproject.toml` (`stock-gplm-m5` script)
- `docs/m5-historical-data-adapter.md`, `docs/m5-eligibility-audit.md`, `docs/m5-real-data-backtest-report.md`, `docs/m5-acceptance-report.md`, this file; `docs/architecture.md`, `README.md`, `docs/README.md`

Generated, git-ignored evidence lives in the primary checkout's
`reports/m5/` (discovery, eligibility audit, decisions, four run directories,
the strategy registry). It is not committed, because research artifacts stay
local.

## 3. Architecture decisions

1. **V1 was preserved before review.** `0534cc8` is the as-found state, so every
   remediation diff is reviewable on its own.
2. **One execution semantics.** `v1.execution` is used by both the legacy
   single-instrument backtester and the M5 portfolio simulator. Close decisions
   fill at the next observed open. A gap through the stop fills at the open; a
   gap through the target is capped at the target; when both are touched, the
   stop fills first.
3. **Eligibility is only produced by `HistoricalDataAdapter`.** Decisions are
   content-hashed, including `bars_sha256`. `verify()` re-derives the status from
   the failures and is called by every consumer. The Studio accepts only
   symbol, range, mode and settings; extra fields get 400.
4. **Two failure classes.** INTEGRITY failures reject in every mode. EVIDENCE
   gaps block `STRICT_RESEARCH` and are listed in `DIAGNOSTIC_ONLY`. There is no
   force or skip switch.
5. **Reuse, not reinvention.** `validate_daily`, the M3.2 split plan, the M2.1
   importers, the M2.2 master and M3.9-B are consumed as they are. The M2.5-R
   calendar is read by its JSON contract, not by importing M2.5-R. That
   compatibility was checked once against the real M2.5-R implementation on
   synthetic input.
6. **Provider price-basis gate.** Genuine Shoonya history mixes back-adjusted
   (RELIANCE 2024 bonus) and unadjusted (65 M3.8 cases) corporate actions.
   M3.9-B events are applied only on a verified `RAW_UNADJUSTED` basis, which
   currently cannot be evidenced, so real data is labelled
   `PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED`.
7. **Causal corporate actions.** With validated events, signals are re-evaluated
   per as-of regime (events effective and known by 15:30 IST on the decision
   date). Held quantities and levels are rescaled at the ex-date.
8. **Generic simulator.** It has no strategy-name logic. Whole shares, no margin,
   cash never negative, exposure cap, one position per instrument. Missing
   sessions cancel entries and defer exits, and nothing is filled in. Open
   positions at the end are not converted to trades.
9. **Immutable runs.** `run_id` = hash(strategy version and config hash, dataset
   fingerprints, settings, simulator version, git commit and dirty flag, source
   hash). Runs are staged and renamed into place, never overwritten.
   `created_at_utc` is the only non-analytical field.
10. **Ranking only for comparable strict runs.** `compare_runs` returns no
    ranking otherwise.

## 4. Test commands and results

The project tests resolve some real-data fixtures relative to the working
directory. Run the full suite from the primary checkout so they execute rather
than skip:

```bash
cd /home/mgurunath/projects/stock-gplm
PYTHONPATH=../stock-gplm-m45/src .venv/bin/python -m pytest ../stock-gplm-m45/tests
# 2026-11-14: 668 passed, 0 skipped
```

Inside the worktree only (two real-data regressions will skip there):

```bash
cd /home/mgurunath/projects/stock-gplm-m45
PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest
PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest \
  tests/test_v1_strategy_engine.py tests/test_m4_v1_remediation.py \
  tests/test_m5_historical_adapter.py tests/test_m5_studio.py   # 69 passed
```

Baseline comparison: clean `35bdc06` gives 597 passed and 2 skipped without
`data/` (619 tests); V1 adds 19, M4 adds 8 and M5 adds 42, for 668.

Note that `PYTHONPATH=src` is required in the worktree. The `.venv` has an
editable install pointing at the primary checkout's `src`.

## 5. Outstanding blockers

| Blocker | Effect | Needs |
| --- | --- | --- |
| No registered NSE calendar | `CALENDAR_EVIDENCE_MISSING` for every symbol | user downloads the documents manually; register with M2.5-R `register-calendar` |
| No listing history | `PIT_MEMBERSHIP_MISSING` | M2.1 `stock-gplm.listing-history.v1` payload and manifest |
| No symbol-change mapping | `ALIAS_EVIDENCE_MISSING` | M2.1 mappings with `COMPLETE` coverage |
| No corporate-action catalog or licensed events | `CORPORATE_ACTION_COVERAGE_MISSING` | M2.1 catalog plus M3.9-B events with `APPROVED_FOR_DERIVATION` |
| Provider price basis unknown and mixed | `PROVIDER_PRICE_BASIS_UNVERIFIED` | a new reviewed evidence contract or an unadjusted source |
| Cost schedule is an assumption | results labelled | verification against current schedules |

Result: strict eligibility is 0 of 2,701 symbols. Only diagnostic,
non-investable runs exist (see `docs/m5-real-data-backtest-report.md`).

## 6. Instructions for an independent review

Scope: `git diff 35bdc06..feature/m5-historical-adapter`, reviewed commit by
commit (`0534cc8` as found, `4822d54` M4, `658782b` M5 code, then the docs tip).

Please verify, adversarially:

1. **Look-ahead.** In `v1/evaluator.py` and `v1/portfolio.py`, no decision at
   session *t* reads data after *t*. Close decisions never fill at that close.
   Check the ordering inside `simulate_portfolio`: share events, then exits at
   the open, then entries at the open, then resting orders, then the
   mark-to-market, then decisions.
2. **Eligibility bypass.** Try to obtain a `VerifiedHistoricalDataset` or a
   strict run without `HistoricalDataAdapter.assess`: construct decisions by
   hand, mutate them, swap bars, or send extra Studio fields. Each should fail
   `verify()` or return 400. Note that Python cannot fully prevent hand-built
   objects; the invariant is that every consumer re-verifies.
3. **Holdout.** No code path opens a segment starting on or after the cutoff, or
   releases a bar dated on or after it (`adapter.assess` step 3,
   `VerifiedHistoricalDataset.verify`).
4. **Corporate actions.** `runner.instrument_signals` regime logic and
   `simulate_portfolio` share-event rescaling. Confirm M3.9-B is never applied to
   an unverified basis.
5. **Accounting.** Cash can never go negative. Exposure is capped. Fees and
   slippage are applied once per side. `net_pnl` reconciles with the change in
   equity: final equity − initial = Σ net realized + Σ unrealized.
6. **Determinism.** Rerun the genuine diagnostic command in
   `docs/m5-real-data-backtest-report.md` into a new output root and compare the
   analytical files byte for byte.
7. **Immutability.** Hash `data/` before and after any review run. Nothing under
   `data/` should change.
8. **Studio.** Loopback binding, Host/Origin checks, body bounds, 25-symbol cap,
   single concurrent backtest.

Known weak spots worth attention:
- `NO_OBSERVATIONS` is classed INTEGRITY (fail closed). A reviewer may prefer
  EVIDENCE.
- The ETF exclusion is the unverified M3.7 heuristic, and some names may be
  misclassified.
- Without a calendar the session axis is the union of observed bars, so missing
  weekday sessions are neither detected nor filled in diagnostic runs.
- `renderStructured` in the Studio still uses `innerHTML` for user-typed
  indicator IDs (self-XSS only; recorded as D11 in the M4 inventory).
- Benchmark units may be fractional; it is a same-universe reference, not an
  index.

Do not, during review: authenticate to Shoonya, collect data, script NSE
downloads, touch Oracle, enable timers or services, push, merge into `main`,
modify `data/`, or score the locked holdout.
