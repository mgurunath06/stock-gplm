# Handover — Live Lab / M8 prototype (2026-11-20, session 2)

Branch `feature/m6-prd-v1`. **Nothing from today is committed yet** (commit after review; see "Files").

## What exists now (all PAPER / research only)

| Part | Where | Notes |
|---|---|---|
| Oracle live collector | `~/gplm-live/live_collector.py` on `sne-cloud` | Completed 1-min candles for `watchlist.txt` (50 stocks + NIFTY, BANKNIFTY, INDIAVIX). Append-only `data/<date>/bars.jsonl`, `heartbeat.json`, warm-up `history*.jsonl`. Stops 15:33. |
| Oracle cloud filler | `~/gplm-live/cloud_sync.py` | Fills gaps via existing `gplm_jobs.minute/eod`; retry/backoff, PROBLEM after 5 fails, OUTAGE not charged. **Currently running with `--minute-sessions 250`** (1-year minute backfill to 2026-01-01; calendar only covers 2026). Writes `inventory.json`. |
| Oracle control | `~/gplm-live/gplm_live_ctl.py login\|start\|stop\|status\|reset SYM` | Login code via STDIN only. |
| PC live lab | `src/stock_gplm/app/live_lab.py` → `python -m stock_gplm.app.live_lab --port 8765` (PYTHONPATH=src) | Push stream (ssh `tail -F`), PC watcher/sync (`cloud_link.py`), 210 strategies on 5m/15m/30m/60m (194 long = paper alerts, 16 short = bearish warnings), setups/outcomes, confidence, behaviour, intraday events, market header, notifications, Explain. |
| Page | `src/stock_gplm/app/static/live.html` | Workstation UI (IBM Plex Sans, dark/light, 3 panes, Lightweight Charts 4.2.0 CDN), tabs Workspace / Behaviour / Data link / History / Visitors (owner). Page is served from disk — page edits need no restart. |
| Behaviour engine | `src/stock_gplm/app/behaviour.py` | Multi-scale profiles D/W/M/Q/Y, change detection (MW + BH + persistence), intraday events every minute. |
| Notifications | `src/stock_gplm/app/notify.py` | Template text per setup (SETUP/OUTCOME/CONFLICT/BEHAVIOUR/INTRADAY/SYSTEM). |
| Local AI | Ollama `qwen3:14b` on 127.0.0.1:11434 | `explain_setup()`; never use `*-cloud` models. |
| Access | Tailscale Serve + **Funnel ON**: `https://<pc-host>.<tailnet>.ts.net` (see `tailscale funnel status`) | Owner = localhost or tailnet identity (full control). Everyone else = family viewer: Basic auth from `var/live-lab/viewer_password.txt` (`username:password`, currently set by owner), read-only (+Explain), logged to `var/live-lab/access.jsonl` (Visitors tab). Disable: `H:\Guru\TailScale\tailscale.exe funnel --https=443 off`. |

## Owner decisions today (binding)
1. **No locked holdout.** Train/validate on all candles ≤ **2026-08-13**; **2026-08-14 → now = out-of-sample test**; then walk-forward retrain. `development_end` set to 2026-08-13 in `config/app/gplm*.json`. Supersedes AGENTS.md holdout text.
2. **Oracle = pass-through for candles only.** All backtests/training run on the PC.
3. **Minimal agent tokens**: give the owner PC commands for heavy runs.
4. Indices (NIFTY/BANKNIFTY/INDIAVIX) context only. Bearish = warnings only (PRD v1 long-only).
5. Family sharing via Funnel + password approved (owner accepted weak password).
6. Confidence must come from **backtested evidence**, not the live-lab 8-session replay (approved rule C: expected net profit after costs + sample size, below).

## Running right now / owner is running
- Oracle collector + cloud filler (backfill ~3 h from ~12:46 IST).
- Owner runs on PC (PowerShell, quote comma lists!):
  `research-matrix --stocks $s --timeframes "1d,1w" --mode catalogue --budget standard --name evidence-daily`
  Tonight after Data link → Cloud filler = COMPLETE: same with `--timeframes "5m,15m,30m,60m" --name evidence-intraday`.
  Output: `<home>/reports/research/<name>` (+ DB `m56_results`). Resumable by rerunning with the same `--name`.

## TODO next (in order)
1. **After 15:30 restart the lab once** to activate the **warm restart** (`save_state/load_state`, `state.pkl` per day; already coded + unit-checked, not yet running). Avoid restarts in market hours (cold replay ≈ 8–10 min).
2. ✅ (code done, active after the 15:30 restart) `app/evidence.py` reads audit validation trades of runs `evidence-intraday`, `evidence-daily` (first wins per lane; reload every ~10 min); setups without backtest = LIVE_REPLAY_ONLY capped LOW. Open: rule C has no multiple-testing guard (q-value) — item 3 must cover it. Original spec: **Wire confidence to research evidence**: per agreeing strategy × symbol × tf read validation metrics (`mean_net`, `trades/effective_n`, `p_value/q_value`, regime) from `m56_results`/report; fall back to MARKET_WIDE labelled. Rule C: HIGH n≥100 independent & cautious net/trade > +0.15% & win rate > break-even for its R:R; MEDIUM n≥40 & cautious net > 0; else LOW; −1 level on bearish conflict.
3. **Test-period check**: frozen params scored on 2026-08-14 → today; downgrade if test fails. Then monthly walk-forward command for owner.
4. Count only **independent** past signals in live-lab fallback; feed 60+ sessions (local minute now synced) instead of 8.
5. **1d/1w strategies at EOD** (15:35) so the monthly trend counts.
6. Week/Month window shows today only until daily state accumulates (relabel / read previous days' `state.pkl`).
7. P2 features approved earlier: paper journal ("I took this"), strategy leaderboard, EOD report (local AI summary), price alerts. P3: watchlist mgmt, events calendar, day replay, sector view.
8. Later: cloud move (Oracle Always Free ARM, if owner wants 24×7), systemd timers, M2.5 auto-login canary (needs approval; after 15:35).
9. Commit today's work (+ roadmap note "M8.1 prototype", test ledger per test-budget discipline).

## Known caveats
- Confidence today = 8-session live replay → almost all LOW (median n 14; win-rate rule ignores R:R ≈ 1.67 ⇒ break-even 38%). Fix = items 2–5.
- Funnel public DNS: Google resolver may show 100.x; Cloudflare shows public IPs (non-100.x). Verified 401 without password, viewer with password.
- Windows ssh truncates long command lines → file lists go via STDIN (`tar -T -`).
- Daily login: Shoonya code pasted on the page (token cached on Oracle, 23 h).

## Files (uncommitted)
`src/stock_gplm/app/{live_lab.py,cloud_link.py,behaviour.py,notify.py,static/live.html}`,
`deploy/oracle/live/{live_collector.py,cloud_sync.py,gplm_live_ctl.py,watchlist.txt}`,
`config/app/gplm.json`, `config/app/gplm.windows.json` (development_end), this handover.
Runtime (gitignored): `var/live-lab/` (events, state, access log, viewer password).
