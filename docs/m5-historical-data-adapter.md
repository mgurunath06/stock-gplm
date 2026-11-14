# M5 historical NSE data adapter and real-data strategy backtesting

M5 connects immutable V1 strategy versions to genuine local Shoonya NSE daily
history without letting a UI flag, a client request or a diagnostic run bypass
provenance. It is historical research only. It places no orders, routes nothing
to a broker, reads no credentials, collects no candles, uses no network, and
writes nothing inside raw-data directories.

```
published segments ──► HistoricalDataAdapter ──► DataEligibilityDecision (+ VerifiedHistoricalDataset)
reference evidence ──┘                                   │
immutable strategy version ──► shared V1 evaluator ──► generic portfolio simulator ──► metrics + benchmark
                                                                 └──► immutable run directory (JSON + Markdown)
```

## Components

| Module | Responsibility |
| --- | --- |
| `stock_gplm.m5.models` | Typed request, failure, evidence, decision, canonical bar and dataset contracts |
| `stock_gplm.m5.evidence` | Read-only loaders for the holdout, calendar, listing, alias, corporate-action, master and ETF-heuristic evidence |
| `stock_gplm.m5.adapter` | `HistoricalDataAdapter.assess`: the only component that releases historical bars |
| `stock_gplm.m5.discovery` | Read-only coverage inventory, optionally re-running `validate_daily` on every segment |
| `stock_gplm.m5.runner` | Causal per-instrument signal evaluation, simulation, metrics, atomic run publication, run comparison |
| `stock_gplm.m5.studio_api` | Server-side historical workflow behind the loopback Strategy Studio |
| `stock_gplm.m5.cli` | `discover`, `assess`, `backtest`, `compare` |
| `stock_gplm.v1.portfolio` | Generic multi-instrument long-only simulator (cash, exposure, costs, equity curve) |
| `stock_gplm.v1.metrics` | Performance statistics with explicit undefined reasons, and the same-universe benchmark |
| `stock_gplm.v1.execution` | Shared next-open and resting stop/target rules |

## Existing contracts reused (no second truth system)

| Evidence | Contract |
| --- | --- |
| Publication integrity and content | `stock_gplm.ingestion.daily.validate_daily` (M1/M2.4 `.done` marker, SHA-256, byte and record counts, `ShoonyaDailyV1`) |
| Locked holdout | M3.2 `split-plan.json`, verified against its `run-manifest.json` hash. The cutoff is the earliest holdout start across horizons. |
| Trading calendar | M2.5-R `stock-gplm.m25r-registered-calendar.v1`, verified by format: canonical-hash identity, NSE HTTPS source, manual browser acquisition, explicit status for every date |
| Listing history, symbol mappings, corporate-action coverage | M2.1 `stock-gplm.listing-history.v1`, `…historical-symbol-mappings.v1`, `…corporate-actions.v1` through `stock_gplm.m2.reference_data` |
| Current identity | M2.2-accepted Shoonya master (`read_shoonya_nse_eq_inventory_v1`) |
| Corporate-action economics | M3.9-B validated, licence-approved `CorporateActionEvent` records (Python API only; M3.9-B has no file loader) |
| ETF exclusion | M3.7 `etf-symbol-heuristic.json`, recorded as `HEURISTIC`, not as evidence |

The M2.5-R calendar format was checked against the real M2.5-R implementation: a
synthetic registration produced by `stock_gplm.m25r.calendar.register_calendar`
(run read-only from the recovery worktree, output in a temporary directory) loads
in `load_registered_calendar` with the same identifier and statuses.

## Eligibility

`DataEligibilityRequest(symbol, start, end, mode, expected_token=None)` is
assessed in this order. Every failure has a fixed class.

| Gate | Failure codes | Class |
| --- | --- | --- |
| Series, ETF, holdout boundary and overlap, M2.4 publication lock, symbol directory | `UNSUPPORTED_SERIES`, `ETF_EXCLUDED`, `HOLDOUT_BOUNDARY_UNVERIFIED`, `HOLDOUT_OVERLAP`, `PUBLICATION_LOCK_ACTIVE`, `SYMBOL_NOT_FOUND` | INTEGRITY |
| Identity | `IDENTITY_MISMATCH` / `IDENTITY_EVIDENCE_MISSING` | INTEGRITY / EVIDENCE |
| Publication of every overlapping segment | `UNEXPECTED_FILE`, `ORPHAN_MARKER`, `UNPUBLISHED_SEGMENT`, `SEGMENT_REJECTED`, `SEGMENT_QUARANTINED`, `METADATA_MISMATCH`, `QUARANTINED_SOURCE`, `CONTRADICTORY_OBSERVATION`, `NO_OBSERVATIONS` | INTEGRITY |
| Publication windows cover the request | `PUBLICATION_COVERAGE_GAP` | EVIDENCE |
| Point-in-time listing membership | `PIT_MEMBERSHIP_MISSING` / `LISTING_CONTRADICTION` | EVIDENCE / INTEGRITY |
| Registered calendar | `CALENDAR_EVIDENCE_MISSING`, `MISSING_SESSIONS` / `CALENDAR_CONTRADICTION` | EVIDENCE / INTEGRITY |
| Alias history | `ALIAS_EVIDENCE_MISSING`, `ALIAS_STITCHING_UNSUPPORTED` | EVIDENCE |
| Provider price basis | `PROVIDER_PRICE_BASIS_UNVERIFIED` | EVIDENCE |
| Corporate actions | `CORPORATE_ACTION_COVERAGE_MISSING`, `CORPORATE_ACTION_EVENT_UNRESOLVED`, `CORPORATE_ACTION_UNSUPPORTED` | EVIDENCE |

Decision status:

- any INTEGRITY failure: `REJECTED` in every mode; no bars are released;
- `STRICT_RESEARCH` with any EVIDENCE failure: `BLOCKED`; no bars are released;
- `STRICT_RESEARCH` with no failure: `ELIGIBLE` (the only investable status);
- `DIAGNOSTIC_ONLY` without INTEGRITY failures: `DIAGNOSTIC_ALLOWED`, with every
  absent component listed in `missing_evidence`.

There is no `force`, `skip_validation` or equivalent parameter. A decision's
`decision_id` is the canonical SHA-256 of its full content, including
`bars_sha256` for the released bars. `verify()` recomputes the identifier, the
status implied by the failures, the bars hash and the holdout cutoff. The runner
calls `verify()` on every dataset, so editing a decision or swapping bars after
release fails.

Rules that are never inferred: listing dates from first candles, holidays from
missing weekdays, aliases from matching prices, corporate actions from gaps.
Price gaps of 20% or more are reported by M3.9-B
`detect_suspicious_discontinuities` with cause `UNKNOWN`.

### Provider price basis

Genuine Shoonya history is not uniformly raw. RELIANCE-EQ's 2024 segment
(fetched 2026-10-30) is back-adjusted for the October 2024 1:1 bonus: the January
2024 closes are near ₹1,295, and there is no gap at the ex-date. M3.8 separately
found 65 probable *unadjusted* splits/bonuses. M3.9-B may only adjust
`RAW_UNADJUSTED` series, so M5 applies share events only when
`ReferenceEvidence.provider_price_basis == "RAW_UNADJUSTED"`. No file contract
establishes that basis yet. Real-data datasets therefore carry the basis
`PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED`, and strict research stays blocked
by `PROVIDER_PRICE_BASIS_UNVERIFIED`.

### Locked holdout

The adapter requires a hash-verified M3.2 split plan. For run `26e41d00…` the
cutoff is **2025-10-21** (the 6–12 month horizon; shorter horizons start
2026-04-29). A request ending on or after the cutoff is `REJECTED`. Segments
starting on or after it are never opened. Rows on or after it in straddling
segments are dropped before any bar is released, and their count is recorded per
segment (`holdout_rows_withheld`).

## Canonical bars

`CanonicalDailyBar` keeps the provider's exact decimal strings, integer volume,
the provider timestamp (`ssboe` UTC), the Asia/Kolkata session date and the
source segment file. Missing sessions are never forward-filled; no volume or
price is synthesized. Raw files are opened read-only, and the M2.4 lock is only
probed with an advisory `flock`, as M3 does. Only daily bars are supported. The
model has a session-timezone field so minute bars can be added later without
reinterpreting daily data.

## Strategy execution

`run_historical_backtest(strategy_version, datasets, settings, output_root)`:

1. Re-validates the stored configuration and requires its hash to equal the
   immutable version's hash.
2. Refuses mixed modes, and strict runs with any non-investable dataset. The
   evaluation range must lie inside every assessed range. Symbols must be inside
   the strategy's universe when the strategy lists one.
3. Evaluates each instrument with the shared V1 evaluator over all released bars
   (bars before `evaluation_start` serve only as indicator warm-up). When validated
   share events exist on a verified raw basis, signals are re-evaluated per
   as-of regime. Each decision date sees only events whose ex-date has passed and
   whose `available_at` is no later than that session's close (15:30 IST), so a
   later split never rescales an earlier decision.
4. Simulates on the union of released session dates with the generic simulator.
5. Computes metrics and the same-universe equal-weight buy-and-hold benchmark.
6. Publishes `run.json`, `signals.json`, `orders.json`, `trades.json`,
   `equity-curve.json`, `report.md` and `run-manifest.json` into
   `<output_root>/<run_id>/`. Files are staged and then renamed in one step. An
   existing run directory is never overwritten, and outputs under a raw root are
   refused.

`run_id` is the canonical hash of the strategy ID, version and configuration
hash, the dataset fingerprints, the settings, the simulator version and the code
identity (git commit, dirty flag and a hash of the V1/M5 sources). Identical
inputs and code reproduce identical analytical files. Only `created_at_utc` in
`run-manifest.json` is non-analytical.

### Simulator conventions

- Long only, whole shares at entry, no margin: cash never goes negative, and
  gross exposure is capped by `max_gross_exposure` (at most 1).
- Entry size = `min(strategy fixed_fraction, max_position_fraction)` × equity at
  the previous close, trimmed to the remaining exposure room and to available
  cash, at most `max_open_positions`. One position per instrument; repeated
  signals never duplicate a position.
- Decisions at a completed close execute at the next observed open. A missing
  entry session cancels the entry (`ENTRY_SESSION_MISSING`). A missing exit
  session defers the exit to the next observed open.
- Resting stop/target levels are measured from the entry fill. A gap through the
  stop fills at the open. A gap through the target is capped at the target. When
  both are touched in one bar, the stop fills first.
- Costs: strategy `commission_bps` and `slippage_bps` per side, plus settings
  `buy_statutory_bps`/`sell_statutory_bps`. The settings' `cost_basis` text is
  recorded verbatim.
- Validated share events rescale the held quantity and price levels at the
  ex-date; cash dividends are excluded (price return).
- Positions open at the range end stay open, marked at the last close, and are
  reported separately; they are never converted into trades.

### Metrics

Signals, trades, wins/losses, win rate, gross and net P&L, unrealized P&L, total
return (including the final marks), annualized return (only with at least 252
sessions), maximum drawdown, average and median trade return, profit factor
(undefined without losses), average holding sessions, average gross exposure,
share of sessions with a position, turnover and ending equity. Sharpe and Sortino
are reported only with at least 252 sessions and 30 closed trades (risk-free rate
0, √252 annualization). Every undefined metric is `null` with a reason.

The benchmark buys every instrument with a bar on the first evaluated session in
equal weights at that open (slippage and fees applied) and marks it at its last
in-range close. It is a same-universe reference, not a market index.

`compare_runs` ranks only when every run is `STRICT_RESEARCH` and the runs share
eligibility mode, dataset fingerprint, evaluation range, cost model and portfolio
settings. Otherwise it returns the reasons and `ranking_by_total_return: null`.

## Diagnostic-only results

Diagnostic runs are classified `DIAGNOSTIC_ONLY_NON_INVESTABLE`, with
`investable`, `ranking_eligible`, `promotion_eligible`, `paper_trading_eligible`
and `ml_training_eligible` all false. Their Markdown report states that they are
not validated historical performance. They never touch the M3.5 trial ledger,
model artifacts or the locked holdout. Diagnostic mode does not relax any
INTEGRITY gate.

## Strategy Studio

Start the Studio with historical research enabled (loopback only):

```bash
.venv/bin/python -m stock_gplm.v1.studio --registry /local/research/studio.sqlite3 \
  --m5-evidence config/m5/evidence-paths.example.json \
  --raw-root data/raw/shoonya/NSE --m5-reports /local/research/m5-runs
```

The "Historical research (M5)" panel discovers datasets, requests eligibility,
shows decisions, failures and warnings, runs a backtest of the selected immutable
version, lists runs, compares them and exports the Markdown report. Endpoints:
`GET /api/m5/datasets`, `POST /api/m5/eligibility`, `POST /api/m5/backtests`,
`GET /api/m5/runs`, `GET /api/m5/runs/<run_id>`, `POST /api/m5/compare`. Request
bodies must contain exactly the documented fields, so a client cannot send a
status, eligibility flag or skip switch. Requests are capped at 25 symbols and one
running backtest at a time (HTTP 429 otherwise). Without `--m5-evidence` the
endpoints return 503. Editing a strategy saves a new version; earlier runs keep
their exact version and hash.

## Command line

```bash
# read-only coverage inventory (add --verify to re-run validate_daily on every segment)
.venv/bin/python -m stock_gplm.m5.cli discover --verify --output reports/m5/discovery.json

# one eligibility decision
.venv/bin/python -m stock_gplm.m5.cli assess --evidence config/m5/evidence-paths.example.json \
  --mode STRICT_RESEARCH --symbol RELIANCE-EQ --start 2022-10-31 --end 2025-10-20

# a backtest (exit status 2 when no dataset is usable)
.venv/bin/python -m stock_gplm.m5.cli backtest --evidence config/m5/evidence-paths.example.json \
  --mode DIAGNOSTIC_ONLY --start 2022-10-31 --end 2025-10-20 --symbols RELIANCE-EQ TCS-EQ \
  --settings config/m5/backtest-settings.example.json --registry reports/m5/registry.sqlite3 \
  --strategy-file config/v1/strategies/swing-breakout-v1.json --output-root reports/m5/runs

.venv/bin/python -m stock_gplm.m5.cli compare reports/m5/runs/<run_a> reports/m5/runs/<run_b>
```

`config/m5/evidence-paths.example.json` resolves paths relative to its own
directory. Add `calendar_registration`, `listing_history`, `symbol_mappings` and
`corporate_actions` entries (M2.1 payload/manifest pairs) once genuine evidence
has been downloaded manually and registered.

## Limitations

- Strict real-data eligibility currently cannot pass. No calendar, listing,
  alias or corporate-action evidence is registered, and no contract yet
  establishes the provider price basis.
- Alias stitching across renamed symbols is unsupported and blocks.
- Rights, mergers, demergers and other complex actions block; cash dividends are
  excluded (price return).
- The benchmark is a same-universe reference; no index data is available.
- Statutory costs are the M3.5 policy's labelled assumptions; market impact
  beyond slippage is not modelled.
- Daily bars only.
