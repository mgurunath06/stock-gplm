# M3.6 evidence report — 2026-11-02

No external reference dataset was acquired, so no reference component validated. M3.2
was not rebuilt, and the daily gates still block every horizon. LightGBM was not
trained, no trial was spent, and the locked holdout was not scored.

## What was checked

- **NSE access terms**, read on 2026-11-02:
  - The Terms of Use prohibit systematic or automated data collection.
  - The Copyright policy allows personal, non-commercial or educational download.
  - The Disclaimer limits free research-data access to individuals from accredited
    academic or research bodies, and NSE may require an NDA.

  Only `robots.txt`, those three policy pages and the two pages named in the request were
  fetched, to read their terms. Nothing from them was kept as data.
- **Local reference files:** the project, `~/`, and the Windows Downloads, Documents and
  Desktop folders were searched. The only reference file found was the Shoonya master
  `data/reference/source/shoonya/NSE_symbols.txt.zip`. It holds no history, so it cannot
  close any gap.
- **Evidence store:** `data/reference/nse` is empty.

Because automated download is not allowed, every source must be downloaded in a browser
and registered.

## Run

| Item | Value |
| --- | --- |
| Bundle | `reports/m36/f2d17cff60e6199099addccd9204a52bea6fceb89c53e1af6fa675450085d56c` |
| M3.1 input | `045e4ba0…`: 2,701 symbols with candles, 979 observed sessions, 2022-10-31 to 2026-10-29 (1,460 calendar dates) |
| Gate recheck | `reports/m36/gate-check-26e41d00-2026-11-02.json`, from `stock-gplm-train --gates-only` on M3.2 `26e41d00…` |
| Trial ledger | Unchanged: 18 entries, 6 of 20 per horizon |

## Remaining evidence gaps

| Component | Gap | Count | Source to download manually |
| --- | --- | --- | --- |
| Calendar | No weekly-schedule document | 1,460 dates | [Holidays and timings page](https://www.nseindia.com/resources/exchange-communication-holidays) |
| Calendar | No trading-holiday transcription | 1,460 dates | Yearly NSE holiday circulars for 2022–2026 |
| Calendar | No special-session transcription | 1,460 dates | NSE circulars for every special or Muhurat session |
| Corporate actions | No full-market export | 2,701 symbols, 1,460 dates | [Corporate filings: actions](https://www.nseindia.com/companies-listing/corporate-filings-actions), Equity, all symbols, CSV from 2022-10-31 to 2026-10-29 or later |
| Classification | No equity list | 2,701 symbols | `EQUITY_L.csv` from [securities available for trading](https://betanseapi.nseindia.com/static/market-data/securities-available-for-trading) |
| Classification | No ETF list | 2,701 symbols | ETF list from the same page |
| Aliases | No symbol-change list | 2,701 symbols | Same page or [all reports](https://www.nseindia.in/all-reports) |
| Membership | No listing dates | 2,701 symbols | `EQUITY_L.csv` |
| Membership | No delisting history; also no V1 adapter | 2,701 symbols | An NSE delisted and suspended securities list; share a sanitised sample |

These gaps produce the following gate results:

| Gate | Status |
| --- | --- |
| `authoritative_calendar` | Blocked |
| `point_in_time_membership` | Blocked |
| `alias_history` | Blocked |
| `corporate_action_coverage` | Blocked |
| `instrument_classification` | Blocked: M3.2 `26e41d00…` predates this gate |
| `raw_candle_acceptance` | `PASS_SCOPED_TO_ELIGIBLE_UNIVERSE` |

The static universe is unchanged at 2,200 eligible symbols. ETF separation is not
established, so ETFs remain in exploratory runs only.

## Even with every downloadable file

Point-in-time membership still blocks training until a delisting history validates. The
M3.1 universe also comes from today's Shoonya master. Securities delisted during the span
have no candles, so the survivorship bias cannot be measured until that history is
supplied and their missing data is counted.

The V1 adapter layouts are unconfirmed. If a real file's header differs, the bundle
reports `LAYOUT_NOT_SUPPORTED` with the observed header, and that component stays blocked
until a versioned adapter is added.
