# M3.2 point-in-time research framework

M3.2 turns an M3.1 readiness run into deterministic features, forward-outcome
labels, walk-forward plans and quality gates. It trains nothing, produces no
predictions, signals or confidence values, downloads nothing and never reads or
writes raw files. Every test uses synthetic fixtures, so the tests prove the
contracts, not anything about the market.

```bash
.venv/bin/python -m stock_gplm.m3.research_cli \
  --readiness-run reports/m3/<m31-run-id> \
  --output-root reports/m32
```

Optional evidence comes in checksum-bound M2.1 form: `--calendar` with
`--calendar-manifest`, `--corporate-actions` with `--corporate-actions-manifest`,
and `--inventory` for a merged inventory with per-field provenance.

The run refuses an M3.1 run whose outputs do not match its manifest, a run that
lacks the current M3.1 outputs, an output root inside the raw tree, and an existing
run ID. The ID binds the M3.1 outputs, the session axis, the evidence hashes and
the contracts. It contains no timestamps, so a rerun reproduces identical bytes.

| Output | Content |
| --- | --- |
| `features.csv` | One row per observed candle, with its source path and hash |
| `labels.csv` | One row per candle and daily horizon, with status and availability |
| `split-plan.json` | Walk-forward plan and locked holdout per horizon |
| `contracts.json` | Feature and label definitions |
| `quality-gates.json` | Gates, per-horizon counts, blockers and decision |
| `run-manifest.json` | Run binding and output hashes |

## Features

`FEATURE_SPECS` in `stock_gplm/m3/features.py` is the registry: 50 columns, each
with a definition and an exact warm-up. A value at session `t` reads only candles
at or before `t`, and it is usable after that session closes. Prices are unadjusted.

| Family | Columns | Warm-up observations |
| --- | --- | --- |
| SMA | 5, 10, 20, 50, 100, 200 | the period |
| EMA, seeded by SMA | 9, 20, 50, 100, 200 | the period |
| RSI (Wilder) | 14 | 15 |
| MACD | line / signal, histogram (12, 26, 9) | 26 / 34 |
| ATR (Wilder) | `atr_14`, `atr_14_pct` | 15 |
| Bollinger | 20 sessions, 2 population deviations; mid, upper, lower, %B, bandwidth | 20 |
| ADX (Wilder) | `plus_di_14`, `minus_di_14` / `adx_14` | 15 / 28 |
| Volume ratio | volume over the mean of the prior 5, 20, 60 sessions | period + 1 |
| Volatility | sample deviation of 20, 60, 120 simple returns, not annualized | period + 1 |
| Momentum | 1, 5, 21, 63, 126, 252 sessions | period + 1 |
| Price structure | close location, range, body, upper and lower wick | 1 |
| Price structure | opening gap | 2 |
| Price structure | distance from 20 and 252-session high and low | the period |
| Price structure | distance from SMA 20, 50, 200 | the period |

Until warm-up completes a value is blank. An undefined ratio, such as close location
when high equals low, or a volume ratio over zero base volume, is also blank and
never zero. Two conventions are explicit: DI is 0 when ATR is 0, and DX is 0 when
both DIs are 0, because no range means no directional movement.

## Labels

Every horizon is a versioned `LabelContract`. "d" means NSE sessions, not calendar
days, and one month is taken as 21 sessions. The buckets do not overlap, so adjacent
ranges start one session after the previous bucket ends.

| Horizon | Sessions after as-of |
| --- | --- |
| `1-7d` | 1 to 7 |
| `7-15d` | 8 to 15 |
| `15-30d` | 16 to 30 |
| `1-3m` | 31 to 63 |
| `3-6m` | 64 to 126 |
| `6-12m` | 127 to 252 |

For as-of session `t`, the decision moment is after the close of `t`. Entry is the
open of `t+1`. Each value is measured from that entry price:

- `gross_return_at_min_offset` and `gross_return_at_max_offset` use the close at the
  bucket's first and last session;
- `max_favorable_excursion` and `max_adverse_excursion` use the highest high and
  lowest low from `t+1` through the last session.

`label_available_date` is the last session of the bucket. Returns are gross. Costs,
slippage, liquidity and whether the entry was executable are future evaluation
concerns and are not applied here.

Offsets count on the session axis, so a missing candle cannot shift an exit date.
Only `COMPLETE` rows carry values; others stay visible with blanks:

| Status | Meaning |
| --- | --- |
| `CENSORED` | The exit session is beyond the axis; the outcome is not yet known |
| `MISSING_CANDLE_IN_HORIZON` | An axis session from entry to exit has no candle |
| `CORPORATE_ACTION_IN_HORIZON` | An action takes effect after entry and on or before exit |
| `DELISTED_IN_HORIZON` | Supplied delisting falls in the horizon; retained for survivorship |

An action on the entry date is allowed because that session's open already reflects it.

### Intraday

M3.4 now ingests real minute bars through its own adapter, but this label path stays closed
until its gates pass; see [M3.4](m34-minute-ingestion-fusion.md). Intraday requires real minute bars. `build_daily_labels` refuses the intraday
contract, and a series with too few bars per session is rejected, so daily candles
cannot pose as minute data. No minute schema is confirmed, so
`CONFIRMED_MINUTE_SCHEMAS` is empty and production calls refuse every series. Tests
use a schema-labeled fixture path whose output is marked `SYNTHETIC_TEST_FIXTURE`.

The contract enters on the next minute's open and exits within the same session at
15, 30 and 60 minutes and at the last bar. Session bounds must be supplied with
provenance. Any missing minute on a path blanks that value without interpolation.

Enabling this requires a sanitized provider sample and a versioned adapter that adds
its schema to the confirmed set.

## Missing data and reference evidence

M3.2 consumes the M2 models and infers nothing they do not state.

| Topic | Handling | Without evidence |
| --- | --- | --- |
| Missing candles | Series restart and warm-up reapply; no row is created and no window bridges a gap | Same |
| Calendar | `SessionAxis.from_calendar` needs a complete, explicit `NSETradingCalendar` | The M3.1 observed union is used and the calendar gate stays blocked |
| Listing and delisting | `Instrument.membership_on`; delisting produces a visible label status | Membership stays `UNKNOWN`; the current master proves nothing historical |
| Aliases | `Instrument.symbol_on`; a series under the wrong name on a date is `ALIAS_MISMATCH`; series are not stitched | Alias gate stays blocked; an empty list does not prove no aliases existed |
| Corporate actions | Each action not marked `NOT_APPLICABLE` restarts the series and blocks labels that span it | Coverage stays `UNKNOWN`; no adjustment is computed or assumed |

The observed union cannot reveal a session where no symbol has a candle, and a stray
candle can add a date. That is why it never satisfies the calendar gate. A candle on a
date absent from a supplied calendar stops the run instead of being absorbed.

Adjusted prices are out of scope until a versioned adjustment policy exists. Until
then, restarting after an action costs warm-up history but cannot create a false return.

## Chronological evaluation

`build_walk_forward` lays out train, validation and test partitions by as-of date,
with no shuffling. Purge equals the horizon's last offset, so it differs per horizon.

Each partition boundary keeps `purge + embargo + 1` sessions between the last as-of
of one partition and the first as-of of the next. A label therefore resolves more
than `embargo` sessions before the next partition starts. Test partitions never
overlap, and the last sessions form a locked holdout outside every fold. Too little
history produces no folds rather than shortened ones.

`verify_plan` rechecks every boundary independently, and `select_samples` raises
`LeakageError` if a real label resolves across a boundary.

| Default policy | Value |
| --- | --- |
| Minimum train | max(252, 3 x last offset) sessions, expanding |
| Validation, test and step | max(63, last offset) sessions |
| Embargo | 5 sessions |
| Locked holdout | max(126, last offset) sessions |
| Trial budget | 20 |

These defaults are proposals and need approval before the first trial. Later
evaluation must use the holdout once and report out-of-sample, benchmark-relative
results with realistic costs, calibrated outcome probabilities and experiment
provenance on a point-in-time universe.

## Quality gates

| Gate | Passes when |
| --- | --- |
| `raw_candle_acceptance` | M3.1 reports `ACCEPT` |
| `authoritative_calendar` | The axis comes from a complete supplied calendar |
| `point_in_time_membership` | Every observed candle is `LISTED` by supplied history |
| `alias_history` | Every instrument has `COMPLETE` alias coverage |
| `corporate_action_coverage` | Complete coverage spans each symbol's history |
| `minute_data` | Never yet; no confirmed schema exists |

A daily horizon also needs at least one walk-forward fold and complete labels. Its
decision is `BLOCKED` or `ELIGIBLE_FOR_DESIGN_REVIEW`; the second only means the
evidence exists to review a training design. `training_readiness` is always
`NOT_READY` and `model_training` is always `NOT_PERFORMED` in this milestone.

## Current blockers

- No authoritative NSE calendar, listing history, alias history or corporate-action
  catalog has been supplied.
- No real minute-level data or confirmed minute schema exists.
- Horizon names, the 21-session month and the default evaluation policy await review.
