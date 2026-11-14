# M5 historical NSE data adapter and real-data strategy backtesting

M5 connects immutable V1 strategy versions to genuine local Shoonya NSE daily
history without letting a UI flag, a client request or a diagnostic run bypass
provenance. It is historical research only. It places no orders, routes nothing
to a broker, reads no credentials, collects no candles, uses no network, and
writes nothing inside raw-data directories.

M5.1 (see `docs/m51-integrity-hardening.md`) adds trusted issuance of eligibility,
all-or-nothing requested universes, corporate-action reconciliation, one verified
run loader, portable provenance, programmatic loopback enforcement in the Studio,
one terminal valuation policy and precise holdout accounting. M5.1.2 replaces
the M5.1.1 decode-before-cutoff validator: only segments whose metadata-verified
declared window ends before the cutoff are opened (see
`docs/m512-critical-remediation.md`). M5.1.3 makes the cutoff itself governed:
only the M3.2 split plan named by the administrative trust root defines it (see
`docs/m513-acceptance-remediation.md`).

```
published segments ──► HistoricalDataAdapter ──► DataEligibilityDecision (+ VerifiedHistoricalDataset)
reference evidence ──┘                                   │
immutable strategy version ──► shared V1 evaluator ──► generic portfolio simulator ──► metrics + benchmark
                                                                 └──► immutable run directory (JSON + Markdown)
```

## Components

| Module | Responsibility |
| --- | --- |
| `stock_gplm.m5.models` | Typed request, failure, evidence, decision, canonical bar, dataset, `UniverseRequest` and `UniverseAssessment` contracts |
| `stock_gplm.m5.issuance` | Process-local HMAC seals that mark adapter-issued decisions and file-loaded evidence |
| `stock_gplm.m5.evidence` | Read-only, sealed loaders for the holdout, calendar, listing, alias, corporate-action, master, ETF-heuristic, price-basis and adjustment-event evidence |
| `stock_gplm.m5.authority` | Source-document registrations and separately hash-pinned reviewer trust policy; self-asserted approval is never authoritative |
| `stock_gplm.m5.adapter` | `HistoricalDataAdapter.assess` / `assess_universe`: the only component that releases historical bars |
| `stock_gplm.m5.holdout_validation` | M5.1.2 partition gate: verifies filename/`.meta.json` windows before opening, opens only wholly pre-cutoff segments, rejects all others unopened |
| `stock_gplm.m5.provenance` | Portable content identity (in `run_id`) and environmental provenance (recorded only) |
| `stock_gplm.m5.run_store` | The one verified run loader: listing, details, reports, export, comparison, replay |
| `stock_gplm.m5.discovery` | Read-only coverage inventory; `--verify` needs `--m32-run` and validates only pre-cutoff segments |
| `stock_gplm.m5.runner` | Causal per-instrument signal evaluation, simulation, metrics, atomic run publication, run comparison |
| `stock_gplm.m5.studio_api` | Server-side historical workflow behind the loopback Strategy Studio |
| `stock_gplm.m5.cli` | `discover`, `assess`, `backtest`, `compare`, `export` |
| `stock_gplm.v1.portfolio` | Generic multi-instrument long-only simulator (cash, exposure, costs, equity curve) |
| `stock_gplm.v1.metrics` | Performance statistics with explicit undefined reasons, and the same-universe benchmark |
| `stock_gplm.v1.execution` | Shared next-open and resting stop/target rules |

## Existing contracts reused (no second truth system)

| Evidence | Contract |
| --- | --- |
| Publication integrity and content | `stock_gplm.ingestion.daily.validate_daily` for segments wholly before the cutoff; M5's isolation wrapper preserves whole-file `.done`, SHA-256, byte and record checks while validating candle content only before the cutoff |
| Locked holdout | M3.2 `split-plan.json`, verified against its `run-manifest.json` hash. The cutoff is the earliest holdout start across horizons. |
| Trading calendar | M2.5-R `stock-gplm.m25r-registered-calendar.v1`, verified by format: canonical-hash identity, NSE HTTPS source, manual browser acquisition, explicit status for every date |
| Listing history, symbol mappings, corporate-action coverage | M2.1 `stock-gplm.listing-history.v1`, `…historical-symbol-mappings.v1`, `…corporate-actions.v1` through `stock_gplm.m2.reference_data` |
| Current identity | M2.2-accepted Shoonya master (`read_shoonya_nse_eq_inventory_v1`) |
| Corporate-action economics | M3.9-B validated, licence-approved `CorporateActionEvent` records, read from a `stock-gplm.m51-adjustment-events.v1` file in their own `to_dict` shape |
| Provider price basis | `stock-gplm.m51-provider-price-basis.v1` reviewed registration (symbols, date range, hash-bound identifier); none exists for real data |
| ETF exclusion | M3.7 `etf-symbol-heuristic.json`, recorded as `HEURISTIC`, not as evidence |
| Evidence authority, ETF/equity classification and cost schedule | M5.1.1 registrations approved only through the M5.1.2 OS-governed trust root; see `docs/m511-evidence-authority.md` and `docs/m512-critical-remediation.md` |

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
| Trusted issuance (M5.1) | `EVIDENCE_NOT_ISSUED`, `CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED` | EVIDENCE |
| Provider price basis | `PROVIDER_PRICE_BASIS_UNVERIFIED` | EVIDENCE |
| Corporate actions | `CORPORATE_ACTION_COVERAGE_MISSING`, `CORPORATE_ACTION_EVENT_UNRESOLVED`, `CORPORATE_ACTION_UNSUPPORTED`, `CORPORATE_ACTION_STATUS_UNRESOLVED` | EVIDENCE |
| Corporate-action reconciliation (M5.1) | `CORPORATE_ACTION_CONTRADICTION`, `CORPORATE_ACTION_AMBIGUOUS`, `CORPORATE_ACTION_FIELDS_MISSING`, `CORPORATE_ACTION_DETAILS_UNSUPPORTED`, `CORPORATE_ACTION_EVENT_UNMATCHED` | INTEGRITY |

Decision status:

- any INTEGRITY failure: `REJECTED` in every mode; no bars are released;
- `STRICT_RESEARCH` with any EVIDENCE failure: `BLOCKED`; no bars are released;
- `STRICT_RESEARCH` with no failure: `ELIGIBLE`. It is investable only when the
  decision also carries this process's adapter seal (`adapter_issued`);
- `DIAGNOSTIC_ONLY` without INTEGRITY failures: `DIAGNOSTIC_ALLOWED`, with every
  absent component listed in `missing_evidence`.

There is no `force`, `skip_validation` or equivalent parameter. A decision's
`decision_id` is the canonical SHA-256 of its full content, including
`bars_sha256` for the released bars, the evidence digest and source hashes, the
price basis, the released share events and the corporate-action reconciliation.
A matching identifier proves only internal consistency. Trust comes from the
seal, which a caller cannot produce (see `docs/m51-integrity-hardening.md`). `verify()` recomputes the identifier, the
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
`RAW_UNADJUSTED` series, so M5 applies share events only when a reviewed
`stock-gplm.m51-provider-price-basis.v1` registration with basis `RAW_UNADJUSTED`
covers the symbol and the requested range. No such registration exists for the
Shoonya history, and none may be fabricated. Real-data datasets therefore carry
the basis `PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED`, and strict research stays
blocked by `PROVIDER_PRICE_BASIS_UNVERIFIED`.

### Locked holdout

The adapter requires a hash-verified M3.2 split plan. For run `26e41d00…` the
cutoff is **2025-10-21** (the 6–12 month horizon; shorter horizons start
2026-04-29). A request ending on or after the cutoff is `REJECTED`. Segments
starting after the request end are never opened.

Partitioning (M5.1.2). A record's date cannot be read without decompressing and
parsing the payload, so payload dates never decide whether a payload is opened.
Before any payload byte is read, the segment's filename window and its
`.meta.json` `from`/`to`/`symbol` must agree exactly; missing or inconsistent
partition metadata is `METADATA_MISMATCH`. A segment is opened only when that
declared window ends strictly before the cutoff. A segment that reaches the
cutoff, including one that straddles it, is `SEGMENT_REJECTED` with
`holdout_partition_required` and its payload is never read. Opened segments go
through the unchanged `validate_daily`: a malformed row, or a row dated outside
the declared window, quarantines the whole segment with each row-level issue in
the failure message. Nothing is skipped.

Every run records:

| Field | Meaning |
| --- | --- |
| `partition_policy` | `DECLARED_WINDOW_STRICTLY_BEFORE_CUTOFF_VERIFIED_BEFORE_OPEN` |
| `holdout_authority` | governed split-plan and trust-root SHA-256; cross-checked against every decision's evidence sources and, on replay, against current governance |
| `opened_segment_windows`, `latest_opened_window_end` | declared windows of every opened segment |
| `latest_released_bar`, `latest_evaluated_session` | from the stored decisions and equity curve |
| `protected_rows_released_to_execution` | 0, valid only because every released bar re-derives as before the cutoff |

Run contract v4 (M5.1.3): `run_store.load_run` re-derives every field except
`holdout_authority` and rejects a run whose record differs or reaches the
cutoff. v2/v3 runs load as `PRIOR_CONTRACT_VERIFIED` and are never replayed or
compared.

The M5.1.1 fields `protected_rows_decoded_during_source_validation` and
`protected_source_byte_integrity_checked`, and the hard-coded
`protected_rows_scored_or_trained`, were self-reported claims and are removed.
Without an installed trust root there is no holdout boundary, so every request
is `REJECTED` (`HOLDOUT_BOUNDARY_UNVERIFIED`) before any segment is considered. The real Shoonya layout uses calendar-year segments, so the 2025
segment straddles the 2025-10-21 cutoff and is rejected until it is republished
as separate immutable pre-cutoff and protected segments (see
`docs/m513-partition-migration-spec.md`).

Pre-M5.1 runs recorded `locked_holdout_accessed: false`. That flag meant only
"not released, scored or trained". It never meant that protected rows were not
decoded. New runs no longer write it.

## Canonical bars

`CanonicalDailyBar` keeps the provider's exact decimal strings, integer volume,
the provider timestamp (`ssboe` UTC), the Asia/Kolkata session date and the
source segment file. Missing sessions are never forward-filled; no volume or
price is synthesized. Raw files are opened read-only, and the M2.4 lock is only
probed with an advisory `flock`, as M3 does. Only daily bars are supported. The
model has a session-timezone field so minute bars can be added later without
reinterpreting daily data.

## Strategy execution

`run_historical_backtest(strategy_version, release, settings, output_root)`, where
`release` is an adapter-issued `UniverseAssessment` (preferred) or a list of
`VerifiedHistoricalDataset`:

1. Re-validates the stored configuration and requires its hash to equal the
   immutable version's hash.
2. Refuses mixed modes. A strict run needs every decision to be adapter-issued
   and `ELIGIBLE` over one assessed range and one evidence set. Caller-supplied
   datasets run only as `DIAGNOSTIC_ONLY`, labelled
   `eligibility_issuance: CALLER_SUPPLIED_UNVERIFIED`. The evaluation range must
   lie inside every assessed range. Symbols must be inside the strategy's
   universe when the strategy lists one.
3. Evaluates each instrument with the shared V1 evaluator over all released bars
   (bars before `evaluation_start` serve only as indicator warm-up). When validated
   share events exist on a verified raw basis, signals are re-evaluated per
   as-of regime. Each decision date sees only events whose ex-date has passed and
   whose `available_at` is no later than that session's close (15:30 IST), so a
   later split never rescales an earlier decision.
4. Simulates on the union of released session dates with the generic simulator.
5. Computes metrics and the same-universe equal-weight buy-and-hold benchmark.
6. Publishes `run.json`, `signals.json`, `orders.json`, `trades.json`,
   `equity-curve.json`, `report.md`, `environment.json` (non-analytical) and
   `run-manifest.json` into
   `<output_root>/<run_id>/`. Files are staged and then renamed in one step. An
   existing run directory is never overwritten, and outputs under a raw root are
   refused.

M5.1.1 publishes `stock-gplm.m5-historical-backtest-run.v3`. `run_id` is the canonical hash of the strategy ID, version and configuration
hash, the dataset fingerprints (which bind the evidence digest), the
requested-universe hash, the eligibility issuance class, the settings, the
simulator version, portable content identity and the complete input-provenance
hash. Input provenance explicitly lists each payload, `.done` marker and
metadata hash, normalized-bars hash, evidence identity, strategy config, cost
profile/assumption and the not-applicable model-artifact status. The content identity hashes
the M1 validation, M2 importers, M3.9-B, V1 and M5 sources by relative path,
plus the adapter version and Python major.minor. Git commit, dirty flag, the full
Python build, platform, required `pyproject.toml` plus both lockfile hashes and loaded third-party versions are
recorded in `environment.json` and kept out of `run_id`. Identical inputs and
code reproduce identical analytical files on any checkout. `created_at_utc` and
`environment.json` are non-analytical.
Missing required reproducibility manifests fail before publication. Existing
immutable v2 runs remain readable at integrity level; replay emits v3 and is
required for comparison/ranking.

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
  reported separately; they are never converted into trades. Their estimated exit
  costs enter `total_return` (see Terminal valuation below).

### Metrics

Signals, trades, wins/losses, win rate, gross and net P&L, unrealized P&L, total
return (terminal liquidation estimate), marked-to-market total return, terminal
exit-cost estimate, liquidation equity estimate, annualized return (only with at least 252
sessions), maximum drawdown, average and median trade return, profit factor
(undefined without losses), average holding sessions, average gross exposure,
share of sessions with a position, turnover and ending equity. Sharpe and Sortino
are reported only with at least 252 sessions and 30 closed trades (risk-free rate
0, √252 annualization). Every undefined metric is `null` with a reason.

The benchmark buys every instrument with a bar on the first evaluated session in
equal weights at that open (slippage and fees applied) and values it at its last
in-range close under the same terminal policy. It is a same-universe reference,
not a market index.

### Terminal valuation

`LIQUIDATION_ESTIMATE_AT_FINAL_OBSERVED_CLOSE_V1` applies to the strategy and the
benchmark alike. `total_return` values every unit still held at the final
observed close × (1 − slippage), less sell commission and statutory charges, the
same convention as a realized sell. `total_return_marked_to_market` values those
units at the close with no exit costs. Entry costs are paid once, and closed
trades already paid their exit costs, so only open positions receive the
estimate. Nothing is counted twice. The equity curve, drawdown and risk ratios
remain marked to market. Reconciliation: `liquidation_equity_estimate − initial =
net_pnl_realized + unrealized_pnl − terminal_exit_cost_estimate`.

### Comparison and ranking

`compare_runs` reads every run through `run_store.load_run`. A run that fails
verification raises `RunIntegrityError`; it is never listed next to valid
results. A ranking needs every run to be `STRICT_RESEARCH`, investable,
`REPLAY_VERIFIED`, and comparable in eligibility mode, dataset fingerprint,
evaluation range, cost model, portfolio settings and terminal valuation.
Otherwise the result has `ranking_by_total_return: null` and the reasons.

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
version, lists runs with their verification status, compares them (with replay)
and exports the verified Markdown report. The symbol list is the requested
universe. Duplicate or non-canonical entries get HTTP 400 with per-entry reasons.
If any member is missing, blocked, rejected or ambiguous, nothing runs (HTTP 422
with every member's decision), unless a `DIAGNOSTIC_ONLY` request explicitly sets
`diagnostic_subset: true`. Endpoints:
`GET /api/m5/datasets`, `POST /api/m5/eligibility`, `POST /api/m5/backtests`,
`GET /api/m5/runs`, `GET /api/m5/runs/<run_id>`, `POST /api/m5/compare`. Request
bodies must contain exactly the documented fields, so a client cannot send a
status, eligibility flag or skip switch (`diagnostic_subset` is the only optional
field). Requests are capped at 25 symbols and one
running backtest at a time (HTTP 429 otherwise). Without `--m5-evidence` the
endpoints return 503. Editing a strategy saves a new version; earlier runs keep
their exact version and hash.

## Command line

```bash
# read-only coverage inventory; --verify validates pre-cutoff segments only and needs the governed M3.2 run
.venv/bin/python -m stock_gplm.m5.cli discover --verify --m32-run <m32-run-dir> --output reports/m5/discovery.json

# one eligibility decision
.venv/bin/python -m stock_gplm.m5.cli assess --evidence config/m5/evidence-paths.example.json \
  --mode STRICT_RESEARCH --symbol RELIANCE-EQ --start 2022-10-31 --end 2025-10-20

# a backtest (exit status 2 unless the whole requested universe is usable;
# DIAGNOSTIC_ONLY may add --diagnostic-subset to analyse only the usable members)
.venv/bin/python -m stock_gplm.m5.cli backtest --evidence config/m5/evidence-paths.example.json \
  --mode DIAGNOSTIC_ONLY --start 2022-10-31 --end 2025-10-20 --symbols RELIANCE-EQ TCS-EQ \
  --settings config/m5/backtest-settings.example.json --registry reports/m5/registry.sqlite3 \
  --strategy-file config/v1/strategies/swing-breakout-v1.json --output-root reports/m5/runs

# verified comparison; a ranking additionally needs replay through --evidence and --registry
.venv/bin/python -m stock_gplm.m5.cli compare reports/m5/runs/<run_a> reports/m5/runs/<run_b> \
  --evidence config/m5/evidence-paths.example.json --registry reports/m5/registry.sqlite3

# write one verified report to a new file (exit status 3 on an integrity failure)
.venv/bin/python -m stock_gplm.m5.cli export reports/m5/runs/<run_a> --output reports/m5/export.json
```

`config/m5/evidence-paths.example.json` resolves paths relative to its own
directory. Add `calendar_registration`, `listing_history`, `symbol_mappings` and
`corporate_actions` entries (M2.1 payload/manifest pairs), `provider_price_basis`
and `adjustment_events` once genuine evidence has been downloaded manually,
reviewed and registered.

## Limitations

- Strict real-data eligibility currently cannot pass. No calendar, listing,
  alias, corporate-action, price-basis, authoritative ETF or verified cost
  evidence is registered.
- Alias stitching across renamed symbols is unsupported and blocks.
- Rights, mergers, demergers and other complex actions block; cash dividends are
  excluded (price return).
- The benchmark is a same-universe reference; no index data is available.
- Statutory costs are the M3.5 policy's labelled assumptions; market impact
  beyond slippage is not modelled.
- Daily bars only.
