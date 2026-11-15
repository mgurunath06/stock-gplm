# CURRENT STATE — M5.5 GPLM research application

Updated 2026-11-15. Read this first after a context reset.

## Location

| Item | Value |
| --- | --- |
| Worktree / branch | `/home/mgurunath/projects/stock-gplm-m54`, `feature/m55-gplm-app` (branched from `feature/m54-discovery`; local only, not pushed or merged) |
| Untouched | `stock-gplm` main checkout (Codex, uncommitted M5.3), `stock-gplm-m45` (Codex), `stock-gplm-claude` |
| Python | `/home/mgurunath/projects/stock-gplm/.venv/bin/python` via `bin/gplm` (Linux/WSL); Windows: `windows\gplm.cmd` with its own `.venv` |
| Authority | Owner directive 2026-11-15: read all local daily+minute data including 2026 (`OWNER_AUTHORIZED_FULL_LOCAL`). AGENTS.md in this worktree carries the M5.5 scope rule. M3.2 records untouched. |

## What exists

`stock_gplm.app` (the `gplm` CLI): ingest, minute_adapter, lanes, compute (NumPy + PyTorch CUDA),
models, candidates, rolling, library, discover, daily, report, dashboard (+ `static/index.html`), ai,
doctor, progress, db, context, settings, cli. Config: `config/app/gplm.json`, `feature-space.json`,
`strategies/*.json` templates. Windows: `windows/install.ps1`, `gplm.cmd`, `gpu-verify.cmd`,
`schedule-daily.ps1`. Docs: `docs/m55-gplm-application.md` (owner's guide), `docs/m55-data-investigation.md`,
`docs/m55-acceptance.md` (evidence and results). M5.4 changes: `m54/portable.py` (Windows: no `resource`,
no fork), NaN-volume propagation in `m54/timeframes.py`.

## Key decisions

* Minute sessions from 2026-08-03 carry a 15:20 cumulative-volume reset. Adapter `CLOSING_PHASE_V2`
  accepts them (prices kept, closing-phase interval volumes withheld). Evidence in the data investigation.
* Official daily candles end 2026-10-29; 2026-10-30 → 2026-11-13 are `MINUTE_DERIVED` daily rows.
* Models predict P(net return after round-trip cost > 0). Baseline = recency-weighted base rate; it is the
  initial champion and can never be actionable. Promotion needs a paired one-sided t-test (n_eff = n/h)
  plus a Brier margin (added after margin-only promotions flip-flopped in the first real replay).
* Discovered strategies are never scored on bars inside their discovery data.
* GPU: code paths are real (torch CUDA matmul scoring, mask generation, logit/MLP training) but PyTorch is
  NOT installed in the shared venv (no install was authorized). GPU acceptance is the owner's
  `windows\gpu-verify.cmd` run on native Windows.

## Next actions

1. Owner: run `windows\install.ps1 -Gpu` and `windows\gpu-verify.cmd` on Windows; keep the gpu-test JSON.
2. Independent review of `app/rolling.py` (cutoffs, LabelGuard, champion test) and `app/minute_adapter.py`.
3. Optional: an authoritative NSE session/closing-auction document would let the closing-phase rule move
   from `OBSERVED_NOT_AUTHORITATIVE` to authoritative; a holiday calendar would remove next-weekday estimates.
4. Universe-wide daily runs are possible (`--universe all --max-symbols N`); ingestion of all 2,394 symbols takes about 4 minutes, and the first replay costs roughly 15-30 s per symbol on CPU.

## Recovery

Every command is resumable: re-run it. `gplm status` shows the lock and progress; a lock left by a dead
process is replaced automatically. `gplm audit --verify` checks the hash chain and SQLite integrity.

## Acceptance status (2026-11-15): PARTIAL

All CPU and real-data items pass (see `docs/m55-acceptance.md`); native-Windows GPU execution is the one open
item. State: `var/gplm` (acceptance database, deep + standard discovery, replay 2026-08-13 → 2026-11-13),
`var/gplm-universe` (catalogue of all 2,394 symbols; its absolute cache paths point at the original
`config/app/var/...` location, so treat it as statistics only), `var/acceptance/` (command outputs).
Result: no model beats the base-rate baseline out of sample; zero actionable forecasts; NO TRADE for the
session after 2026-11-13. Two RELIANCE daily ROBUST_CANDIDATE rules (high-volatility regime) are weak.
