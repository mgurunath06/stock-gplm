# M5.1.1 remediation — handover to Codex

Written 2026-11-14. Self-contained: no access to the originating Claude session is needed.

## 1. Objective

Close the M5.1 independent audit (`docs/reviews/m51-independent-audit.md`, audited
revision `0a25f86`) as milestone **M5.1a / M5.1.1 strict authorization closure**. The
acceptance criteria are in §10 of the audit (ten measurable items). Project rules are in
`AGENTS.md`; read it first.

Audit verdict per finding: A FAIL; B PASS; C PARTIAL; D PASS; E PARTIAL; F PARTIAL;
G PASS; H PARTIAL. Strict real-data research stays **BLOCKED** regardless of code
changes: there is no registered real calendar, PIT membership, aliases, complete
corporate actions or provider price basis evidence.

## 2. Location and Git state

| Item | Value |
| --- | --- |
| Worktree | `/home/mgurunath/projects/stock-gplm-m45` |
| Branch | `fix/m511-strict-authorization` (local only, never pushed) |
| Base | `feature/m51-integrity-hardening` @ `0a25f864dd4ea255a828a4ff6cce3a66d02c04bc` |
| HEAD | docs commit after `621e39e` (see `git log -1`). Code commits: `667ab61` A.1, `2cd419a` A.3/A.4, `621e39e` A.2 |
| Working tree | clean |

The main checkout `/home/mgurunath/projects/stock-gplm` is on
`feature/v1-configurable-strategy-engine` and has the audit untracked under
`docs/reviews/`. Do not work there.

## 3. Status of findings A–H

| Finding | Status | Notes |
| --- | --- | --- |
| A.1 importable signer | **FIXED** (`667ab61`) | See §4. |
| A.2 freshness / revalidation | **FIXED** (`621e39e`) | See §4c. |
| A.3 heuristic ETF + unverified costs not gated | **FIXED (gate)** | Strict always BLOCKED until authoritative sources exist. See §4b. |
| A.4 synthetic/unapproved evidence can reach strict | **FIXED (gate)** | See §4b. The positive approval path is C (§6.4). |
| B atomic universes | PASS in audit, no work needed | |
| C evidence authority | FIXED IN CODE (pending checkpoint commit) | Separate hash-pinned trust policy, structured registrations, classification and cost loaders; genuine approvals remain unavailable. |
| D fabrication/ranking | PASS in audit, no work needed | |
| E provenance completeness | NOT STARTED | P1. Design in §6.6. |
| F local API authentication | NOT STARTED | P1. Design in §6.5. |
| G accounting | PASS in audit, no work needed | |
| H holdout validation isolation | FIXED (`ca3bd4e`) | M5 validates whole-source bytes but decodes only pre-cutoff candle content; protected-only metamorphic test added. |

## 4. Completed: A.1 — directly importable signer

**Root cause.** `stock_gplm.m5.issuance` exposed a module-global `_seal(kind, digest)`
signer. `from stock_gplm.m5.issuance import _seal` let a caller seal a hand-built
`ELIGIBLE` decision and obtain an investable `STRICT_RESEARCH_HISTORICAL` run.

**Remedy.**
- `src/stock_gplm/m5/issuance.py`: no module-level signer. `_issuer()` returns
  `_claim_signer(kind)` (each of `decision`, `universe`, `evidence` can be claimed once;
  a second claim raises `RuntimeError`) and `verify_seal`. The decorator `_issues(kind)`
  claims the signer at decoration time and passes it to the decorated function as `_sign`.
  A caller-supplied `_sign` raises `TypeError`. `__wrapped__` is removed.
- `src/stock_gplm/m5/adapter.py`: `HistoricalDataAdapter.assess` is
  `@_issues("decision")`, `assess_universe` is `@_issues("universe")`.
- `src/stock_gplm/m5/evidence.py`: `load_reference_evidence` is `@_issues("evidence")`.
- Claims happen while `stock_gplm.m5` is imported (its `__init__` imports adapter and
  evidence), so they always precede caller code. Closure-cell reading and package
  patching stay explicitly out of scope (same-interpreter hostile code).
- Tests in `tests/test_m51_integrity.py`:
  `test_A_no_importable_signer_can_seal_a_forged_decision`,
  `test_A_issuing_functions_reject_a_caller_supplied_signer`.
- Docs: `docs/m51-integrity-hardening.md` (API surface bullet and a "not yet closed" note).
- `docs/reviews/m51-independent-audit.md` was added to the branch unchanged.

**Risk.** `importlib.reload` of `adapter` or `evidence` now raises because the kind is
already claimed. That is intentional. Do not add a reset hook.

## 4b. Completed: A.3/A.4 — strict authority gates

**Root cause.** Strict eligibility accepted any structurally valid local evidence.
`synthetic://` manifests, the M3.7 ETF naming heuristic and caller-asserted (even zero)
costs could all yield an `ELIGIBLE`, investable strict run.

**Remedy.**
- `src/stock_gplm/m5/adapter.py`: `asserted_sources()` walks the evidence (dicts,
  dataclasses, sequences; depth ≤ 6) for `source`, `source_reference`, `reference`,
  `source_identifier` and `source_url` strings. `strict_authority_failures()` returns
  `EVIDENCE_SYNTHETIC` when any of them contains a `SYNTHETIC_MARKERS` word, and
  **always** returns `EVIDENCE_AUTHORITY_UNAPPROVED`, `INSTRUMENT_CLASSIFICATION_UNVERIFIED`
  and `COST_SCHEDULE_UNREGISTERED`, because no authoritative source for those exists in
  code yet. `assess` applies them only in `STRICT_RESEARCH`.
- `src/stock_gplm/m5/models.py`: the four codes are EVIDENCE class, so strict →
  `BLOCKED` and diagnostics are unaffected.
- `tests/m5_fixtures.py`: TEST-ONLY `waive_strict_authority` fixture (monkeypatches the
  policy to `[]`). `tests/test_m51_integrity.py` and `tests/test_m5_historical_adapter.py`
  opt in with `pytestmark` because they test strict run, replay, ranking and accounting
  machinery on synthetic fixtures.
- `tests/test_m511_authority.py` (9 tests, no waiver): synthetic evidence → BLOCKED with
  all four codes; strict universe and zero-cost strict run fail before writing; diagnostic
  run still succeeds; clean-looking sources are still unapproved; nested markers found.
- Docs: `docs/m51-integrity-hardening.md`.

**To lift a gate later (Codex):** replace the unconditional entry with a check of a
loaded, checksum-bound authoritative source. For example, add `cost_schedule` and
`instrument_classification` keys to the evidence-paths contract, load and hash them in
`load_reference_evidence`, and verify coverage for the symbol and range. Never delete a
gate without that replacement. When a positive path exists, add a non-waived test for it.

**Residual risks.** Marker matching is a denylist and only one layer of defence. The real
protection is that `EVIDENCE_AUTHORITY_UNAPPROVED` always fails until C is built. Words
such as `test` can appear in legitimate references; that fails closed (BLOCKED), which is
acceptable. The waiver fixture must never move into `src/` or a non-test conftest.

## 4c. Completed: A.2 — assessment freshness (`621e39e`)

**Root cause.** A sealed in-memory `UniverseAssessment` stayed strict-authorizing after
its raw root moved; the runner never re-read sources.

**Remedy.**
- `m5/adapter.py`: segment records add `done_sha256` and `meta_sha256` (bound into the
  decision; relative names, so relocation-stable). Issued decisions get
  `revalidator = adapter.revalidate`. `HistoricalDataAdapter.revalidate(decision)`:
  - checks the evidence digest matches;
  - reloads `evidence.config_path` and compares digests, so modified, removed or
    unregistered evidence fails;
  - checks the symbol directory exists and has no unexpected entries;
  - checks the overlapping-segment set is unchanged;
  - re-hashes payload, `.done` and `.meta.json` bytes (no decoding).
- `m5/models.py`: `StaleAssessmentError(ValueError)` and a non-binding, non-serialized
  `revalidator` field. `DataEligibilityDecision.revalidate()` fails closed when no
  revalidator is attached.
- `m5/evidence.py`: `ReferenceEvidence.config_path` (excluded from the digest).
- `m5/runner.py` `_resolve_release`: strict universe → every decision revalidated (atomic);
  strict dataset list → every dataset revalidated. Diagnostic runs are not revalidated.
  Replay re-assesses from files, so it is unchanged.
- `m5/__init__.py`: exports `StaleAssessmentError`.
- `tests/test_m511_freshness.py` (13 tests; uses the test-only waiver to reach strict):
  - unchanged sources run;
  - each of payload modified/replaced, `.done` modified/removed, meta modified/removed
    and symbol directory moved in BETA only blocks the whole universe and writes nothing;
  - master modified, paths file removed and source unregistered each fail;
  - a stale dataset list is rejected;
  - diagnostic runs are unaffected.

**Residual risks.**
- TOCTOU: there is a window between the hash check and use. The run uses in-memory bars
  verified at check time, so later changes cannot alter that run; they are detected only
  by the next revalidation or by replay.
- The payload hash recorded at assessment comes from `validate_daily`, while `.done` and
  meta are hashed moments later in the same pass; a change in that sub-second window is
  caught at revalidation.
- Same-process code can replace `revalidator` (out of scope, as with seals).
- Evidence reload re-reads the M3.2 split-plan metadata, not holdout candles.

## 5. Test results

Command (the worktree has no `.venv`; without `PYTHONPATH=src` the **main checkout** is
imported instead and results are meaningless):

    cd /home/mgurunath/projects/stock-gplm-m45
    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider

| Revision | Result |
| --- | --- |
| `0a25f86` baseline | 734 passed, 2 skipped |
| `667ab61` | **736 passed, 2 skipped, 0 failed** |
| `2cd419a` | **745 passed, 2 skipped, 0 failed** |
| `621e39e` | **758 passed, 2 skipped, 0 failed, 0 deselected** |

The two skips (`test_m22_master_validation.py:95`, `test_m2_quality.py:272`) need local
real-data files that are not in this worktree. The audit's 736/0-skip figure came from an
environment that had them. All evidence here is synthetic.

## 6. Remaining work, in priority order

### 6.1 P0 — strict authority gates (audit criteria 3, 4, 5)

**DONE as blocking gates (§4b).** Remaining here: the positive paths (registered cost
schedule and authoritative classification loaders). The notes below are kept for that work.

- **ETF classification.** `adapter.py` (~line 237) always appends
  `EligibilityEvidence("instrument_classification", "HEURISTIC", …)` and only fails
  `ETF_EXCLUDED` when the heuristic matches. In `STRICT_RESEARCH`, add an EVIDENCE-class
  failure (e.g. `INSTRUMENT_CLASSIFICATION_UNVERIFIED`) whenever classification is only
  heuristic. Keep `ETF_EXCLUDED` as is. Check `FailureClass` mapping in `models.py` so the
  new code is EVIDENCE (→ `BLOCKED`), not INTEGRITY.
- **Cost schedule.** `runner.py` `BacktestSettings.cost_basis` is free text; strategy
  `trading_costs` (`v1/schema.py:280`) are caller bps. For strict runs, require a
  registered, checksum-bound cost profile (new evidence-paths key, loaded and hashed by
  `load_reference_evidence`) with effective dates covering the evaluation range. Zero
  components are allowed only when the profile says so explicitly. Bind the profile hash
  into the run identity. Without it, strict must fail before an investable run exists.
- **Synthetic evidence.** M2.1 manifests accept any non-empty `source`/`source_reference`
  (`m2/reference_data.py:73-136`). Add an explicit trust domain: evidence whose source,
  reference or environment tag marks it synthetic/fixture/test/unapproved can produce
  only diagnostic output. Do **not** change the M2.1 manifest contract silently; add a
  versioned field or an M5-side policy check, and update tests in `tests/m5_fixtures.py`
  (their fixtures will then need a tag that the tests explicitly allow in a test-only
  domain that can never yield `investable=True`).
- Expect many existing strict-ELIGIBLE fixtures to become BLOCKED. Update tests to assert
  the new blockers; never weaken a gate to keep an old test green.

### 6.2 P0 — freshness / revalidation (audit criterion 2)

**DONE (§4c).** Remaining: Studio and CLI surface `StaleAssessmentError` as a generic
error (the CLI exits non-zero); consider a dedicated 409 response. Original notes follow.

Problem: `runner._resolve_release` (`runner.py:186-232`) trusts a sealed in-memory
assessment; moving the raw root afterwards still yields an investable run.

Recommended design: one trusted operation. Add an adapter method (e.g.
`HistoricalDataAdapter.revalidate(assessment)`) or make `run_historical_backtest` take the
adapter and re-run `assess_universe` on the same `UniverseRequest` immediately before
`build_run`, requiring identical `assessment_id`. Evidence files must also be re-read:
store the evidence-paths file path on `ReferenceEvidence` (outside the binding so run IDs
stay relocation-stable) and reload it, comparing `evidence_digest`. Only then set
strict/investable. Studio (`m5/studio_api.py:62-94`) and CLI (`m5/cli.py:112-138`) both
already hold the adapter, so wiring is small. Tests: rename/delete/modify payload,
`.done`, `.meta.json` and each evidence file after assessment, and assert that strict
build fails before any run directory is written.

### 6.3 P0 — holdout validation isolation (audit criterion 6)

**DONE in the Codex continuation (commit recorded in the progress log).**
`m5/holdout_validation.py` is an M5-only validation path. Segments wholly before
the cutoff still use the established M1/M2 validator. For a straddling segment,
the new path verifies compressed bytes, the completion marker, metadata, record
count and bounded decompression for the entire immutable source, but parses
candle values only for rows dated before the cutoff. Protected rows never enter
OHLCV/schema/duplicate/chronology checks, decisions, warnings or ordinary logs.
`test_H_protected_candle_mutation_cannot_change_development_research` corrupts
only synthetic protected candle structures and proves identical development
bars, statuses, failures, warnings, signals, orders, trades, equity, performance
and benchmark results. Full payload hashes remain provenance and stale-assessment
guards, so a changed source still rejects a previously issued assessment.

Original design note retained below:

`validate_daily` (`ingestion/daily.py:176-279`) validates whole segments, so a malformed
protected row can change the disposition of pre-holdout bars, and error messages can
reveal protected dates. Options: (a) byte-integrity (hash, size, marker) for the whole
segment, but content validation only on rows dated before the cutoff, with protected-row
failures reported as a segregated `PROTECTED_INTEGRITY_ALARM` that carries no dates or
values; or (b) require segment boundaries that end before the holdout. Do not modify
`ingestion/daily.py` semantics for M1/M2 callers. Add an M5-side path. Add a metamorphic
test that mutates only post-cutoff rows (synthetic) and asserts identical development
bars, signals, metrics and ordinary logs. Never decode or score the real holdout to test.

### 6.4 P1 — evidence authority (C)

**DONE IN CODE in the Codex continuation (commit recorded in the progress log).**
See `docs/m511-evidence-authority.md`. Registrations verify actual source bytes,
structured provenance and dependency hashes, but never approve themselves. A
separate reviewer trust policy is accepted only with an out-of-band exact
SHA-256. Synthetic registrations remain unapproved even if listed in a policy.
Effective-dated authoritative instrument classification and cost-schedule
loaders replace the unconditional gates only when their registrations and the
whole evidence bundle satisfy that policy. Strict execution also requires exact
cost component and profile-ID equality. No genuine approval was fabricated;
strict real-data research remains blocked.

Original design note retained below:

`load_adjustment_events` (`m5/evidence.py:269`) trusts `validation_state` and
`licence_status=APPROVED_FOR_DERIVATION` written inside the JSON. The evidence-paths
document itself is unhashed. Introduce a versioned, checksum-bound evidence bundle
recording source type, acquisition method (manual browser download for NSE), source
document bytes hash, licence decision and reviewer approval, and verify the referenced
document bytes. NSE downloads must stay manual (AGENTS.md M3.6). Never script them.

### 6.5 P1 — local API authentication (F)

`v1/studio.py` checks peer, Host and Origin (`~167-205`) but has no client
authentication. Add a per-launch `secrets.token_urlsafe(32)` token, inject it into the
served page, require it (header) on every API request, and compare with
`hmac.compare_digest`. Return 403 when it is missing or wrong. Set owner-only permissions
on registry/report directories. Document that a loopback reverse proxy expands exposure.
Live tests: unauthenticated GET/POST → 403, and proxy-style requests with allowed headers
but no token → 403.

### 6.6 P1 — provenance completeness (E)

Record payload, `.done` and `.meta.json` hashes per segment in the decision or run
provenance. Require both lockfiles (`requirements-dev.lock`, `requirements-m35.lock`) in
`m5/provenance.py:61-89`. Keep Python micro/platform as environment metadata and require
replay before comparison.

### 6.7 P2

Durable, checksum-bound audit-evidence location for reproduction scripts (audit §9.7).
Architecture gaps (§9.8) are out of scope for M5.1.1.

### Closing steps for the milestone

1. Full suite passes with 0 failures (record skips and why).
2. Capture raw-data and existing-report tree hashes before and after, in a retained,
   checksum-bound manifest outside raw directories (criterion 10).
3. Update `docs/m51-integrity-hardening.md` (or a new `docs/m511-*.md`) with the A–H
   matrix, and keep `docs/handover/m511-progress.md` current.

## 7. Known implementation risks

- Changing decision or run binding changes `decision_id`/`run_id`. Any edit under M5,
  V1, M2 or M1 ingestion already changes the code fingerprint, so existing M5.1 genuine
  artifacts will no longer replay to the same ID. They are `INTEGRITY_VERIFIED`, not
  rankable, and must **not** be rewritten. Never overwrite prior run directories or
  acceptance evidence.
- Adding strict gates will turn current synthetic strict-ELIGIBLE fixtures BLOCKED, so
  expect wide test churn in `tests/m5_fixtures.py`, `test_m51_integrity.py`,
  `test_m5_historical_adapter.py` and `test_m5_studio.py`.
- The seal remains same-process only. Do not describe it as authentication.

## 8. Outstanding strict-data blockers (unchanged; not closable by code)

Official trading calendar, point-in-time listing history, symbol aliases, complete
corporate actions, provider price basis, authoritative ETF classification and a verified
transaction-cost schedule. See audit §8. The safe state is `BLOCKED`/`UNKNOWN`. Never
substitute a heuristic.

## 9. Resume and verify

    cd /home/mgurunath/projects/stock-gplm-m45
    git status && git log --oneline -3          # expect HEAD 667ab61 on fix/m511-strict-authorization
    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider
    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_m51_integrity.py -k test_A_

## 10. Codex must not

- Push, merge, deploy, or touch remotes. No network, broker, Oracle or credential access.
- Modify raw provider files, `.done` markers, metadata, or prior run/acceptance
  directories. Reports go outside raw directories.
- Decode, unlock or score the locked holdout, or edit gates, splits, labels or the M3
  trial ledger.
- Script NSE downloads or bypass its access controls.
- Relax any existing gate, re-add a module-level signer, or add a signer reset hook.
- Treat synthetic tests as evidence of real-data readiness or profitability.
- Rewrite `docs/reviews/m51-independent-audit.md`. It is the audit record.
