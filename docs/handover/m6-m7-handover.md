# M5.10 – M7.3 handover (PRD v1 roadmap, phases 0, A, B and C)

Date 2026-11-19 (Claude). Branch `feature/m6-prd-v1` (from `c691a16`). Local only: **not pushed, merged or
deployed**. Guide and acceptance: `docs/m6-m7-prd-v1-build.md`. Test ledger: `docs/m6-m7-test-budget.md`.

## Status against the roadmap

| Milestone | Code | CLI | Synthetic test | Owner evidence |
| --- | --- | --- | --- | --- |
| M5.10 model export | done | done | not executed (owner smoke) | pending |
| M6.0 cost model | done | done | 1 case, pass | contract-note check pending |
| M6.1 timeframes and indicator store | done | done | 2 cases, pass | pending |
| M6.2 primitives and coverage | done | done | 1 case, pass | `gplm verify` pending |
| M6.3 horizons | done | done | 1 case, pass | pending |
| M6.4 benchmark | done | done | 1 case, pass | real-data run ×2 pending |
| M6.5 hybrid and controls | done | done | 1 case, pass | research run pending |
| M6.6 paper | done | done | 1 case, pass | forward paper period pending |
| M7.0–M7.2 opportunity, alerts, risk | done | done | 1 case, pass | pending |
| M7.3 ops and dashboard | done | done | not executed | one paper session day pending |
| M8.0–M8.2 cloud data | **blocked** | — | — | provider decision |

Overall roadmap completion is about **75%**. That is 12 of 15 milestones built and synthetically tested.
The remaining 25% is the M8 cloud work plus owner validation.

## Tests

9 of 10 budgeted cases executed, all passing first time (`9 passed in 2.79s`). Statically edited and not
executed: `tests/test_m59a_prediction_contracts.py:297` (schema `"4"`). No earlier milestone's tests were
rerun.

## Owner actions

1. Run the full suite and the acceptance commands in `docs/m6-m7-prd-v1-build.md`.
2. Verify the cost schedule against a real contract note.
3. Earlier open decisions, still open: AI_GENERATED origin mapping; whether an uncalibrated probability
   may go LIVE; numeric approval thresholds and holdout dates (the Phase B gate).
4. Choose the market-data provider and licence (unblocks M8).
5. Choose notification channels. File outbox is on by default; webhook and email are configured by
   environment variable.

## Known limitations

See "Limits" in the build guide. In addition, weekly and intraday labels still assume a 15:30 close on
partial sessions, and `ops scan` handles long-only strategies (cash equity, PRD v1).
