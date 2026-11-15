# Five-stock diagnostic MVP (`stock_gplm.mvp`)

Status: working, `DIAGNOSTIC_ONLY_NON_INVESTABLE`. Results are not rankable and are
not evidence that a strategy works.

## What it does

`stock_gplm.mvp` runs config-driven V1 strategies over pre-cutoff daily candles. It
composes existing code: `ingestion.daily.validate_daily` (hashes, sidecars,
`ShoonyaDailyV1` decoding), and the V1 schema, evaluator, portfolio simulator and
metrics. The only new pieces are:

- `mvp.data`: pins the locked-holdout cutoff from a manifest-verified M3.2 split
  plan (earliest `first_as_of` across all horizons). It opens only segments whose
  declared window **ends before** the cutoff. Straddling and later segments are
  listed and never read.
- `mvp.experiment`: an experiment file (JSON or the V1 YAML subset) defines the
  symbols, strategy files, horizon profiles, portfolio and costs. Each
  (strategy, horizon) pair is one run, and its `run_id` is the SHA-256 of the
  code, the variant config, input bar hashes, the holdout pin, costs and the
  window. A completed run (`runs/<id>/run.json`) is skipped on re-execution, so
  interrupted batches resume. A run directory is staged and then renamed into
  place atomically.
- `mvp.research`: rolling-window and recency-weight (equal or exponential
  half-life) interfaces, and a causal daily+intraday join protocol. Walk-forward
  folds and purge/embargo stay in `m3.splits`.
- V1 indicators added: `RSI` (Wilder), `MACD` (`fast`, `period`=slow, `signal`,
  `output` = line/signal/histogram), `ATR` (Wilder) and `VOLATILITY` (sample
  standard deviation of daily log returns, not annualized). The normalized form
  of existing indicators is unchanged, so earlier config hashes are stable.

Horizons override `exit.max_holding_sessions` and re-validate the strategy, so
each variant has its own config hash. A strategy whose `universe.symbols` is
non-empty runs only on those symbols. User copies live in
`config/v1/strategies/user/` with their own id. Every base config is recorded in
an append-only `strategy-registry.sqlite3` under the output root.

## Reproduce

```bash
cd /home/mgurunath/projects/stock-gplm-claude
PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m stock_gplm.mvp.cli run \
  --experiment config/mvp/five-stock-diagnostic.json \
  --output-root reports/mvp5/five-stock-diagnostic-v1
```

Outputs: `data-manifest.json`, `summary.{csv,json}`, `report.md`, `progress.json`,
plus `runs/<run_id>/{run.json,trades.csv,orders.csv,equity.csv,per-symbol.csv,strategy-variant.json}`.

## Data limits

- Opened windows: 2022-10-31 to 2024-12-31. The 2025 segment straddles the
  2025-10-21 cutoff and stays unopened, and 2026 segments follow the cutoff.
- The M5 runner (`stock_gplm.m5.cli backtest`) rejects every symbol with
  `HOLDOUT_BOUNDARY_UNVERIFIED` until a governed trust root is installed, even in
  `DIAGNOSTIC_ONLY`. Installing one needs separate approval. This MVP does not
  bypass that gate. It is a separate diagnostic path under the M3.5 daily-data
  rule and opens no protected or straddling payload.
- Price basis is unverified. RELIANCE shows no break at the 2024 1:1 bonus, which
  suggests provider back-adjustment, and shows −10.7% on 2023-07-21, consistent
  with the Jio Financial demerger (warm-up period, before evaluation). No
  corporate-action adjustment is applied.
- Costs are assumptions copied from the M5 example settings.
- The session axis is the union of observed dates. No calendar is inferred.
