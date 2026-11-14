# M5 eligibility audit — 2026-11-14

Genuine local data was examined read-only with the code at `658782b`. The raw
tree was not modified: all 31,928 pre-existing files under `data/` and `reports/`
match their pre-session SHA-256 baseline, and no file was added under `data/`.
No network, credential or broker access was used. The JSON evidence is in the
git-ignored `reports/m5/` directory of the primary checkout:

| File | Content |
| --- | --- |
| `discovery-verified-2026-11-14.json` | `discover --verify`: per-symbol segment inventory plus a `validate_daily` disposition for every segment |
| `eligibility-audit-2026-11-14.json` | strict and diagnostic decisions for every symbol, range 2022-10-31 to 2025-10-20 |
| `eligibility-RELIANCE-EQ-{STRICT_RESEARCH,DIAGNOSTIC_ONLY}-2026-11-14.json` | full RELIANCE-EQ decisions |
| `evidence-paths-2026-11-14.json` | evidence actually supplied (M3.2 run, ETF heuristic, Shoonya master) |

## Publication coverage (actual counts)

| Item | Count |
| --- | --- |
| Symbol directories under `data/raw/shoonya/NSE` | 2,701 (all series `EQ`) |
| ETF by the M3.7 naming heuristic / not ETF | 309 / 2,392 (heuristic, not exchange evidence) |
| Complete publication triplets (`.json.gz`, `.meta.json`, `.json.gz.done`) | 10,355 |
| Incomplete triplets, unexpected files, unexpected root entries | 0, 0, 0 |
| M2.4 publication lock held | no |
| `validate_daily` ACCEPT | 10,346 |
| `validate_daily` QUARANTINE_CONTENT (OHLC bounds inconsistent) | 9 |

Segments by window: `2022-10-31_2022-12-31` 1,596; `2023-01-01_2023-12-31` 1,791;
`2024-01-01_2024-12-31` 1,990; `2025-01-01_2025-12-31` 2,276;
`2026-01-01_2026-10-29` 2,701; `2026-10-01_2026-10-07` 1 (the RELIANCE-EQ
real-data acceptance window). The earliest window starts 2022-10-31 and the
latest ends 2026-10-29.

Count reconciliation: 10,355 local publications = 10,354 standard yearly windows +
1 non-standard RELIANCE acceptance window. This matches the earlier "10,354 valid
standard publications" Oracle figure. The "10,357 segments in the broader
collection" refers to the Oracle side and cannot be confirmed locally; 2 segments
may not have been synchronized, and this is recorded as unverified. 10,346 of the
10,355 local segments are content-accepted.

Quarantined segments: ANTGRAPHIC-EQ 2023 and 2025; GAYAHWS-EQ 2023, 2024 and
2025; GTLINFRA-EQ 2023; VIVIDHA-EQ 2023; MITTAL-EQ 2026; NAVKARURB-EQ 2026. None
is repaired. Any request overlapping one is rejected.

## Evidence available

| Component | Status | Source |
| --- | --- | --- |
| Publication integrity | available | segment `.done` markers (SHA-256, bytes, records) |
| Locked holdout | VERIFIED | M3.2 `split-plan.json` `5c0b0a9d…` matches its run manifest; cutoff **2025-10-21** |
| Current identity | VERIFIED (current only) | Shoonya master `129b0eb8…` (M2.2 ACCEPT); proves current tokens, not history |
| Instrument classification | HEURISTIC | M3.7 ETF naming heuristic `8da64cf6…` |
| Trading calendar | MISSING | no registered NSE calendar (M3.6 `sources: []`; M2.5-R registration never run on real evidence) |
| Point-in-time listing | MISSING | no M2.1 listing history |
| Alias history | MISSING | no M2.1 symbol-change mapping |
| Corporate actions | MISSING | no M2.1 catalog; no licence-approved M3.9-B events |
| Provider price basis | MISSING | no contract exists; observed basis is mixed (below) |

## Provider price-basis finding

The "raw" Shoonya history is not uniformly unadjusted.

- RELIANCE-EQ `2024-01-01_2024-12-31.json.gz` (fetched 2026-10-30) shows
  2024-01-01 open 1290.28 and close 1295.12, and is continuous through the
  October 2024 1:1 bonus (2024-10-25 close 1327.85, 2024-10-28 open 1337.00).
  Before the bonus RELIANCE traded around twice those levels, so the provider has
  back-adjusted this history as of fetch time. That is an inference from the
  price level and the missing ex-date gap; no provider statement documents it.
- M3.8 found 65 probable *unadjusted* splits/bonuses in 65 symbols.
- 165 of the 1,980 diagnostically usable symbols have at least one unexplained
  open gap of 20% or more before the holdout.

Consequence: price levels may embed adjustments known only after the decision
date, and the adjustment status differs across symbols. M3.9-B must not be
applied on top. The adapter therefore labels every real dataset
`PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED`, never applies share events to it,
and blocks strict research with `PROVIDER_PRICE_BASIS_UNVERIFIED`.

## Decisions for every symbol (2022-10-31 to 2025-10-20)

| Mode | ELIGIBLE | BLOCKED | DIAGNOSTIC_ALLOWED | REJECTED | Bars released |
| --- | ---: | ---: | ---: | ---: | ---: |
| STRICT_RESEARCH | **0** | 1,980 | — | 721 | 0 |
| DIAGNOSTIC_ONLY | — | — | 1,980 | 721 | 1,233,210 |

Failure codes (symbols affected, same in both modes):

| Code | Class | Symbols |
| --- | --- | ---: |
| `CALENDAR_EVIDENCE_MISSING` | EVIDENCE | 1,980 |
| `PIT_MEMBERSHIP_MISSING` | EVIDENCE | 1,980 |
| `ALIAS_EVIDENCE_MISSING` | EVIDENCE | 1,980 |
| `CORPORATE_ACTION_COVERAGE_MISSING` | EVIDENCE | 1,980 |
| `PROVIDER_PRICE_BASIS_UNVERIFIED` | EVIDENCE | 1,980 |
| `PUBLICATION_COVERAGE_GAP` | EVIDENCE | 880 (468 of them otherwise usable; publication starts after 2022-10-31) |
| `ETF_EXCLUDED` | INTEGRITY | 309 |
| `NO_OBSERVATIONS` | INTEGRITY | 408 (no accepted bar before the holdout, for example listed after 2025-10-20) |
| `SEGMENT_QUARANTINED` | INTEGRITY | 4 (ANTGRAPHIC, GAYAHWS, GTLINFRA, VIVIDHA) |

Every rejection is explained by ETF exclusion, absence of pre-holdout data, or a
quarantined segment. No rejection came from a hash, marker, metadata or
contradiction failure.

RELIANCE-EQ example: strict `BLOCKED` with 727 bars (2022-10-31 to 2025-10-20).
48 rows dated on or after the cutoff in `2025-01-01_2025-12-31.json.gz` were
withheld, and the 2026 segments were never opened. A request ending 2025-10-21 is
`REJECTED` with `HOLDOUT_OVERLAP`.

## What would unblock strict research

1. A registered NSE calendar covering at least 2022-10-31 to 2025-10-20 (a manual
   browser download, then `stock-gplm-m25r register-calendar`).
2. M2.1 listing history with `membership_verified_through` on or after the range
   end, for each symbol.
3. M2.1 symbol-change mappings with `COMPLETE` coverage of the range.
4. An M2.1 corporate-action catalog with `COMPLETE` coverage, plus validated,
   licence-approved M3.9-B events for every split/bonus in range.
5. A new, reviewed evidence contract establishing the provider price basis, or a
   genuinely unadjusted price source. Without it, M3.9-B cannot be applied
   safely to Shoonya history.

Each of these needs a user decision and manually obtained evidence. NSE terms
forbid automated collection.
