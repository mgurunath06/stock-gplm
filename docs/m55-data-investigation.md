# M5.5 local data investigation (2026-11-15)

This document records what the local Shoonya/NSE data actually contains, the anomalies found and the
transformation rules adopted for the GPLM application. Every number below was measured on the local
files; nothing was downloaded. Raw files were opened read-only and never modified.

## Locations and coverage

| Source | Path (WSL) | Content |
| --- | --- | --- |
| Daily candles | `stock-gplm/data/raw/shoonya/NSE/<SYM>-EQ/<from>_<to>.json.gz` (+ `.meta.json`, `.json.gz.done`) | 2,701 symbol directories. Windows: 2022-10-31…2022-12-31 (1,596 symbols), 2023 (1,791), 2024 (1,990), 2025 (2,276), 2026-01-01…2026-10-29 (2,701). RELIANCE also has an overlapping `2026-10-01_2026-10-07` file whose rows agree with the main 2026 file. |
| Minute candles | `stock-gplm/data/raw/shoonya/NSE/minute/<SYM>-EQ/<session>.json.gz` | 2,690 symbols. RELIANCE: 213 sessions (2026-01-01 → 2026-11-13). TCS: 17. Every other symbol: 10 sessions (2026-10-30 → 2026-11-13). |
| Minute candles (M3.4 tree) | `stock-gplm/data/raw/shoonya-minute/NSE/<SYM>-EQ/` | RELIANCE (204 sessions) and TCS (8 sessions); every copy has the same SHA-256 as the main minute tree (de-duplicated, recorded as `DUPLICATE_IDENTICAL`). |

The latest completed session in the local data is **2026-11-13** (Friday). Official daily candles end on
**2026-10-29**; the ten sessions 2026-10-30 → 2026-11-13 exist only as minute data. 2026-11-10 is absent
from every source; it is recorded as a calendar gap and is **not** explained (no holiday calendar is used).

## Finding 1 — minute volume regime change on 2026-08-03 (investigated, not discarded)

The unchanged M3.4 validator quarantines **every** minute session from 2026-08-03 onward with
`NEGATIVE_INTERVAL_VOLUME_MEANING_UNKNOWN` and `SESSION_POLICY_NOT_ESTABLISHED_FOR_DATE`. That period
contains the whole rolling-evaluation window, so the cause was investigated instead of dropping the files.

Observations (RELIANCE, all 213 sessions; HDFCBANK 2026-11-13 shows the same pattern):

* `intv` (interval volume) equals `diff(v)` (cumulative volume) on every bar of every session, before and
  after 2026-08-03. Negative `intv` is therefore a *cumulative counter reset*, not a corrupt bar.
* Before 2026-08-03 the counter never resets. From 2026-08-03 it resets at the **15:20** bar on every session
  (61 of 61 RELIANCE sessions that overlap official daily candles) and never before 15:20.
* After the reset the counter counts only post-15:20 trades. On most days it is restored to the full-day
  total at 15:28 or 15:29 (a large positive lump); on six days (2026-08-19 → 2026-09-03) it is not restored.
* **Prices are unaffected.** Over the 61 overlapping sessions, the last regular bar's close equals the
  official daily close exactly (median and 95th-percentile difference 0.0000), the session high and low
  match exactly, and the open differs by a median 0.07%. Before 2026-08-03 the median close difference is
  0.095% (the official close is not the 15:29 bar's close in that regime).
* Day volume: the last cumulative value matches official daily volume (median 0.06%) when the counter was
  restored; on the six unrestored days the cumulative value just before the reset plus the post-reset count
  matches within 0.5%.

Interpretation (labelled `OBSERVED_NOT_AUTHORITATIVE`; no exchange document was supplied): from 2026-08-03 the
provider reports a separate closing phase (consistent with a closing-auction session) from 15:20, whose volume
is accounted separately and whose final price is the official close.

Adapter rule (`stock_gplm.app.minute_adapter`, version `gplm-minute-adapter.v2`):

1. Sessions are parsed by the unchanged M3.4 validator (strict fields, IST/ssboe agreement, OHLC bounds,
   duplicates, ordering).
2. A session whose negative interval volumes **and** counter resets all lie at or after 15:20 is accepted as
   `ACCEPT_CLOSING_PHASE_V2`. Every price is used. Interval volumes of 15:20–15:29 bars are **withheld**
   (NaN in the analytical frame; resampled buckets containing them get NaN volume, never a partial sum).
3. Negative volume before 15:20, a publication mismatch or any M3.4 `REJECT` keeps the session out.
4. Minute-derived day volume uses the reconciled rule above and is labelled `MINUTE_DERIVED`.

## Finding 2 — official daily candles end on 2026-10-29

For the ten later sessions the composite daily series uses minute-derived candles (open = first regular bar
open, high/low = session extremes, close = last regular bar close, volume = reconciled cumulative rule).
Each such row carries `source = MINUTE_DERIVED`; the decision bar's source is stored with every prediction.
Where both exist the two are reconciled and every difference above 0.5% is recorded as a data issue.

## Finding 3 — large moves and corporate actions

RELIANCE closes 1164.9 on 2026-10-30 and 1300.0 on 2026-11-02 (+11.6%, 88.0 M shares, the highest volume in
the window). HDFCBANK rises from 683 to 812 between 2026-10-29 and 2026-11-12. These may be news or corporate
actions; **no corporate-action evidence exists locally**, so nothing is adjusted. Open gaps above 10% are
recorded as `CORPORATE_ACTION_UNCERTAIN`; gaps above 20% (or 5% intraday bar moves) block trades and blank
features, as in M5.4.

## Other checks (recorded per file in `data_files` / `data_issues`)

| Check | Result for RELIANCE, HDFCBANK, ICICIBANK |
| --- | --- |
| Duplicate records | none inside files; 204 + 8 cross-tree minute duplicates, all byte-identical |
| File overlap | RELIANCE daily `2026-10-01_2026-10-07` overlaps the main 2026 file; rows agree (`PARTITION_OVERLAP`, INFO) |
| Missing candles | minute sessions have 375 regular bars (09:15–15:29) plus a 09:14 pre-open bar, which is dropped as off-session |
| Corrupt observations / OHLC inconsistencies | none (M3.4 and daily validators) |
| Negative volumes | only the closing-phase counter resets described above |
| Incomplete sessions | none for these symbols; any session with fewer than 375 regular bars is flagged `INCOMPLETE_SESSION` |
| Calendar gaps over four days | listed, not explained |

A checksum proves byte integrity only, not authenticity or market-data correctness. The full catalogue is
reproducible with `gplm ingest --stocks ...` (or `--universe all`); results land in the audit database.
