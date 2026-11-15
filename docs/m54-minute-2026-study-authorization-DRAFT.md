# DRAFT — independent 2026 minute-research study authorization (NOT AUTHORIZED)

Status: **DRAFT_NOT_AUTHORIZED**. No code reads 2026 payloads. This draft prepares a decision for the user.

## Why it is needed

Every local minute file (2026-01-01 → 2026-11-13, 2,690 symbols, 27,110 files) falls inside the M3.2
locked-holdout range, which starts on 2025-10-21. Today the unified engine excludes these files whole and
never opens them. The same holds for the 2025 daily file. Codex hit the same restriction. Using these files
consumes holdout dates. That is a governance decision, not an engineering one.

## Preconditions (in this order)

1. **Score or retire M3.2 first.** Score the M3.2 models on their locked holdout exactly once, as AGENTS.md
   requires, before anyone inspects a 2026 research result. Alternatively, record a signed retirement of
   the M3.2 holdout. Otherwise the 2026 study contaminates M3.2's only clean evaluation.
2. **Add an AGENTS.md scope rule** naming this study: what it may read, that it is research-only, and that
   it uses its own trial budget.
3. **Write a hash manifest** of every 2026 minute payload used, before any payload is decoded.

## Exact proposed scope

| Item | Proposal |
| --- | --- |
| Study id | `m54-minute-2026-v1` |
| Data | `data/raw/shoonya/NSE/minute/<SYM>/2026-*.json.gz` and `data/raw/shoonya-minute/NSE/...` (duplicates reconciled by hash); 2025-10-21 → 2025-12-31 daily rows only if the 2025 file is also authorized |
| Symbols | An explicit list (start with RELIANCE, HDFCBANK, ICICIBANK, INFY, SBIN), or `all-eligible` labelled survivorship-biased |
| Session policy | Bars on or after 2026-08-03 carry negative `intv` and are QUARANTINEd by the M3.4 adapter. A new authoritative session/volume definition is needed before those sessions can be used |
| Partitions | train 2026-01-01 → 2026-06-30; embargo 5 sessions; validation 2026-07-08 → 2026-08-31; embargo 5; final test 2026-09-08 → 2026-11-13, opened once after the configuration is frozen |
| Trial budget | Proposed: 20 per horizon, recorded in a shared ledger |
| Outputs | Research diagnostics only; never compared against or substituted for M3.2 |

## Approval mechanism (to implement only after approval)

1. The user signs an approval file, `config/m54/authorizations/m54-minute-2026-v1.json`, containing the
   study id, the scope table above, the payload-manifest SHA-256 and the approval date. Its SHA-256 is
   recorded in the docs.
2. `candles.resolve_policy` gains a third basis, `AUTHORIZED_STUDY`. It accepts only partitions listed in
   the signed manifest whose hashes match, and only for the named study's config. Every run records the
   approval hash, and an M3.2 run without a recorded holdout score or retirement refuses to start.
3. The default config keeps `M32_LOCKED_HOLDOUT`. Without the signed file, nothing changes.
