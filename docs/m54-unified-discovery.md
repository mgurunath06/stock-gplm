# M5.4 unified daily + minute strategy discovery

One local command discovers, backtests, validates and ranks stock-specific strategies across the
1m, 5m, 15m, 30m, 60m, daily and weekly timeframes. It needs no network, LLM, Claude or Codex: every
calculation runs on the local CPU. Every output is `DIAGNOSTIC_ONLY_NON_INVESTABLE`.

```bash
bin/gplm discover --stock RELIANCE --data all-available --timeframes auto --budget standard --device auto --resume
bin/gplm discover --stocks RELIANCE,HDFCBANK,ICICIBANK,INFY,SBIN --data all-available --budget standard --workers 5 --max-memory-gb 4
bin/gplm discover --universe all-eligible --data all-available --budget quick --workers 8 --dry-run   # every -EQ dir minus ETF-like names
bin/gplm report --stock RELIANCE --print
bin/gplm library list --stock RELIANCE
bin/gplm library export --strategy-id <id> --name my-variant          # editable copy; the default stays read-only
bin/gplm stop --run-id <prefix>   /   bin/gplm clear-stop --run-id <prefix>
```

Passing `--data` or `--timeframes` selects this engine. Without them, `gplm discover` runs the earlier
daily-only engine unchanged. Other options: `--timeframes 5m,15m,1d`, `--data daily|minute`,
`--start/--end` (these can only narrow the permitted range), `--recency validated|uniform`, `--workers`,
`--max-memory-gb`, `--detach`, `--hypotheses <file>`, `--daily-root/--minute-root/--unified-config`.

## Data and governance

`stock_gplm.m54.candles` inventories every partition under the daily root
(`stock-gplm/data/raw/shoonya/NSE/<SYM>/`) and both minute roots (`…/NSE/minute/<SYM>/` and the
M3.4 tree `…/shoonya-minute/NSE/<SYM>/`). One access policy covers all of them:

- **`M32_LOCKED_HOLDOUT`** (real data). The cutoff is the earliest locked-holdout session of the
  manifest-verified M3.2 split plan (**2025-10-21**). A partition whose window ends on or after the
  cutoff is excluded whole, and its payload is never opened. Only its marker and meta sidecars are read,
  for coverage. The control is file-level, as in M5.4. It is not relaxed to row filtering.
- **`SYNTHETIC_FIXTURE`**. Accepted only when every data root carries a `SYNTHETIC_FIXTURE.json` marker
  and no root lies inside the real raw tree. `gplm fixture` writes such data in the exact
  `ShoonyaDailyV1` / `ShoonyaMinuteV1` triplet formats.

Permitted daily files pass `validate_daily`. Permitted minute files pass the M3.4 `verify_publication`
and `validate_segment` with the observed session policy: ACCEPT and REVIEW are used, QUARANTINE and REJECT
are listed. A session present in two roots is de-duplicated when the declared hashes are identical. When
they differ, the session is marked `CONFLICTING_DUPLICATE` and neither copy is used. Nothing is filled or
repaired. Minute aggregates are reconciled against daily candles and the differences are reported.

**Status on 2026-11-15:** every local minute file (27,110 files, 2,690 symbols, 2026-01-01 → 2026-11-13)
is after the cutoff, so real-data runs use daily candles only (2022-10-31 → 2024-12-31, 527 bars for
RELIANCE). The 2025 daily file straddles the cutoff and stays withheld. Production acceptance of the
minute path is **BLOCKED** on the authorization described in
[the 2026 study draft](m54-minute-2026-study-authorization-DRAFT.md). The minute path is proven on the
synthetic fixture.

## Timeframes and causality

- Minute bars are resampled into session-anchored buckets from 09:15. A bucket missing source minutes is
  built from what exists and flagged `incomplete`, and signals are suppressed on it. Weekly bars group
  sessions by ISO week.
- Each bar has `end_ns` (its values are final) and `avail_ns` (when another timeframe may read it).
  Daily candles reach intraday bars from 00:00 IST of the next day, because the provider's publication
  time is unknown. A weekly bar is readable from the later of its last close and Friday 15:30 IST of its
  week, so a still-open final week is never read. Higher-timeframe features join with *latest
  avail_ns ≤ base bar end_ns*. A test shows that features are unchanged when future bars are removed.
- Price discontinuities (a gap above 20%, or an intraday bar move above 5%) block any trade path that
  touches them and blank features for `max_period` bars afterwards. Corporate actions remain `UNKNOWN`.

## Search and validation (per lane, then per symbol)

1. Features: about 50 indicator families over the `search-space.json` grids (capped by the budget's
   `max_period`). Intraday lanes add session VWAP, opening range, minute of session and opening gap.
   The higher-timeframe confirmation set comes from `htf_search_space`. Results are cached on disk by
   content hash.
2. Screen: single conditions with thresholds from discovery quantiles or configured levels. Half the
   budget goes to features with the highest discovery rank-IC (sampled every h bars); the other half is
   stratified across indicators. Every lane horizon is tried with a time exit.
3. Refine (parameter-neighbour stability), interactions (pairs from different families), cross-timeframe
   (strongest base conditions AND higher-timeframe conditions).
4. Exit grid per finalist: holding multiple × stop ATR × target ATR × trailing ATR. The pick needs a
   minimum trade count. Exit-neighbour stability is recorded.
5. Walk-forward inside discovery: exits are re-selected on earlier folds and scored on the next fold.
6. Validation, scored once: the frozen strategies, plus selection of the recency scheme.
7. Inner test, scored once: validation passers only. It never changes a choice.

Fills: entry at the next bar's open. The stop is checked first; a gap fills at the open, and a bar that
touches both stop and target counts as the stop. Intraday lanes never enter across sessions and are flat
at the last bar of the session. A trade must finish inside its segment. Costs are per-profile
assumptions (`delivery`, `intraday`). Statistics test excess net return over the same exit policy
entered on every eligible bar. **Intraday statistics are clustered by session**, so minute bars cannot
pose as independent observations. Benjamini-Hochberg and Bonferroni adjustments cover every discovery
test of every lane of the symbol.

## Recency

Three separate concepts are configured separately. **Observation age** is measured in sessions before
the end of the training segment. **Candle granularity** is the lane timeframe. **Prediction horizon** is
the holding period in bars. The schemes are uniform, exponential with half-lives of 20, 60, 120 and 250
sessions, regime-match, and exponential × regime. A scheme ranks strategies on discovery, and the scheme
whose top-K earn the most on validation is selected. Weights change rankings only, never p-values. The
report compares each scheme with uniform.

## Evidence tiers

`ROBUST_CANDIDATE` needs every one of these: FDR survival, a validation pass, inner-test consistency,
walk-forward positive in at least half the folds, and stable entry and exit parameters. The other tiers
are `PROMISING_NOT_SIGNIFICANT`, `VALIDATION_ONLY`, `REJECTED_TEST`, `REJECTED_VALIDATION` and
`EXPLORATORY` (fallback candidates that never passed the screen). The report states "NO ROBUST STRATEGY
FOUND" when that is the result, and lists failure reasons for every candidate.

## Outputs

`reports/m54/unified/<SYM>/<run_id>/` holds `report.md` (sections A–R), `strategies.json`
(`stock-gplm.m54-strategy.v2`), `results.csv`, `trades.csv`, `coverage.json`, `run-manifest.json`, and
checkpoints in `stages/<tf>/`. Read-only library defaults are in `reports/m54/library/defaults/<SYM>/`.
The run id hashes the config, the code, the policy, the input fingerprints and the lane plan. Any change
creates a new run, and old runs remain.

## Performance

The CPU path forks one pool per batch of evaluations. Single-stock runs parallelise inside a stage;
batches parallelise across stocks, with one thread each and an address-space cap. Measured: a real
RELIANCE standard run takes 4 s (repeat 0.7 s); five stocks take 4.3 s; the synthetic daily + 5m/15m/30m/60m
standard run takes 30 s (repeat 1.8 s). `--device auto` resolves to CPU: no CUDA framework is installed,
and no stage has a benchmark that justifies a GPU.
