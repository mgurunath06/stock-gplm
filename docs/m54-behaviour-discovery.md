# M5.4 local-first stock-behaviour discovery

`gplm` researches the daily behaviour of NSE equities on this machine. It needs no Claude, Codex or external API
for any computation. AI providers are optional, disabled by default, and can only *propose* hypotheses, which the
local engine then tests like any other.

Classification of every output: **`DIAGNOSTIC_ONLY_NON_INVESTABLE`**. Nothing is a trading signal.

## Running it

From any checkout (`bin/gplm` uses that checkout's `src` and the project venv):

```bash
cd /home/mgurunath/projects/stock-gplm-m54
bin/gplm discover --stock RELIANCE --budget quick --dry-run      # workload, data boundaries, no writes
bin/gplm discover --stock RELIANCE --budget standard             # ~60 s on CPU
bin/gplm discover --stocks RELIANCE,SBIN,INFY,HDFCBANK,ICICIBANK --workers 4 --max-memory-gb 4 --resume
bin/gplm discover --universe five-stock --budget standard --workers 4
bin/gplm discover --stock SBIN --budget deep --detach            # background job, survives the terminal
bin/gplm status                       # recent runs
bin/gplm status --run-id <prefix>     # one run: stage, progress, pid, RSS, CPU seconds
bin/gplm status --batch-id <prefix>
bin/gplm stop --run-id <prefix>       # STOP file: halts at the next checkpoint (add --terminate for SIGTERM)
bin/gplm clear-stop --run-id <prefix> # then re-run the same discover command to resume
bin/gplm report --stock RELIANCE [--print]
bin/gplm export --run-id <prefix> --hypothesis <id> --horizon 5 --name my-copy
bin/gplm devices                      # CPU/GPU detection
bin/gplm benchmark
bin/gplm providers check              # key presence only, never values
bin/gplm providers mock-health        # adapter request shaping, no network
```

Once the package is installed (`pip install -e '.[m54]'`), the same commands are available as `gplm ...`. The
shared venv was **not** reinstalled from this worktree, because its editable install points at the main checkout.

Other long-running options are `tmux new -s gplm 'bin/gplm discover ...'` or a `systemd-run --user` unit. Progress is
on disk, so a closed terminal or a dropped AI session loses nothing.

## Data boundaries

- Daily Shoonya `-EQ` files under `../stock-gplm/data/raw/shoonya/NSE`, opened read-only through
  `mvp.data.load_symbol` and `ingestion.daily.validate_daily`.
- Only files whose declared window ends before the **M3.2 cutoff 2025-10-21** are opened. The cutoff comes from the
  manifest-verified split plan pinned in `config/m54/discovery-default.json`. The 2025 and 2026 files are listed
  as withheld and never decoded. A test segment that reaches the cutoff is refused.
- In practice this is 2022-10-31 to 2024-12-31 (about 527 sessions per large-cap).
- Segments: discovery 2022-10-31 to 2024-01-31, validation 2024-02-01 to 2024-07-31, inner test 2024-08-01 to
  2024-12-31. Every event must exit inside its own segment (purging).
- No minute data. The M3.2 locked holdout is never touched. The governed M5 path stays blocked until a trust root is
  approved (see `m54-reconciliation.md`).

## Method

1. **Features** (`m54/indicators.py`): about 50 registered indicators in 7 families. Moving averages are SMA, EMA,
   WMA, HMA, DEMA, TEMA, VWMA and KAMA (distance, slope, acceleration, ATR distance, bounce), plus MA crosses and
   alignment. Momentum covers RSI, MACD, PPO, ROC, CCI, Stochastic, StochRSI, Williams %R, TRIX, TSI and rolling
   divergence. Volume covers relative volume, OBV, A/D, CMF, MFI, PVO and price-volume events. Trend covers ADX/DMI,
   Aroon, Supertrend, Ichimoku, PSAR and Vortex. Volatility covers ATR%, Bollinger, Keltner, Donchian and
   historical volatility. Price action covers breakouts, failed breakouts, retests, gaps, range compression and
   NR/inside bars, structure, candles and support/resistance distance. Statistical features cover autocorrelation,
   return z-score, streaks, skew and volatility clustering. Every value at t uses bars up to t only. A test truncates
   the series and checks that history is unchanged.
2. **Hypotheses** (`m54/hypotheses.py`): an AND of conditions (`gt`, `lt`, `cross_above`, `cross_below`, `is`).
   Thresholds are **discovery-segment quantiles** (or configured levels for bounded oscillators, and zero for signed
   spreads), so validation and test data never shape them and no conventional value is privileged.
3. **Staged search** under a budget (`config/m54/budgets.json`):
   screen (stratified sample across indicators) → parameter-neighbour stability → cross-family AND interactions →
   validation once → inner test once.
4. **Evaluation** (`m54/evaluate.py`): signal at close t, entry at the open of t+1, exit at the open of t+1+h, with
   explicit per-side costs. Statistics use non-overlapping *independent* events. Each evaluation reports mean,
   median, hit rate, quantiles, MFE/MAE, in-trade drawdown, time to ±k·ATR target or failure, a bootstrap CI, a
   one-sided t-test against unconditional entry, a random-entry percentile, compounded non-overlapping returns and
   equity drawdown, regime splits (trend vs SMA, volatility vs the discovery median) and walk-forward folds.
5. **Multiple testing**: every (hypothesis × horizon) evaluated on discovery counts as a test. Benjamini–Hochberg q
   and Bonferroni p are reported over the total.
6. **Statuses**: `REJECTED_SCREEN`, `REJECTED_UNSTABLE_NEIGHBOURS`, `REJECTED_VALIDATION`,
   `*_BUDGET` (not reached), `VALIDATION_PASS`, `TEST_CONSISTENT`, `TEST_INCONSISTENT`, `TEST_INSUFFICIENT_EVENTS`.
   `TEST_CONSISTENT` means "kept a positive net and excess mean on one short window". It is a research lead,
   not edge.

Suspected price discontinuities (gap or move above 20%) are excluded from event paths, because the provider price
basis is unverified.

## Outputs (per stock, under `reports/m54/runs/<SYMBOL>/<run_id>/`)

`report.md`, `findings.json`, `results.csv` (every evaluation with its status), `events.csv` (finalists' events
in all segments), `run-manifest.json` (output hashes and governance flags), `identity.json` (everything the
run id covers), `status.json`, `stages/*.json` (integrity-hashed checkpoints) and `discovered/*.json` (immutable
discovered defaults for validation passers). Batches live in `reports/m54/batches/<batch_id>/` with
`batch-manifest.json`, `summary.csv` and per-symbol `errors/`.

## Reproducibility and resume

The run id hashes the resolved configuration (budget and search space included), the symbol, the bar hash, the
holdout pin, the code hash and any hypothesis file. Identical inputs give byte-identical `report.md`,
`findings.json`, `results.csv` and `events.csv` (tested). Screening is checkpointed in chunks. A STOP file, a crash
or a disconnect resumes from the last chunk. A completed run is verified against its manifest and reused. Edited
evidence or a corrupt checkpoint is refused, not silently recomputed.

## Strategy library

Validation passers are written as immutable `discovered/<id>-h<h>.json` (`stock-gplm.m54-pattern-strategy.v1`,
`RESEARCH_ONLY_NOT_FOR_TRADING`, with provenance and feature definitions). `gplm export` makes an editable user
copy under `config/m54/strategies/user/` and never overwrites one. The original stays in the run. Copies and
hand-written or AI-proposed files (`stock-gplm.m54-hypotheses.v1`, see `config/m54/hypotheses/example.json`) are
re-tested with `--hypotheses` and count as tests. The existing V1 strategy library is unchanged.

## CPU/GPU

Measured on this workstation (WSL2): 28 logical CPUs, about 31 GiB visible to WSL, and an RTX 5070 (12 GiB, driver
610.88) visible through `nvidia-smi`. No CUDA framework (torch, cupy, cudf, xgboost) is installed in the venv. Feature
build for about 950 features takes about 0.2 s, and one evaluation about 1 ms (`gplm benchmark`). A standard
single-stock run takes about 60 s and about 340 MB, and five stocks with 4 workers take about 66 s. The workload is too
small for a GPU to pay off, so `--device auto` resolves to the CPU with the reason recorded, and `--device cuda` is
refused rather than silently ignored. Results never depend on the device. `--max-memory-gb` sets a per-process
address-space limit, and workers use one BLAS thread each.

## Optional AI providers

`config/research/providers.json` lists MWAPI and StartupAPI (Anthropic-compatible), OpenRouter (OpenAI-compatible,
`openrouter/auto`), Ollama (local), OpenAI and Anthropic (adapters available), and Gemini (declared future
adapter). All are disabled, and the registry is `offline: true`. A live call needs `offline: false`,
`enabled: true`, `live_calls_authorized: true`, a configured key and `--allow-live`. Per-run request, token and USD
caps apply on top of the client's daily limits. Remote prompts may not contain market rows, and the AI prompt sends
only the feature catalog. Pricing figures in the file are placeholders and must be set before any paid use.

Credentials: `GPLM_*_API_KEY` variables in the git-ignored `.env` of the main checkout. A linked worktree finds it
through its `.git` file, or `GPLM_ENV_FILE` can be set. The file must be mode 0600 and owned by the user. Only
`GPLM_*` names are loaded. Values are never logged, printed, written to reports or put into config.

## Unsupported (documented, not fabricated)

Market-relative features (relative strength, beta, correlation, sector momentum, broad regimes) need verified index
and sector series. Delivery volume needs NSE delivery data. VWAP needs minute data. Pivot-based divergence needs
confirmation-lag rules. Universes larger than an explicit list need a universe file. Lists based on current
membership must be labelled `CURRENT_CONSTITUENT_DIAGNOSTIC` and are survivorship-biased.

## Known limitations

- About two years of pre-cutoff history. Horizons above 60 sessions are not meaningful, and 120 is excluded from
  every budget.
- The inner-test window was already exposed to earlier diagnostics (MVP and M5.3) over 2024.
- Costs are assumptions. Price basis, calendar completeness and corporate-action coverage are unknown.
- Single-stock results are survivorship-biased (currently listed names).
