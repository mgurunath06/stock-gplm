# M5.1 integrity hardening and audit remediation — 2026-11-14

An independent adversarial audit of M4/M5 at `a9d0c57` returned **NO-GO for M5
integration**. Its reproductions showed that strict eligibility, universe
completeness, corporate-action consistency and run rankings could all be
bypassed, even though all 668 tests passed. This document records the fixes made
on `feature/m51-integrity-hardening` (branched from `a9d0c57`). Each fix comes
with the threat model it addresses and the claim it does and does not support.

Nothing here changes data-readiness. **Strict real-data backtesting remains
BLOCKED** until independent evidence is registered and verified: the NSE
calendar, point-in-time listings, aliases, complete corporate actions, the
provider price basis, authoritative ETF classification and verified
transaction-cost schedules. None of these were created, inferred or relaxed.

## A — Trusted issuance of eligibility

**Defect.** `DataEligibilityDecision` was trusted when its `decision_id` matched
its content. Anyone could compute that hash, so fabricated bars plus a
fabricated `ELIGIBLE` decision produced an `investable: true` strict run.
Evidence objects could also be edited with `dataclasses.replace`. That is how
the old fixtures set `provider_price_basis`.

**Contract.** `stock_gplm.m5.issuance` holds one 256-bit key drawn from
`secrets` at import. The key lives only in a closure. Two kinds of object carry
an HMAC-SHA256 seal over their content hash:

| Sealed object | Issued by | Content bound |
| --- | --- | --- |
| `ReferenceEvidence` | `load_reference_evidence` only | every field, canonicalized (holdout plan, calendar, listing, aliases, catalog, events, master tokens, price-basis registration, source hashes) |
| `DataEligibilityDecision` | `HistoricalDataAdapter.assess` only | request (symbol, range, mode), failures, segment file hashes, bars hash, holdout cutoff, evidence digest and source hashes, price basis, share events, corporate-action reconciliation |
| `UniverseAssessment` | `HistoricalDataAdapter.assess_universe` only | canonical requested universe, every member decision ID, status, released and excluded members |

`decision.investable` now requires `ELIGIBLE`, `STRICT_RESEARCH` **and** a valid
seal. The runner refuses a strict run unless every decision is sealed and all of
them share one assessed range and one evidence digest. Evidence that is
unsealed, edited or mutated in place adds the `EVIDENCE_NOT_ISSUED` gap. So do
hand-built evidence objects. Corporate-action events passed by a caller to
`assess` add `CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED` in strict mode. Strict
events come only from a `stock-gplm.m51-adjustment-events.v1` file named in the
evidence-paths document.

**Threat model.**

- *Defended:* caller-constructed decisions, datasets and assessments with
  self-consistent hashes. Seals copied onto other content. Decisions decoded from
  JSON, pickles or other processes; the key differs per process, which a test
  demonstrates through a subprocess. Evidence edited after loading. A genuine
  decision reused with other bars, another price basis, a different period or a
  different evidence set.
- *Not defended:* code inside the same interpreter that deliberately reads the
  closure key or patches this package. Python has no memory isolation, and such
  code could rewrite the runner itself. That attacker is out of scope.
- *Lifetime and storage:* the key exists only in process memory. It is never
  logged, serialized or persisted. Seals are meaningless outside the issuing
  process.
- *Replay:* re-presenting a sealed object works only for exactly the content it
  was issued for. That is idempotent, because the same evidence, request and
  bars give the same decision. Any change voids the seal.
- *API surface:* the sealing function is private, and only `verify_seal` is
  public. Nothing in `stock_gplm.m5.__all__` issues seals.

**Caller-supplied research is still allowed.** A list of caller-built datasets
runs as `DIAGNOSTIC_ONLY_NON_INVESTABLE`, labelled
`eligibility_issuance: CALLER_SUPPLIED_UNVERIFIED`.

**Provider price basis.** A new reviewed `stock-gplm.m51-provider-price-basis.v1`
registration replaces the unbacked `provider_price_basis` attribute. It names
the provider, the basis (`RAW_UNADJUSTED`, `PROVIDER_BACK_ADJUSTED` or `MIXED`),
symbols, an effective range and a document hash, and it uses a canonical-hash
identifier. Share events are applied only when a `RAW_UNADJUSTED` registration
covers the symbol and range. No registration exists for the Shoonya history, so
real data stays `PROVIDER_PRICE_BASIS_UNVERIFIED`.

## B — Requested-universe integrity

**Defect.** The Studio and CLI de-duplicated and sorted symbols, assessed each
one, and ran on the survivors. A strict request for `ALPHA-EQ` and `ABSENT-EQ`
produced an investable run on `ALPHA-EQ` alone.

**Fix.** `UniverseRequest` is the requested universe. `universe_problems`
validates it with no normalization and returns per-entry reasons:
`DUPLICATE_OF_INDEX_n`, `SYMBOL_NOT_CANONICAL_CASE`,
`SYMBOL_HAS_SURROUNDING_WHITESPACE`, `SYMBOL_INVALID_FORMAT`,
`SYMBOL_NOT_A_STRING` or `AMBIGUOUS_WITH_INDEX_n`. Its canonical identity is the
sorted member list plus range, mode and subset flag (`universe_sha256`). The
requested order is recorded too. `assess_universe` assesses every member and
returns:

| Mode | Every member usable | Some member not usable |
| --- | --- | --- |
| `STRICT_RESEARCH` | `ELIGIBLE` (all must be `ELIGIBLE`), all released | `BLOCKED` or `REJECTED`, nothing released |
| `DIAGNOSTIC_ONLY` | `DIAGNOSTIC_ALLOWED`, all released | `REJECTED`, nothing released |
| `DIAGNOSTIC_ONLY` + `diagnostic_subset` | `DIAGNOSTIC_ALLOWED`, all released | `DIAGNOSTIC_ALLOWED`, usable members released, the others listed in `excluded` |

The Studio returns 400 with `problems` for invalid lists and 422 with every
member's decision when nothing may run. The CLI exits with status 2 and prints
`INVALID_UNIVERSE` or the universe status. The run records
`requested_universe` (canonical form, members, exclusions), and its hash is part
of `run_id`. `diagnostic_subset` is refused outside `DIAGNOSTIC_ONLY`.

## C — Corporate-action reconciliation

**Defect.** M2.1 catalog actions were matched to M3.9-B events by type and
ex-date only. `adjustment_status` was ignored, including `UNKNOWN`. Ratios and
other details were never compared. Events with no catalog action were silently
dropped.

**Rules.** Every catalog action whose effective date falls after the first
released bar and on or before the request end is reconciled. Each outcome is
stored in `decision.corporate_action_reconciliation`.

| Check | Rule | Failure | Class |
| --- | --- | --- | --- |
| Type | Only these aliases normalize: `SPLIT`/`STOCK_SPLIT` → `STOCK_SPLIT`; `REVERSE_SPLIT`/`CONSOLIDATION` → `REVERSE_SPLIT`; `BONUS` → `BONUS`; `DIVIDEND`/`CASH_DIVIDEND` → `CASH_DIVIDEND` (excluded, price return). Anything else is unsupported. | `CORPORATE_ACTION_UNSUPPORTED` | EVIDENCE |
| Ex-date and symbol | Catalog `effective_date` must equal event `ex_date`. The catalog symbol and the event symbol must both equal the request symbol. | unmatched or unresolved | — |
| Status | `UNKNOWN` or `POLICY_REQUIRED` blocks. On a registered `RAW_UNADJUSTED` basis, share actions must be `UNADJUSTED` and dividends `UNADJUSTED` or `NOT_APPLICABLE`. `ADJUSTED` contradicts the basis. | `CORPORATE_ACTION_STATUS_UNRESOLVED` / `CORPORATE_ACTION_CONTRADICTION` | EVIDENCE / INTEGRITY |
| Ratio | Share actions need `details.ratio` written as `A:B`, with positive integers. SPLIT and REVERSE_SPLIT ratios are new:old; BONUS is bonus:held. Both orientations match M3.9-B `new_shares:base_shares`. Ratios compare as reduced fractions, so `4:2` equals `2:1`. | `CORPORATE_ACTION_FIELDS_MISSING` / `CORPORATE_ACTION_CONTRADICTION` | INTEGRITY |
| Other details | Allowed keys: share actions `ratio`, `record_date`, `event_id`; dividends `amount_inr`, `record_date`, `event_id`. Each value present must equal the event's. A value the catalog states but the event lacks counts as missing. Unknown keys are rejected. | `…_FIELDS_MISSING`, `…_CONTRADICTION`, `…_DETAILS_UNSUPPORTED` | INTEGRITY |
| Duplicates | Two catalog actions with the same normalized type and date, or two validated events for one action. | `CORPORATE_ACTION_AMBIGUOUS` | INTEGRITY |
| Unmatched | A validated, licensed event in range with no catalog action. | `CORPORATE_ACTION_EVENT_UNMATCHED` | INTEGRITY |
| Missing event | A split or bonus with no validated, licensed event. | `CORPORATE_ACTION_EVENT_UNRESOLVED` | EVIDENCE |

INTEGRITY failures reject in every mode, so contradictory evidence is not
available even to diagnostics. Share events are applied only when every check
passes on a verified raw basis. The applied events are bound into the decision,
and `VerifiedHistoricalDataset.verify` checks them.

## D — One verified run loader

**Defect.** `compare_runs` and the Studio parsed `run.json` directly. A directory
containing a syntactically valid `run.json` claiming a 9,900% return was ranked
first.

**Fix.** Every reader goes through `stock_gplm.m5.run_store.load_run`: listing
(`scan_runs`), details and reports, Studio endpoints, CLI `export` and
`compare`, and ranking. It checks, in order:

- the directory is real and named by a run ID;
- there are no symlinks;
- the file inventory is exact;
- every file matches the manifest hash;
- the manifest contract, `run_id`, inventory and non-analytical-file list;
- the `run.json` contract and `run_id`;
- `run_id` re-derives from the binding fields;
- `environment.json` names the same run;
- the classification agrees with the mode and issuance;
- no holdout rows were released or scored;
- the performance block recomputes exactly from the stored orders, trades, open
  positions and equity curve;
- the equity curve matches the evaluation range;
- `report.md` re-renders exactly.

`RunNotFound`, a `KeyError` that becomes HTTP 404, means ordinary missing data.
`RunIntegrityError`, a `ValueError` that becomes HTTP 400 or CLI exit 3, means
the run exists but cannot be trusted.

| Level | Meaning | Rankable |
| --- | --- | --- |
| `INTEGRITY_VERIFIED` | all of the above | no |
| `REPLAY_VERIFIED` | re-running through the adapter (current raw data and evidence) and the registered strategy version reproduces every analytical byte | yes, if strict and investable |
| `LEGACY_UNVERIFIED` | a pre-M5.1 run (`…-run.v1` manifest) whose hashes match; listed and shown with that label | never |
| `INTEGRITY_FAILED` | shown in listings with the reason, never served as a result | never |

**Local attacker model.** Hashes and internal recomputation catch accidental
corruption, partial edits and naive fabrication. They do **not** authenticate:
someone who can write the reports directory can fabricate a run, manifest and
equity curve that agree with each other. Ranking therefore requires replay, which
derives the result again from raw data, evidence and registered code instead of
trusting the artifact. Someone who can also write the raw data, the evidence or
the code is outside what any local check can detect. The four pre-M5.1 genuine
runs in `reports/m5/runs` classify as `LEGACY_UNVERIFIED`. They were left
unmodified.

## E — Provenance

`code_identity` used to hash only `v1/*.py` and `m5/*.py`, and it put git state
into `run_id`. `stock_gplm.m5.provenance` now separates two things:

- **Content identity**, part of `run_id`. It contains per-component SHA-256 over
  `{package-relative POSIX path: file SHA-256}` for M1 publication validation
  (`ingestion`), the M2 importers (`m2`), M3.9-B (`m39b`), V1 (`v1`) and M5
  (`m5`), plus the package `__init__`, the adapter version and the Python
  implementation and major.minor. It contains no absolute paths, hostnames,
  timestamps or git state. A copy of the package at another path gives the same
  identity. Editing one component changes only that component's hash.
- **Environment**, in `environment.json`. It is hashed in the manifest but is
  non-analytical and not part of `run_id`. It records git commit and dirty flag,
  the full Python version and build, platform, the SHA-256 of `pyproject.toml`
  and both lockfiles, and the versions of any loaded non-standard-library
  modules. A test enforces that the execution path imports only the standard
  library, so lockfiles are environmental facts.

Strategy ID, version and configuration hash are already in `run_id`. Data,
calendar, corporate-action and price-basis evidence versions enter `run_id`
through each decision's evidence digest. They are listed per run in
`evidence_versions`. The cost basis is bound through the settings.

## F — Studio security

`make_server` (used by `serve`) accepts only `127.0.0.1` or `localhost`, which it
binds as `127.0.0.1`. Every other value is refused before a socket or the
registry is opened, including `0.0.0.0`, `::`, `""`, routable IPs, hostnames and
`None`. After binding it asserts that the bound address is loopback.
`LoopbackHTTPServer.verify_request` drops any connection whose peer IP is not
loopback (IPv4-mapped loopback included). Names are never resolved.

Each request then checks, in order:

1. the peer address is loopback;
2. `Host` is the Studio's own `127.0.0.1:<port>` or `localhost:<port>`;
3. an `Origin`, if present, is the Studio's own (`null` is rejected);
4. a request with no `Origin` is accepted only if `Sec-Fetch-Site` is absent,
   `same-origin` or `none`.

A missing `Origin` is never treated as proof of a trusted peer: the peer-address
check is what admits the request. No new endpoint was added.

## G — Terminal valuation

The strategy marked open positions at the close with no exit costs. The
benchmark deducted exit slippage and fees. Their returns did not measure the
same thing. Both now use `LIQUIDATION_ESTIMATE_AT_FINAL_OBSERVED_CLOSE_V1`:
`total_return` values units still held at close × (1 − slippage) × (1 − sell
fee rate), the same convention as a realized sell.
`total_return_marked_to_market` is reported alongside for both.

Entry costs are paid once, closed trades have already paid their exit costs, and
only open positions receive the estimate, so nothing is counted twice. Costs
were not weakened. The headline return can only fall: on the genuine diagnostic
run, −2.462% marked to market became −2.489% after liquidation.

## H — Holdout wording

The M3.2 boundaries are unchanged. The five shorter horizons start 2026-04-29,
6–12m starts 2025-10-21, and M5 uses the earliest, **2025-10-21**. Runs replace
`locked_holdout_accessed` with `holdout_access`, which reports three separate
counts:

- `protected_rows_decoded_during_source_validation`: rows that `validate_daily`
  parsed in segments straddling the cutoff. This is 960 for the genuine
  20-symbol run.
- `protected_rows_released_to_execution`: computed from the released bars, and
  asserted to be 0.
- `protected_rows_scored_or_trained`: always 0.

In pre-M5.1 artifacts, `locked_holdout_accessed: false` means only "not released,
scored or trained".

## Remediation matrix

The tests listed are in `tests/test_m51_integrity.py` unless noted. Every
pre-fix row was reproduced with
`scratchpad/repro/reproduce_findings.py` against `a9d0c57`, and every post-fix row
with the same script on this branch.

| # | Finding | Pre-fix reproduction (a9d0c57) | Changed files | Tests | Post-fix | Residual risk |
| --- | --- | --- | --- | --- | --- | --- |
| A | Forged strict eligibility | forged bars + `ELIGIBLE` decision → `investable=True`, `STRICT_RESEARCH_HISTORICAL` | `m5/issuance.py` (new), `m5/models.py`, `m5/evidence.py`, `m5/adapter.py`, `m5/runner.py`, `tests/m5_fixtures.py` | `test_A_*` (9) | `ValueError: … issued by HistoricalDataAdapter in this process` | In-process code that reads the closure key (out of scope, documented) |
| B | Partial universe | Studio `ALPHA-EQ`+`ABSENT-EQ` strict → HTTP 201, `investable=True` | `m5/models.py`, `m5/adapter.py`, `m5/studio_api.py`, `m5/cli.py`, `v1/studio.py` (UI) | `test_B_*` (9) | HTTP 422 / CLI exit 2, `REJECTED`, nothing run | — |
| C | Corporate-action contradictions | `UNKNOWN` status + `3:1` vs event `2:1` → `ELIGIBLE` | `m5/models.py`, `m5/adapter.py`, `m5/evidence.py` | `test_C_*` (18) | `REJECTED`: `…STATUS_UNRESOLVED`, `…CONTRADICTION` | The normalization table is a contract for evidence that does not exist yet; a real catalog may need a new reviewed version |
| D | Forged ranking | fabricated `run.json` (`total_return` 99.0) ranked first | `m5/run_store.py` (new), `m5/runner.py`, `m5/studio_api.py`, `m5/cli.py`, `m5/__init__.py` | `test_D_*` (15) | `RunIntegrityError`; ranking requires `REPLAY_VERIFIED` | Writers of raw data, evidence or code can forge anything (documented) |
| E | Narrow provenance | only `v1/` and `m5/` hashed; git state in `run_id` | `m5/provenance.py` (new), `m5/runner.py` | `test_E_*` (3) | five components + Python; environment separated | Python micro version and platform float differences are recorded, not bound |
| F | Programmatic non-loopback bind | `serve(host="0.0.0.0")` bound `('0.0.0.0', port)` | `v1/studio.py` | `test_F_*` (10) | `ValueError: … binds only to ['127.0.0.1', 'localhost']` | Other local users on the same machine can still reach loopback (unchanged, as before) |
| G | Terminal valuation mismatch | strategy marked at close without exit costs; benchmark deducted them | `v1/metrics.py`, `m5/runner.py` | `test_G_*` (3) | one policy for both; reconciliation within 4e-10 on genuine data | Final-close liquidation ignores market impact beyond slippage (documented limitation) |
| H | Holdout wording | `locked_holdout_accessed: false` implied no decoding | `m5/runner.py`, `docs/m5-historical-data-adapter.md`, this file | `test_H_*`, `test_run_provenance_points_to_exact_immutable_version` | decoded / released / scored reported separately | — |

## Genuine-data checks (2026-11-14, read-only)

These ran from the primary checkout with `PYTHONPATH=../stock-gplm-m45/src` and
`reports/m5/evidence-paths-2026-11-14.json`. Outputs are in `reports/m51/`
(git-ignored).

- `discover --verify`: 2,701 symbols, 10,355 complete segments, 0 incomplete,
  10,346 `ACCEPT`, 9 `QUARANTINE_CONTENT`. The publication lock is inactive.
- `assess RELIANCE-EQ` strict, 2022-10-31 to 2025-10-20: `BLOCKED`, with
  `adapter_issued: true` and `investable: false`. Gaps:
  `PIT_MEMBERSHIP_MISSING`, `CALENDAR_EVIDENCE_MISSING`, `ALIAS_EVIDENCE_MISSING`,
  `PROVIDER_PRICE_BASIS_UNVERIFIED`, `CORPORATE_ACTION_COVERAGE_MISSING`. There is
  no `EVIDENCE_NOT_ISSUED`, because the real evidence loads sealed. Diagnostic
  mode releases 727 bars ending 2025-10-20; 48 protected rows were decoded and
  withheld.
- Strict backtest of the prior 20-symbol universe: `BLOCKED`, exit 2, no run.
  Strict `RELIANCE-EQ ABSENT-EQ`: `REJECTED`, exit 2, no run. Duplicate
  `RELIANCE-EQ RELIANCE-EQ`: `INVALID_UNIVERSE`, exit 2.
- Diagnostic 20-symbol swing-breakout run `f1eee07f…`, run twice into separate
  roots. All analytical files and `environment.json` were byte-identical; only
  `created_at_utc` in the manifest differs. The run is `INTEGRITY_VERIFIED`, and
  replay gives `REPLAY_VERIFIED`. It is still `DIAGNOSTIC_ONLY_NON_INVESTABLE`
  and never ranked.
- `data/`: SHA-256 of all 31,704 files before and after the work is identical
  (manifest digest `51398f56…3edd6`).
