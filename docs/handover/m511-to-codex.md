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
| HEAD | `667ab61` fix(m511): remove the directly importable seal signer (audit finding A.1) |
| Handover docs | committed in the commit after `667ab61` (docs only); working tree otherwise clean |

The main checkout `/home/mgurunath/projects/stock-gplm` is on
`feature/v1-configurable-strategy-engine` and has the audit untracked under
`docs/reviews/`. Do not work there.

## 3. Status of findings A–H

| Finding | Status | Notes |
| --- | --- | --- |
| A.1 importable signer | **FIXED** (`667ab61`) | See §4. |
| A.2 freshness / revalidation | NOT STARTED | P0. Design in §6.2. |
| A.3 heuristic ETF + unverified costs not gated | NOT STARTED | P0. Design in §6.1. |
| A.4 synthetic/unapproved evidence can reach strict | NOT STARTED | P0/P1. Design in §6.1. |
| B atomic universes | PASS in audit, no work needed | |
| C evidence authority | NOT STARTED | P1. Design in §6.4. |
| D fabrication/ranking | PASS in audit, no work needed | |
| E provenance completeness | NOT STARTED | P1. Design in §6.6. |
| F local API authentication | NOT STARTED | P1. Design in §6.5. |
| G accounting | PASS in audit, no work needed | |
| H holdout validation isolation | NOT STARTED | P0. Design in §6.3. |

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

## 5. Test results

Command (the worktree has no `.venv`; without `PYTHONPATH=src` the **main checkout** is
imported instead and results are meaningless):

    cd /home/mgurunath/projects/stock-gplm-m45
    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider

| Revision | Result |
| --- | --- |
| `0a25f86` baseline | 734 passed, 2 skipped |
| `667ab61` | **736 passed, 2 skipped, 0 failed** |

The two skips (`test_m22_master_validation.py:95`, `test_m2_quality.py:272`) need local
real-data files that are not in this worktree. The audit's 736/0-skip figure came from an
environment that had them. All evidence here is synthetic.

## 6. Remaining work, in priority order

### 6.1 P0 — strict authority gates (audit criteria 3, 4, 5)

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
