# M3.4 evidence report — 2026-11-01

Evidence comes from read-only access to Oracle (`sne-cloud`) and local files. No Shoonya
call, Oracle write, daily re-extraction or model training took place. Run:
`reports/m34/ad9689e375876177abc1adef742b1bba04711532d27a82fc07137e1189035b33`. Run `903f0b1d...`
is kept as prior evidence: it rejected the same 70 segments before the quarantine existed.

## Daily dataset

`CONFIRMED_PUBLISHED_LOCALLY`: all 31,065 files (10,355 publications) under
`data/raw/shoonya/NSE` match the M3.1 manifest, with none unbound and the publication lock free.
Oracle holds 10,357. The two absent locally are RELIANCE samples
`2022-10-31_2022-11-30` and `2023-01-01_2023-01-31`, which overlap collector windows.

## Minute inventory

WSL had no minute data before this work. Oracle has 636 files, or 212 triplets, in
`StockGPLMDownloads/data/minute/shoonya/NSE`. All were verified against source hashes and
published to `data/raw/shoonya-minute/NSE`.

| Symbol | Files present | Sessions | Regular-window bars | Fused sessions | Quarantined |
| --- | --- | --- | --- | --- | --- |
| RELIANCE-EQ | 2026-01-01 to 2026-10-30 | 204 | 375 each | 142 (to 2026-07-31) | 62 (from 2026-08-03) |
| TCS-EQ | 2026-10-21 to 2026-10-30 | 8 | 375 each | 0 | 8 |
| Other 2,713 current EQ symbols | none | 0 | | 0 | |

Totals: 79,664 bars and 53,250 fused rows. One-minute interval; timestamps agree on all bars.
No duplicates, bad OHLC or missing regular minutes were found.

## Gaps

- **TCS-EQ:** 197 sessions before 2026-10-21 have no file. The collector's daily job looks back
  only ten days; whether a backfill would succeed is unknown.
- **RELIANCE-EQ 2026-02-01 (Sunday):** a daily candle exists, but no minute file. The collector
  skips weekends unless a special session is configured.
- **2026-10-30:** minute files exist for both symbols, but no daily candle, because the daily
  window ends 2026-10-29.
- All reasons stay `UNKNOWN` in the reports unless shown above from collector code.

## Findings

**Regime change on 2026-08-03.** On all 70 sessions from that date, bars from 15:15 are flat,
15:16-15:19 have zero volume, and `v` drops sharply at 15:20 on 69 of them. 142 bars have negative
`intv`. Before that date, none of the 142 sessions show this. Public reports say a Closing
Auction Session began for eligible stocks on that date, with conflicting timings and a revision
reported on 2026-10-16. That is a lead rather than verified cause, so these sessions are quarantined.

**Bar 09:14.** 164 sessions have a flat, zero-`intv` bar at 09:14 with nonzero `v`. When
present and a daily candle exists, its open equalled the daily open on all 162 sessions.
It is flagged `OFF_SESSION` and never becomes a feature or the session open.

| Daily reconciliation | Before 2026-08-03 (142) | From 2026-08-03 (68) |
| --- | --- | --- |
| Close differs from last bar | 141 | 1 |
| Open differs from first bar | 48 | 0 |
| High or low differs | 4 | 0 |
| Volume gap over 1% | 23 (largest 3.3%, median 0.48%) | not comparable: 68 |

Volume flags: 511 zero-volume bars (164 at 09:14, 346 in the 15:00 hour, 1 at 10:00), 341 spikes,
and 142 negative bars. Nothing was repaired; causes are unknown.

## Leakage audit

`PASS`: 2,272 session and prefix recomputations, 142 daily truncation recomputations, and 0 of
53,250 rows with a daily date on or after the session. See the vintage limit in
[the M3.4 contract](m34-minute-ingestion-fusion.md).

## Gates

`minute_publication`, `minute_schema` and `leakage_audit` pass. Blocked: `minute_content`,
`minute_session_policy`, `minute_universe_coverage`, `minute_history_coverage`,
`daily_minute_reconciliation` and the five inherited daily gates. Intraday horizon: `BLOCKED`.
Training readiness: `NOT_READY`.

## Dependencies

1. An exchange or provider definition of sessions from 2026-08-03, including the 2026-10-16
   revision, and the meaning of negative `intv` and the `v` reset. Without it, no TCS-EQ session
   and no RELIANCE-EQ session after July can be fused.
2. A decision on the minute universe and a backfill; collection is outside this milestone.
3. An authoritative calendar, including special sessions, and corporate-action, listing and
   alias evidence, as M3.2 already requires.
4. Provider confirmation of bar-label semantics and revision behaviour.
