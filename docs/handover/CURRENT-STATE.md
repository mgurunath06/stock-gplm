# CURRENT STATE — M5.4 local-first behaviour discovery

Updated 2026-11-15. Read this first after a context reset.

## Location

| Item | Value |
| --- | --- |
| Milestone | M5.4: M5.3 reconciliation, then universal stock-behaviour discovery and ML research readiness |
| Worktree | `/home/mgurunath/projects/stock-gplm-m54` |
| Branch | `feature/m54-discovery` (local only; not pushed or merged), created from `76ae301` |
| HEAD | `git log -1` (code commit `180a682`, then a docs and ML-readiness commit) |
| Untouched worktrees | `stock-gplm-claude` (Claude MVP, `76ae301`); `stock-gplm` main checkout (Codex's **uncommitted** M5.3 on `feature/m53-research-mvp` @ `38973e6`); `stock-gplm-m45` (Codex) |
| Python | `/home/mgurunath/projects/stock-gplm/.venv/bin/python` (3.12; numpy, pandas, scipy, sklearn, lightgbm) |
| Running processes | none |

## Completed and verified

1. **Reconciliation**: `docs/m54-reconciliation.md` contains the comparison, the reason for
   HOLDOUT_BOUNDARY_UNVERIFIED (no governed trust root; T4, not done), the reuse matrix and the V1 RSI/MACD
   dialect conflict (Claude vs Codex).
2. **Engine** `stock_gplm.m54` (indicators, hypotheses, evaluate, engine, store, report, bulk, device, ml, ai, cli)
   plus `bin/gplm`. Configs are in `config/m54/` (discovery-default, budgets, search-space, universes/five-stock,
   hypotheses/example).
3. **Providers**: `llm/envfile.py` (0600 `.env`, GPLM_* only, linked-worktree lookup), `llm/registry.py`
   (offline by default, live-call gates, per-run caps, mock health) and an Anthropic-compatible adapter in
   `llm/client.py`. Config is `config/research/providers.json`. `/home/mgurunath/projects/stock-gplm/.env` was
   changed from mode 644 to **600**. It is git-ignored and absent from history.
   `gplm providers check` shows the MWAPI, StartupAPI and OpenRouter keys present, and OpenAI, Anthropic and Gemini
   empty. **No live API call was made.**
4. **Real-data runs** (pre-cutoff daily only, 2022-10-31 to 2024-12-31, 527 bars per stock):
   - RELIANCE quick (an earlier code hash, superseded) and **RELIANCE standard**:
     `reports/m54/runs/RELIANCE-EQ/a123ad5b…/report.md`
   - **Five-stock standard batch** `e2b09fa5…` (4 workers, 4 GB cap): `reports/m54/batches/e2b09fa5…/summary.csv`

   | Stock | Discovery tests | Validation passers | TEST_CONSISTENT | Min BH q |
   | --- | ---: | ---: | ---: | ---: |
   | RELIANCE | 13,415 | 3 | 0 | 0.93 |
   | HDFCBANK | 13,475 | 5 | 4 | 1.00 |
   | ICICIBANK | 13,420 | 7 | 1 | 0.98 |
   | INFY | 13,385 | 3 | 2 | 1.00 |
   | SBIN | 13,490 | 1 | 0 | 0.96 |

   **Interpretation:** no finding survives FDR. The seven test-consistent patterns are plausible chance and
   not edge.
5. **Resume**: re-running the batch reused all five runs in 0.7 s, with hashes verified.
6. **Tests**: full suite **921 passed, 2 skipped**
   (`PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q`). The 25 M5.4 tests in
   `tests/test_m54_discovery.py` are synthetic only.
7. **CPU/GPU**: RTX 5070 visible via nvidia-smi (driver 610.88). No CUDA framework is installed, so `auto` resolves
   to CPU and `cuda` is refused. The benchmark is about 1 ms per evaluation, so a GPU is not justified at this scale.

## Governance confirmations

No minute file was opened. The M3.2 locked holdout and the 2025/2026 files were not decoded. No trust root was
changed, no model was fitted, no trial was reserved, no external API was called, and nothing was pushed, merged or
deployed. Raw files were opened read-only.

## Blockers and decisions for the user

1. **Governed M5 path**: installing the M5 approval trust root (T4) is needed for STRICT research. Not done.
2. **More history**: 2025–2026 daily data is at or after the M3.2 cutoff. Using it requires scoring or retiring M3.2
   first.
3. **Universes beyond explicit lists**: no point-in-time NIFTY membership exists locally. A current list must be
   supplied and labelled `CURRENT_CONSTITUENT_DIAGNOSTIC`.
4. **AGENTS.md scope** does not yet name M5.4. Consider adding a rule when accepting this milestone.
5. **Live AI calls**: each provider needs `enabled`, `live_calls_authorized`, a real model id and verified pricing,
   plus `offline: false`.
6. **V1 MACD dialect conflict** with Codex's uncommitted M5.3 must be resolved when Codex commits.

## Next commands

```bash
cd /home/mgurunath/projects/stock-gplm-m54
bin/gplm status
bin/gplm report --stock HDFCBANK --print
bin/gplm discover --universe five-stock --budget deep --workers 4 --max-memory-gb 4 --dry-run
```

## Next engineering actions

1. Codex independent review of `feature/m54-discovery` (engine timing, purging, multiple-testing count).
2. Add a cross-stock pooled view: the same hypothesis across the five stocks, with pooled BH. That is stronger evidence
   than per-stock leads.
3. Optional adoption of Codex `m53.recency` as a finding-level diagnostic, after it is committed.
4. A universe file for a current NIFTY 50 list (labelled survivorship-biased), then a batch dry run before running it.
5. ML: feed `m54.ml.build_dataset` into the M3.5/M3.7 pipelines only under their trial-ledger rules.

## Recovery

Re-run the same `gplm discover` command. Completed runs are verified and reused, completed stages and screen chunks
are reused, and a STOP file halts at a checkpoint (`gplm clear-stop` to continue). A code or config change produces
a new run id, and old runs stay as evidence.

## History: Claude five-stock MVP (superseded for discovery)

`stock_gplm.mvp` remains the V1-strategy diagnostic runner (`config/mvp/five-stock-diagnostic.json`,
results in `stock-gplm-claude/reports/mvp5/`). Its loader is reused by M5.4.
