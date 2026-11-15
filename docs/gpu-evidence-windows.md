# Native Windows GPU research runner

Run from `H:\Guru\Learning\StockGLPM\app`. No Codex, Claude, LLM service, paid API,
broker, download, or network access is needed by this command. It uses the existing
Python 3.12 workspace environment and production local-candle adapters, causal
features, candidate discovery, logistic/MLP trainer and exact trade simulator.
No packages are installed by the launcher.

## Bounded training and evaluation

```powershell
Set-Location 'H:\Guru\Learning\StockGLPM\app'
.\gpu-evidence.cmd --stocks RELIANCE --start 2026-08-01 --end 2026-11-14 --timeframes 15m --horizon 4 --depth quick --folds 2 --epochs 20 --device gpu --fresh
```

This fits both models twice, discovers candidates separately at each cutoff and
evaluates each on its following unseen block. It is bounded by one symbol, one
lane, two folds, 20 epochs per model, and 30 candidate pairs per fold. Ingestion
still validates the symbol's available source partitions; this is not a wall-time
limit. Missing/insufficient real candles produce an explicit failure, never a
synthetic substitute. A bounded workload may show brief GPU spikes rather than
sustained utilization.

## Deep research

```powershell
Set-Location 'H:\Guru\Learning\StockGLPM\app'
.\gpu-evidence.cmd --stocks RELIANCE,HDFCBANK,ICICIBANK --start 2020-01-01 --end 2026-11-14 --timeframes 1d,15m --horizon 4 --depth deep --folds 10 --epochs 200 --device gpu --fresh
```

Choose dates and stocks available in your local collection. Dates are inclusive;
features and training use only the selected range. Horizon is in bars of each
selected timeframe (4 daily bars versus 4 fifteen-minute bars). Minimum history:
250 warmup/training bars plus at least `folds * (horizon + 10)` evaluation bars;
there must also be 150 valid training labels and both classes at each cutoff.
Quick/standard/deep defaults are 2/5/10 folds and 20/80/200 epochs. Explicit
`--folds` and `--epochs` override these. Ctrl+C stops, preserving partial evidence;
a subsequent invocation starts a new run rather than resuming it.

`--device gpu` requires usable PyTorch CUDA and fails explicitly if unavailable.
`--device auto` selects CUDA when usable and prints the reason for CPU fallback;
`--device cpu` runs the same research on NumPy. The selected CUDA backend performs
candidate reductions and actual Adam training/backpropagation on CUDA tensors.
Feature construction, inference and trade simulation run on CPU. Each model fit
records `training_backend` and `cuda_calls`; these are workload counters, not a
hardware profiler. Driver telemetry records GPU utilization and VRAM every two
seconds; missing telemetry is reported rather than fabricated. CPU percentage is
process CPU time relative to one logical core and can exceed 100%.

## Launcher and program

- `H:\Guru\Learning\StockGLPM\app\gpu-evidence.cmd`
- `H:\Guru\Learning\StockGLPM\app\src\stock_gplm\app\gpu_evidence.py`
- The existing `windows\gplm.cmd gpu-evidence` command invokes the same runner.
- Python entry point: `python -m stock_gplm.app.gpu_evidence` with `src` on PYTHONPATH.

The launcher uses `.venv\Scripts\python.exe` and the existing
`config\app\gplm.windows.json` when present. Override via `--config PATH` or
`GPLM_CONFIG`. Configuration supplies local daily/minute paths and cost assumptions.
`--output PATH` changes the parent output directory; `--home` selects the settings
home used to derive the default output parent. Outputs inside raw trees are refused.

Every invocation is fresh, even without `--fresh`: no old model, parsed-candle,
feature, prediction or checkpoint is reused. `--fresh` makes this intent explicit.
The existing audit database is never opened. New outputs default to
`H:\Guru\Learning\StockGLPM\state\research-runs\<timestamp-uuid>\` with the
Windows config. Each folder contains its own audit database and audit run ID.
Existing raw files, models, predictions and reports are preserved.

## Watching and inspecting results

Live JSON lines show symbol, candle counts, training/evaluation row counts,
candidate tests, retained rules, sample rejected rules and rejection reasons,
epoch progress (approximately ten updates per fit), measured loss/training
accuracy, prior OOS accuracy, fold OOS accuracy, CPU/GPU telemetry and elapsed time.
Full candidate decisions are saved, including candidates omitted by the selection
budget; unsupported/constant features and insufficient-effective-sample tests
are represented by discovery counts. An empty strategy set is a valid negative
result; model training still runs if its data requirements pass.

Open the printed `report.md` for rules, next-bar-open entries, stop/target/time
exits, fold accuracy, trades, net returns, win rates and closed-trade drawdowns.
`report.json` and `*-evaluation.json` include individual predictions, training
histories, model hashes and every trade. `*-discovery.json` includes candidate
conditions, selection statistics and decisions. `execution.jsonl` contains live
progress and telemetry. `source-files.json`, `source-issues.json`, the isolated
`gplm.sqlite`, and config/code hashes record source checksums, publication/content
decisions, rejected records and computation provenance. Models are separate NPZ
files. Partial files and failure details remain on unsuccessful runs.

Training expands chronologically; training labels must finish before the OOS
block starts. Candidate discovery and preprocessing fit only on earlier data.
Each fold retrains from scratch; calibration uses only already-matured earlier
OOS outcomes and otherwise says UNCALIBRATED. There is no locked-holdout scoring
or live forecast. Comparators are the training-majority direction/base-rate
Brier score, always-long with the same holding horizon/costs, and cash. Model
trades require probability >= 0.56. Candidate trades use their discovered
stop/target/time policies. Trading costs/slippage are the configured assumptions,
not a claim of broker-verified fills. Long-only trades are non-overlapping per
strategy; each strategy has its own reinvested equity and fold boundaries close
positions. Drawdowns are closed-trade drawdowns, not mark-to-market drawdowns.
Selection statistics and in-sample accuracy are not OOS evidence. Repeated user
research can overfit these OOS windows: no promotion or performance guarantee is
made. Current symbols do not establish point-in-time universe membership; raw
unadjusted prices and unknown corporate actions remain research limitations.

Delivery validation: syntax/import/argument parsing only on Python 3.12.10.
No real-data training, benchmark, full search or acceptance suite was executed
for this delivery. CUDA compatibility and financial results require your run.
