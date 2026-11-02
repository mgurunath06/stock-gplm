# M3.6 external reference evidence

M3.6 turns official NSE reference files into the M2.1 evidence that the M3.2 gates and
M3.5 training gates need. It does not download anything. The results of the first real
run are in the [evidence report](m36-evidence-report-2026-11-02.md).

## Why acquisition is manual

On 2026-11-02 the NSE [Terms of Use](https://www.nseindia.com/static/nse-terms-of-use)
said: "User is prohibited to conduct any systematic or automated data collection
activities (including scraping, data mining, data extraction and data harvesting)".
The [Copyright policy](https://www.nseindia.com/static/nse-copyright) lets users "view,
print copies and download the Content for personal, non-commercial or educational
purpose", with acknowledgement of the source. Following those terms:

- M3.6 has no HTTP client and calls no NSE API.
- It does not prime cookies or get around bot protection.
- A person downloads each file in a browser.
- `register` records where and when the file was obtained and which dates it covers.
- Every stored record cites the terms.

## Workflow

`pip install -e .` installs this command as `stock-gplm-evidence`. The module form below
works without reinstalling.

```bash
# 1. Download a file in a browser, then register it (repeat per file).
.venv/bin/python -m stock_gplm.m36.cli register ~/Downloads/EQUITY_L.csv \
  --component nse-equity-list --scope ALL_LISTED_EQUITIES \
  --source-url 'https://…/EQUITY_L.csv' --downloaded-at 2026-11-03T10:15:00+05:30 \
  --applicable-start 2026-11-03 --applicable-end 2026-11-03

# 2. Build a bundle and gap report for an M3.1 run.
.venv/bin/python -m stock_gplm.m36.cli build --readiness-run reports/m3/<m31-id> --output-root reports/m36

# 3. Rebuild M3.2 only if a component validated, then recheck gates without spending trials.
.venv/bin/stock-gplm-research --readiness-run reports/m3/<m31-id> --output-root reports/m32 \
  --reference-bundle reports/m36/<bundle-id>
.venv/bin/stock-gplm-train --readiness-run reports/m3/<m31-id> --research-run reports/m32/<m32-id> \
  --policy config/m35/daily-training-policy.json --gates-only
```

The `list` subcommand re-verifies every stored file and prints any integrity error.

## Evidence store

The store is at `data/reference/nse/<component>/<sha256>/` and must be outside
`data/raw`. Each record holds the original bytes and an `acquisition.json` file. Both
are made read-only.

The acquisition record contains:

- the SHA-256 and the size;
- the source URL, which must be `https` on an `nseindia.com` or `nseindia.in` host;
- the timezone-aware download time;
- the applicable dates and the scope;
- the time of registration and a reference to the access terms.

Registering identical bytes and metadata again returns the existing record. Any other
difference is refused. Changed bytes are reported as integrity errors and are never
skipped silently.

| Component | Scope | Adapter |
| --- | --- | --- |
| `nse-weekly-schedule` | `WEEKLY_SCHEDULE` | Document only; must declare `NO_SESSION_ON_SATURDAY_OR_SUNDAY_EXCEPT_ANNOUNCED_SPECIAL_SESSIONS` |
| `nse-calendar-source` | `TRADING_HOLIDAYS`, `SPECIAL_SESSIONS` | Document only, such as a circular PDF |
| `nse-calendar-transcription` | Same as its sources | `date,status,label,source_sha256`; every row cites a registered source |
| `nse-corporate-actions` | `ALL_EQUITY_SYMBOLS` (`SELECTED_SYMBOLS` is stored but never used) | Corporate-filings CSV export |
| `nse-equity-list` | `ALL_LISTED_EQUITIES` | `EQUITY_L.csv` |
| `nse-etf-list` | `ALL_LISTED_ETFS` | ETF list CSV |
| `nse-symbol-changes` | `ALL_SYMBOL_CHANGES` | Symbol-change CSV |
| `nse-delisted-securities` | `ALL_DELISTED_SECURITIES` | None: stored, never parsed |

## Adapters

The V1 adapters require exact headers, listed in `stock_gplm/m36/adapters.py`. These
layouts follow NSE's published file formats but are unconfirmed: no real NSE file has
passed through them yet. If a file differs, it is refused with its observed header so a
versioned adapter can be added. Whitespace around header cells is the only thing
normalised.

A single bad row makes the whole file unusable. The rejected rows are written to
`rejected-rows.jsonl`.

### Corporate actions

Ratios come only from numbers in the NSE `PURPOSE` text:

| Purpose | Action | Status | Price factor |
| --- | --- | --- | --- |
| `Bonus a:b` | `BONUS` | `UNADJUSTED` | b/(a+b) |
| `Face Value Split … From Rs x To Rs y` | `SPLIT` | `UNADJUSTED` | y/x |
| `Consolidation … From Rs x To Rs y` | `CONSOLIDATION` | `UNADJUSTED` | y/x |
| Bonus, split or consolidation without stated numbers | Same | `UNKNOWN` | None |
| Rights, demerger, scheme, capital reduction, buyback | Named type | `POLICY_REQUIRED` | None; needs the cum price and an approved policy |
| Dividend, AGM or EGM | `DIVIDEND`, `MEETING` | `NOT_APPLICABLE` | None; labels are price returns |
| Anything else | `UNRECOGNIZED` | `UNKNOWN` | None |

Prices are never adjusted. M3.2 restarts each symbol's series at every action that is
not `NOT_APPLICABLE`, so `UNKNOWN` and `POLICY_REQUIRED` actions stay conservative breaks.
An action filed under an older symbol is moved to today's symbol through the validated
symbol-change list, and the original is recorded as `reported_symbol`.

## Bundle components

Each component is either `VALIDATED` or `BLOCKED` with counted gaps. Only validated
components are written. Before the bundle is published, they are re-imported with the
M2.1 importers.

| Component | Validates when | Output |
| --- | --- | --- |
| `calendar` | A weekly-schedule document covers the span, holiday and special-session transcriptions cover every date of it, and the result matches the M3.1 candles | `nse-calendar.csv` and its M2.1 manifest |
| `corporate_actions` | `ALL_EQUITY_SYMBOLS` exports, with no rejected rows, cover the M3.1 span contiguously | `corporate-actions.json` and its manifest; each M3.1 symbol gets a `COMPLETE` catalog |
| `instrument_classification` | Both the equity list and the ETF list validate | `instrument-classes.csv`: `EQUITY`, `ETF` or `UNKNOWN` (in neither list or in both) |
| `alias_history` | The symbol-change list validates and no M3.1 symbol was renamed between its first and last candle | Aliases with `alias_history_status: COMPLETE` in `inventory.json` |
| `point_in_time_membership` | Never in V1 | Listing dates and ISINs in `inventory.json`; `membership_verified_through` stays unset |

### Calendar

Sessions are never inferred from candles. Each date in the span is set as follows:

1. A transcribed special session becomes `SPECIAL_SESSION`.
2. A transcribed holiday becomes `MARKET_CLOSED`.
3. A Saturday or Sunday becomes `MARKET_CLOSED`, citing the weekly-schedule document.
4. Any other weekday becomes `TRADING_SESSION`.

The weekend rule is evidence: NSE's published schedule, registered with its hash and
cited on each row.

A special-session transcription cannot cover dates after it was made, because those
sessions are announced ad hoc.

The calendar is then checked against the candles. Both of the following block it, with
every date listed:

- candles on a closed date;
- a calendar session with no candle for any symbol.

### Membership

A current list proves that a security is listed today and when it was first listed. It
does not prove that it stayed listed in between. It also says nothing about securities
delisted during the span, and those are absent from M3.1 altogether. Membership therefore
stays `UNKNOWN` until a delisting and suspension history validates. No V1 adapter accepts
one yet: register the file and share a sanitised sample so one can be added.

## Bundle files

All files are bound by `bundle-manifest.json`. The bundle ID hashes the M3.1 outputs used,
the store's acquisition records (without registration times) and the adapter version. A
bundle is never overwritten.

| File | Content |
| --- | --- |
| `coverage-report.json` | Status, gaps with counts and date ranges, reports and sources for each component |
| `evidence-gaps.md` | Readable gap list with the official pages to download from |
| `corporate-actions-unresolved.csv` | `UNKNOWN` and `POLICY_REQUIRED` actions |
| `corporate-actions-outside-universe.csv` | Actions for symbols absent from M3.1 |
| `large-moves-unexplained.csv` | M3.1 `LARGE_CLOSE_MOVE` flags with no price-affecting action on that session |
| `rejected-rows.jsonl` | Every rejected source row |

## Downstream effects

- **M3.2:** `--reference-bundle` loads only validated components and binds the bundle ID.
  M3.2 adds an `instrument_classification` gate. With a calendar it writes
  `session-axis.csv`; with classes it writes `instrument-classes.csv`.
- **M3.5:** the run uses the bound axis and refuses an authoritative M3.2 run that lacks
  one. For other M3.5 changes, see [M3.5](m35-daily-training.md).
