# Handover: research automation + universe download (2026-11-20, evening)

Continues `live-lab-handover-2026-11-20.md` (read that first for the live lab itself). Branch `feature/m6-prd-v1`.
**Nothing after commit 9b93b07 is committed** (mine and Codex's work, see "Files"). All PAPER / research only.

## Owner rules set today (binding)
1. **No polling.** Long jobs must self-start/self-chain; never spend tokens waiting. Check status only when asked.
2. **Use the whole PC** (CPU + GPU) for research when the owner is away; don't ask.
3. **Per-timeframe judgement:** each stock × TF judged on its own evidence; if a strategy wins on several TFs keep
   it on all of them; never force/assume across TFs, never make strategies unique per TF.
4. **Entire NSE universe** minute data, **stock by stock**; research each stock as soon as its data is complete.
5. Shoonya login stays **owner-only** (localhost / tailnet); family viewers never see it.
6. Codex (owner-run) added **cloud retention**: verified delivery to PC permits deleting the Oracle copy (ledgers kept).

## What runs where (all automatic)
| Process | Where | Notes |
|---|---|---|
| `cloud_sync.py --minute-sessions 250 --order symbol --universe universe.txt --floor 2026-01-19` | Oracle `~/gplm-live` | Stock-by-stock, oldest day first, adaptive pace (1 s market hours, 0.25 s idle, doubles on errors), outage pause, quick retry sweep, provider-window floor (`_floor` in `fetch_state.json`). Watchdog: user crontab `@reboot` + `*/5` → `run_cloud_sync.sh`. Progress: `universe_progress.json`. Codex: `transfer_ledger.py` + `transfer-ledger.sqlite`. Log `sync-YYYYMMDD.log` (stdout buffered). |
| `live_collector.py` | Oracle | unchanged (daily Shoonya login on the page). |
| `stock_gplm.app.live_lab` | PC :8765 | Restarted 16:07 (Codex). New: Data link full-window completeness + Learning/Universe/Results KPIs, owner-only Shoonya login reminder (08:45 weekdays if token won't last; on expiry), mobile layout, rule-C confidence from `evidence.py`. |
| `var/runs/research_feed.py` | PC | Every 10 min: stocks complete 2026-01-19→yesterday (watchlist first, then `universe.txt` order) → `research-matrix` intraday deep (`--home state-intraday`, 12 workers, OPENBLAS 1 thread) → local-LLM proposer (`llm-intraday`, qwen3:14b, ctx 8192, 6 workers) → after all 50: `setup_replay test-period-2` (once), `stock_profile`, `strategy_scorecard`. Lock-retry + FAILED-retry. Done list `research_feed_done.txt`. Codex edited it (CUDA screening required, LLM for all queued). |
| `stock_gplm.app.universe_sync` | PC | Pulls any stock where Oracle has more days than PC (sha-verified, no overwrite). |
| `stock_gplm.app.cloud_retention` | PC | Codex: verifies PC copies, acknowledges, Oracle deletes. Docs `docs/cloud-staging-retention.md`. |

Workspaces (separate locks): `state` (main), `state-intraday` (intraday research; junction
`state/reports/research/evidence-intraday` → `state-intraday/...`), `state-wide` (daily discovery of other stocks).

## Results so far
- **Setup replay 14 Aug–19 Nov (old live logic, 50 stocks, 314k setups):** avg **−0.18 %/trade** net; HIGH≈LOW
  (old live-replay confidence meaningless); 5m worst (−0.19 %), 60m least bad (−0.13 %); agreement count useless.
  `var/setup-replay/test-period/summary.md`.
- **Daily deep (`discover-daily`, 50 stocks 1d/1w):** 23.5k backtests, 485 ML rules saved, 0 validated (history only
  since 2022-10 / 2025-10 → thin). `llm-daily` done (qwen3:14b). `state-wide`: 250 liquid + 1,481 other stocks done.
- **Intraday deep, first 40 stocks:** 35.9k tests, 0 VALIDATED, 641 DISCOVERED, 83 with ≥20 val trades & positive
  net, q 0.6–1.0 (not significant). MAXHEALTH: trend strategies on every TF (+0.3–0.4 %/trade). Awaiting test-period
  check. Matrix: `state-intraday/reports/research/evidence-intraday/matrix.csv`.
- Behaviour profile smoke (RELIANCE): many reactions "stronger than average but still lose after ~0.2 % costs".

## Data facts
- **Shoonya serves only ~206 recent sessions of 1-min data.** 2026-01-01..16 is gone for everyone except RELIANCE
  (fetched 30 Oct). Window floor moves forward daily → daily collection matters.
- Universe = 2,690 symbols (`deploy/oracle/live/universe.txt`, by turnover). ~10–11 days for all at ~36 calls/min.
- Calendar: `config/calendar/nse_2026.json` (copy of Oracle's). Muhurat 8 Nov not configured.
- Gap check: `python -m stock_gplm.app.minute_gaps --start 2026-01-19` (exit 0 = no gaps).

## New modules (PYTHONPATH=src)
`evidence.py` (rule C), `setup_replay.py`, `strategy_scorecard.py` (stock view, strategy view, `assign_distinct` =
best per stock×TF, `assigned.csv`), `stock_profile.py` (character + ~40 event reactions, dev/test, held),
`strategy_llm.py` (+ `llm` method in research.py/cli.py), `minute_gaps.py`, `universe_sync.py`;
`strategy_evolve.py` gained level/intraday building blocks; research.py: per-rule try/except for evolve/llm,
traceback in RESEARCH_LANE_FAILED events. Codex: `library.py` 4h feature regex fix (+ `tests/test_library_4h_features.py`),
`cloud_retention.py`, `deploy/oracle/live/transfer_ledger.py`, `windows/train-entire-universe.ps1`, AGENTS.md edits.

## Known caveats
- Windows **commit limit**: many worker processes × OpenBLAS threads exhausted it → always `OPENBLAS_NUM_THREADS=1`.
- One GPLM research process per home (`gplm.lock`); hence separate homes.
- Daily strategies get no test-period verdict (replay is intraday only) — profiles do check 1d reactions.
- Shoonya token ~23 h: without the daily login the download and live feed pause (research on existing data continues).
- `api/state` ≈ 1 MB per refresh (heavy on mobile data).

## TODO (in order)
1. When asked: check `var/runs/research-feed.log`, then read `state/reports/scorecard/latest/scorecard.md`
   (Assigned section first) and `state/reports/profiles/latest/profile.md`; summarise for the owner.
2. ~~Review + commit~~ DONE 2026-11-20: commit `6adbb03`, ledger `docs/m81-test-budget.md` (6/10 run, 6 passed),
   roadmap note added. Left uncommitted on purpose: `.git.wsl-backup`, `notifications1.mp4`, `var/`.
   Review note: `cloud_link.pull()` raises if the Oracle transfer-ledger RPC fails (and calls it even for an
   empty list); intended (receipts required) but watch for it.
3. Feed the scorecard the `state-wide` runs (pooled daily priors) and the intraday ML/LLM runs; add a daily (1d)
   test-period replay.
4. Live lab: use scorecard `assigned.csv` to choose which strategies alert per stock×TF (instead of all 210).
5. Mobile: installable PWA + Android-emulator test (owner has Android Studio); send only changes instead of 1 MB state.
6. Longer daily history (NSE bhavcopy 10+ years) for reliable 1d/1w findings — needs owner OK.

## Next-chat prompt
> Read docs/handover/research-automation-handover-2026-11-20.md and continue from the TODO list.
