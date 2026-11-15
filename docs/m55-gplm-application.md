# Stock GPLM research application (M5.5) — owner's guide

A local, audit-first application that discovers and tests trading-rule hypotheses, runs a genuine
walk-forward learning loop over your local Shoonya/NSE daily and minute candles, and issues next-session
research forecasts. It runs without Claude, Codex or any network service. **Every output is research
evidence, not investment advice and never an order.** A result of "no robust strategy" or "NO TRADE" is a
valid, honest outcome.

## 1. Install

### Native Windows (target machine)

1. Install Python 3.12 (python.org, tick *Add to PATH*) and the current NVIDIA driver.
2. Copy the application and (for speed) the raw data to a Windows drive. From PowerShell:

   ```powershell
   robocopy \\wsl.localhost\Ubuntu-26.04-H\home\mgurunath\projects\stock-gplm-m54 C:\GPLM\app /E /XD var reports .git __pycache__ .venv
   robocopy \\wsl.localhost\Ubuntu-26.04-H\home\mgurunath\projects\stock-gplm\data\raw C:\GPLM\data\raw /E
   ```

   (Reading the raw tree directly over `\\wsl.localhost` also works but is slower.)
3. Install (creates `.venv`, installs NumPy/pandas/SciPy, the CUDA 12.8 PyTorch build, writes
   `config\app\gplm.windows.json`, then runs `doctor` and the GPU self-test):

   ```powershell
   cd C:\GPLM\app
   powershell -ExecutionPolicy Bypass -File windows\install.ps1 -DataRoot C:\GPLM\data\raw -StateDir C:\GPLM\state -Gpu
   ```

   Without `-Gpu` the CPU-only installation is complete and fully functional.
4. Verify the GPU on native Windows and keep the evidence:

   ```bat
   windows\gpu-verify.cmd
   ```

   It runs `gplm gpu-test --require-gpu` (matrix product, the 20,000-candidate scoring kernel and MLP training
   on CUDA, each compared with the CPU result), then a real GPU rule screen and five GPU replay steps. The JSON
   evidence is written to `C:\GPLM\state\reports\gpu-test-<utc>.json`. Status `GPU_PASS` is the only GPU
   acceptance; `CPU_ONLY` means CUDA did not execute.

   If CUDA is not usable, `gplm doctor` and `gplm gpu-test` say why: PyTorch missing, a CPU-only wheel,
   a build without `sm_120` kernels (RTX 50-series needs a CUDA 12.8+ build:
   `.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cu128`),
   or a driver problem (`nvidia-smi` output is shown).
5. Optional: `powershell -ExecutionPolicy Bypass -File windows\schedule-daily.ps1 -Stocks RELIANCE,HDFCBANK,ICICIBANK -At 19:30`.

Use `windows\gplm.cmd <command>` on Windows and `bin/gplm <command>` on Linux/WSL. Both run
`python -m stock_gplm.app.cli`.

### Linux / WSL (development machine)

`bin/gplm` uses this checkout's `.venv`, else the shared project venv. Python 3.12 with numpy, pandas and
scipy is enough for the CPU path.

## 2. Everyday use

```text
gplm daily --stocks RELIANCE,HDFCBANK,ICICIBANK --device auto      # the evening command
gplm dashboard --open                                              # browser dashboard on http://127.0.0.1:8765
gplm predictions --stocks RELIANCE --mode live                     # next-session forecasts in the terminal
```

`gplm daily` (1) optionally runs your configured data refresh (`--refresh`, off by default), (2) ingests only
new or changed files, (3) finds the latest completed session, (4) advances every lane's walk-forward replay:
evaluates earlier forecasts whose outcomes are now known, retrains, re-searches rules, compares challenger
models with the accepted champion, (5) issues next-session forecasts, and (6) writes the report and a
database backup. Running it twice without new data inserts nothing (no predictions, fits, evaluations or API
calls). Ctrl+C is safe: completed sessions are checkpointed; re-run the same command to resume.

## 3. All commands

| Command | Purpose |
| --- | --- |
| `gplm doctor` | Python, packages, data roots, database integrity, hash chain, lock, GPU/CUDA diagnosis |
| `gplm gpu-test [--require-gpu]` | real CUDA workloads vs CPU, JSON evidence; exit 2 when `--require-gpu` fails |
| `gplm ingest --stocks A,B` / `--universe all [--max-symbols N]` | incremental catalogue and validation only |
| `gplm discover --stocks A,B --data all-local --timeframes auto --budget quick\|standard\|deep --device auto --resume` | multi-timeframe discovery (M5.4 engine + batched screen); results enter the strategy library |
| `gplm replay --stocks A --start 2026-08-13 [--max-steps N] [--horizons 1,5]` | the walk-forward replay alone (same engine as `daily`) |
| `gplm daily --stocks A,B --device auto [--refresh] [--no-intraday]` | evening workflow |
| `gplm dashboard [--port 8765] [--open]` | local read-only dashboard |
| `gplm strategies list [--stock S] [--active]` | latest version of every strategy |
| `gplm strategies show ID` / `export --id ID [--out f.json]` / `history ID` | inspect, export, version history |
| `gplm strategies edit ID --set exit.max_hold_bars=7 [--name ...]` | new editable version (inactive until activated) |
| `gplm strategies activate ID` / `deactivate ID` / `import file.json` / `validate file.json` / `seed` | library management |
| `gplm audit --run RUN_ID` / `--prediction ID` / `--sample` | reconstruct a run or a prediction |
| `gplm audit --verify` / `--export TABLE --format csv\|json [--out f]` / `--backup` | integrity, exports, backups |
| `gplm ai status` / `ai generate --provider ollama --stock S --timeframe 1d` | optional AI hypotheses (off by default) |
| `gplm status` / `gplm symbols` | lock and progress; local universe |
| `gplm research ...` | pass-through to the M5.4 CLI (`fixture`, `report`, `library`, `stop`, ...) |

`--device auto` uses CUDA when PyTorch can run on the GPU and otherwise states why it chose the CPU;
`--device gpu` fails with exit code 2 if CUDA is unusable; `--device cpu` forces NumPy. Exit code 3 means
another GPLM process holds the writer lock (`<home>/gplm.lock`; a lock left by a dead process is replaced
automatically and the takeover is logged).

## 4. What the forecasts contain

Each candidate lists symbol, source (model kind, discovered rule or library strategy with version),
prediction time (the decision bar's close), intended session (`NEXT_SESSION_AFTER_<date>` plus a
next-weekday estimate — no holiday calendar is inferred), direction, entry rule (buy at the next open;
reference close shown), ATR-based stop and target, holding horizon, risk/reward and expected R-multiple,
calibrated confidence and its calibration status, historical and rolling-20 out-of-sample accuracy,
round-trip cost assumption, market conditions (RSI, ADX, ATR, distance to EMA50, weekly RSI), the strongest
feature contributions, reasons, and every reason it is *not* actionable.

A forecast is **actionable** only if all hold: it comes from the accepted champion model, the champion is
not the base-rate baseline, the probability is Platt-calibrated on earlier sequential outcomes, enough
sequential outcomes exist, the calibrated probability clears the threshold, the stop/target geometry gives a
positive expected R-multiple, the direction is long (delivery shorting is not modelled), and the bar is not
flagged. Rules need FDR survival and a positive sequential record. Otherwise the symbol shows **NO TRADE**.

## 5. Metrics vocabulary (kept separate everywhere)

* **Training accuracy** — in-sample fit on the training rows of the last step.
* **Sequential out-of-sample (OOS) accuracy** — predictions stored before their outcome existed at the replay
  clock and evaluated after the outcome window closed. Compare it with the base rate shown next to it: with
  a cost-aware label most short-horizon moves are "not profitable", so a high accuracy can be the majority
  class. `accuracy_minus_majority` and the Brier score against the baseline model are the honest measures.
* **Effective n** — multi-day labels overlap; daily-lane evidence is worth about `evaluated / horizon`
  independent observations.
* **Model-selection performance** — in-sample statistics used to pick rules or champions (never quoted as
  validation).
* **Trading performance** — simulated trade returns after cost assumptions for predictions that were
  actionable when issued.
* **Live forward performance** — evaluated predictions whose mode is `LIVE` (issued after the latest
  completed session, before the outcome existed anywhere locally).

## 6. How the learning loop works

For each session from `rolling.start` (2026-08-13) to the latest completed session, per lane (daily, and
15-minute where at least 60 minute sessions exist):

1. the cutoff is the decision bar's close (daily) or the previous session's close (intraday);
2. earlier predictions whose outcome window has completed are evaluated;
3. models are re-fitted on rows whose label window ended by the cutoff (a `LabelGuard` raises otherwise),
   with recency weights (half-life 250 sessions); `logit` every step, `mlp` warm-started and fully re-fitted
   every 5 steps; histories are stored per epoch;
4. predictions are written; probabilities are Platt-calibrated on earlier OOS outcomes only;
5. rules are re-searched every 10 steps on completed labels with Benjamini–Hochberg correction; library
   strategies signal on the same bars (a discovered strategy is never scored on bars inside the data it was
   discovered on);
6. a challenger replaces the champion only with ≥25 outcomes and a Brier improvement > 0.002 over the last
   60; promotions are logged and old artifacts kept;
7. the step is checkpointed. Horizons without enough history (fewer than 12 independent labels) are skipped
   and reported (for example 120- and 250-session horizons with the current 989 daily bars).

## 7. Data handling

See [m55-data-investigation.md](m55-data-investigation.md). In short: every local file is catalogued with
its size, mtime and SHA-256; only new or changed files are parsed; nothing is filled or repaired; minute
sessions from 2026-08-03 are accepted under the documented closing-phase interpretation with closing-phase
volumes withheld; daily candles after 2026-10-29 are minute-derived and labelled; every anomaly is a row in
`data_issues`. Access basis: `OWNER_AUTHORIZED_FULL_LOCAL` (owner directive, 2026-11-15). The M3.2 split plan
and its records are unchanged; its 2025-10-21 cutoff does not govern this workflow.

## 8. Strategy library

Definitions are JSON (`stock-gplm.strategy.v3`) — see `config/app/strategies/*.json` for hand-written
templates. Discovered defaults are immutable, read-only, hash-bound and versioned under
`<home>/strategies/defaults/`; `gplm strategies edit` writes a new version under `<home>/strategies/user/`.
Scope may be one symbol, a list or `"*"`, plus timeframes. Validation is strict: registered indicator outputs
only (optionally `1w:`/`1d:`/`60m:` prefixed), operators `gt lt cross_above cross_below is`, numeric
thresholds and exits. Nothing in a strategy is executed as code.

## 9. Audit trail and storage

`<home>` (default `var/gplm`, Windows `C:\GPLM\state`):

| Path | Content |
| --- | --- |
| `gplm.sqlite` | runs, hash-chained events, data catalogue and issues, strategies, models, champions, training history, predictions (append-only), evaluations (append-only), API calls, checkpoints, config snapshots |
| `events/events-YYYY-MM-DD.jsonl` | best-effort mirror of every event (the database is authoritative) |
| `models/<SYM>/<tf>/<replay>/` | every fitted model state, one file per fit, SHA-256 in `models` |
| `cache/` | parsed-file and canonical-candle caches (rebuildable) |
| `reports/daily/<session>/`, `reports/latest/` | `report.html`, `forecasts.csv/json`, `accuracy.csv/json`, `summary.json` |
| `reports/gpu-test-*.json` | GPU evidence |
| `discovery/unified/<SYM>/<run>/` | M5.4 engine reports (`report.md`, `strategies.json`, trades, checkpoints) |
| `backups/` | database backups after every daily run (last 10 kept) |

`gplm audit --prediction ID` returns the prediction, its evaluation, the exact model artifact (re-hashed),
the training step, the run, the configuration snapshot, the canonical-candle fingerprint, every source file
with its SHA-256 up to the decision date, the feature-config hash and the event-chain status.

## 10. Optional AI assistance

Off by default (`ai.enabled=false`). Local Ollama needs only `enabled`. Remote providers additionally need
`ai.allow_remote=true` and `--allow-remote`; keys are read only from the named environment variable and never
logged. Monthly and per-run USD limits are enforced from the `api_calls` table; when prices are not configured
the cost is recorded as unknown (NULL) and remote calls need `--allow-unknown-cost`. Only the feature catalogue,
symbol and timeframe are sent. Replies are validated as strategy JSON and enter the library **inactive** for
human review.

## 11. Configuration

`config/app/gplm.json` (override with `--config` or `GPLM_CONFIG`; `GPLM_HOME` moves the state). Sections:
`data`, `refresh`, `timeframes`, `horizons`, `rolling`, `models`, `candidates`, `prediction`, `costs`
(assumptions, not broker-verified), `discovery`, `ai`, `dashboard`, `retention`. Feature sets are in
`config/app/feature-space.json`. Every run snapshots the effective configuration; its SHA-256 is stored with
the run and therefore with every prediction.
