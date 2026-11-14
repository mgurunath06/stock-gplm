# M5.1 independent integrity, security and integration audit

Date: 2026-11-14  
Audited revision: `0a25f864dd4ea255a828a4ff6cce3a66d02c04bc`  
Parent: `a9d0c57b5a283f435502fa95ec53b84ea5591338`  
Audit method: read-only source, Git and existing-evidence inspection; exact-commit
exported-tree tests; synthetic adversarial probes. No protected holdout was unlocked or
scored, no real raw file was changed, and no network, credential, broker, merge, push or
deployment operation was performed.

## 1. Executive assessment

M5.1 is a substantial improvement over M5.0. Atomic universes, corporate-action
field reconciliation, replay-gated ranking, broader code fingerprints, loopback binding,
consistent terminal valuation and honest holdout counters all exist and are covered by
tests. The reported suite result is reproducible: **736 passed, 0 failed, 0 skipped or
deselected**.

It is not yet a complete strict-research trust boundary. Independent probes found that:

1. `stock_gplm.m5.issuance._seal` is directly importable. Calling it converts a
   caller-constructed `ELIGIBLE` decision into an investable strict run. This is within
   the documented same-interpreter exclusion, but it makes “issued only by the adapter”
   technically false and easier to bypass than the report's “read the closure key or
   patch the package” wording suggests.
2. A valid assessment has no freshness check. After its synthetic raw root was moved
   away, the in-memory assessment still authorized an investable strict run. Ranking
   later requires replay and would fail, but initial strict classification does not
   re-read the source.
3. Structurally valid local evidence is treated as authoritative. A manifest whose
   `source` was `synthetic-test-fixture` and whose reference was
   `synthetic://not-market-evidence` produced an `ELIGIBLE` strict decision. The seal
   proves which loader ran, not source authenticity or approval.
4. Two stated strict blockers are not code gates. An instrument classified only by the
   M3.7 heuristic can be strict-eligible, and a strict run can use zero costs with the
   free-text basis `caller assertion only`.
5. The local HTTP service is loopback-restricted but unauthenticated. A same-host user,
   process, browser extension or loopback reverse proxy can act as the client. A proxy
   can make a remote request appear to have a loopback peer.
6. Protected rows are not released or scored, but they are fully decoded and content-
   validated. A protected-row schema, chronology, duplicate-date or OHLCV failure can
   change the disposition of a segment and therefore the development-data decision.

The “seven fixes versus A-H” discrepancy is a counting/wording discrepancy. The commit,
report matrix and tests contain **eight** remediation areas, A-H. If H is described as a
wording/accounting clarification rather than a security fix, there are seven behavioral
security/integrity fixes plus one disclosure change; nevertheless H also changes the run
schema, so the most accurate count is eight remediations.

### Decisions

- **M5.1 CODE INTEGRATION: CONDITIONAL GO.** It is suitable as a non-release hardening
  baseline because it removes exploitable M5.0 behavior. Do not treat the merge as
  approval to enable strict classification. Before a release can expose strict mode,
  close the authoritative-classification and cost-evidence gaps, replace object seals as
  the strict authorization boundary, add freshness/revalidation, and isolate holdout
  validation. If integration cannot keep strict mode disabled until those conditions are
  met, this becomes NO-GO.
- **STRICT REAL-DATA RESEARCH: BLOCKED.** Current real evidence lacks calendar, point-in-
  time membership, aliases, complete corporate actions and provider price basis; ETF and
  cost authority are additionally ungated.
- **LIVE/PAPER EXECUTION READINESS: BLOCKED.** M5 explicitly marks paper/live eligibility
  false and has no broker route. The V1 paper intent path is local and non-routable, but
  local API authentication and shared end-to-end scanner/paper execution semantics are
  not release-ready.

Passing tests establish implementation behavior on synthetic fixtures. They establish
neither evidence authenticity nor strategy profitability.

## 2. Verified repository and branch state

### Repository and worktrees

Repository root: `/home/mgurunath/projects/stock-gplm`.

| Worktree | Branch / HEAD | State at audit start |
| --- | --- | --- |
| `stock-gplm` | `feature/v1-configurable-strategy-engine` / `0534cc8` | clean |
| `stock-gplm-m45` | `feature/m51-integrity-hardening` / `0a25f86` | clean; target commit present |
| `stock-gplm-m3` | `feature/m3-data-readiness` / `0307786` | clean |
| `stock-gplm-m34` | `feature/m34-minute-fusion` / `0307786` | dirty: 6 tracked modifications and 9 untracked paths; not touched |
| `stock-gplm-m25` | `fix/m25-auth-safety` / `fb050d9` | clean |
| `stock-gplm-m25-browser` | `fix/m25-browser-diagnostics` / `cb31f9a` | untracked `transfer/`; not touched |
| `stock-gplm-m25r` | `feature/m25r-cloud-recovery` / `dbba2b1` | clean |

The M5.1 branch currently has `origin/feature/m51-integrity-hardening` at the same
revision. Therefore the current repository state does **not** support the supplied
“nothing pushed” assertion, even if it was true when the brief was written.

### Ancestry and merge bases

The relevant committed line is linear:

`aea2fcb` M3.8 -> `df8324b` M3.9 -> `35bdc06` M3.9-B -> `0534cc8` V1 ->
`4822d54` M4 -> `658782b` M5 code -> `a9d0c57` M5 evidence/docs -> `0a25f86` M5.1.

Relevant merge bases and ahead counts:

| Left | Right | Merge base | Left/right unique commits |
| --- | --- | --- | --- |
| V1 `0534cc8` | M4 `4822d54` | `0534cc8` | 0 / 1 |
| M4 `4822d54` | M5 `a9d0c57` | `4822d54` | 0 / 2 |
| M5 `a9d0c57` | M5.1 `0a25f86` | `a9d0c57` | 0 / 1 |
| M3.8 `aea2fcb` | M5.1 | `aea2fcb` | 0 / 7 |
| M3.9-B `35bdc06` | M5.1 | `35bdc06` | 0 / 5 |

`main` is also an ancestor of M5.1. Tree-level merge simulations between M5.1 and
V1/M4/M5 reported no textual conflicts because each earlier branch is an ancestor. This
does not make the dirty, older M3.4 worktree safe to overwrite; its uncommitted work is a
separate integration concern.

### Commit identity and change set

`0a25f86` exists, is a commit, has parent `a9d0c57`, and has the stated subject:
`fix(m51): harden M5 eligibility, universes, corporate actions, run loading and Studio`.
It changes 19 files: 2,546 insertions and 235 deletions. New production modules are
`m5/issuance.py`, `m5/provenance.py` and `m5/run_store.py`; the new test file is
`tests/test_m51_integrity.py`. No `pyproject.toml`, requirement or lockfile changes occur
between M5 and M5.1. `git diff --check` is clean.

The implementation matches most of the report's file-level claims. Exceptions and
overstatements are recorded under A, C, E, F and H below.

## 3. A-H security and integrity audit

| Finding | Result | Independent conclusion |
| --- | --- | --- |
| A. Strict eligibility boundary | **FAIL** | External/copy/tamper cases are blocked, but the signer is directly callable and stale issued objects remain strict-authorizing without source revalidation. |
| B. Atomic universe rejection | **PASS** | Strict and default diagnostic requests are all-or-nothing; partial diagnostics require explicit opt-in and remain non-investable. |
| C. Corporate-action consistency | **PARTIAL** | Reconciliation is strong, but evidence authority is self-asserted by local files; file-loaded synthetic/licence assertions can become strict evidence. |
| D. Fabrication and ranking | **PASS** | Integrity loading plus mandatory replay protects ranking against report-only fabrication and checks all analytical outputs byte-for-byte. |
| E. Provenance and reproducibility | **PARTIAL** | Code coverage is broad and strategy/settings are bound, but source-sidecar identity and evidence authority are incomplete; Python micro/platform are environmental only. |
| F. Local API security | **PARTIAL** | Binding, peer, Host and Origin checks are sound defence in depth; there is no client authentication or protection from a trusted loopback proxy. |
| G. Consistent accounting | **PASS** | Strategy and benchmark use the same terminal liquidation estimate; open-position exit costs are deducted once. |
| H. Holdout isolation | **PARTIAL** | No protected row reaches execution or training, but protected values affect full-segment validation and may affect release eligibility. |

### A. Strict eligibility trust boundary — FAIL

**Original failure.** At M5.0 a self-consistent hash was treated as provenance; forged
bars and an `ELIGIBLE` decision produced an investable strict run.

**Implemented remedy.** A process-local HMAC key and seals cover evidence, decisions and
universe assessments. `DataEligibilityDecision.investable` requires strict mode,
`ELIGIBLE`, and a valid seal (`m5/issuance.py:45-57`, `m5/models.py:199-211`). Dataset and
universe consumers re-check bindings (`m5/models.py:228-246,359-402`), and the runner
checks issued decisions and common range/evidence (`m5/runner.py:186-232`).

**Independent verification.** The shipped tests correctly block caller construction,
copied seals, mutation, cross-process deserialization and mismatched evidence. Two
additional synthetic probes exposed residual authorization paths:

- `from stock_gplm.m5.issuance import _seal` followed by sealing a forged decision made
  `decision.investable == True`; `build_run` emitted `investable: true` and
  `STRICT_RESEARCH_HISTORICAL`. `_seal` is private only by naming convention and is
  imported by both the evidence loader and adapter (`m5/adapter.py:33-35`,
  `m5/evidence.py:387-390`). No closure-key extraction or package patch was needed.
- A genuine synthetic `UniverseAssessment` was created, then the raw root was renamed.
  The stale in-memory assessment still produced an investable strict run. The runner
  verifies object contents but does not re-open source files before classification.

An exact object copied with identical sealed content is intentionally reusable in the
same process. A changed object is rejected. A decision from another process is rejected.
The seal protects against accidental/public-API construction and serialization reuse; it
does not protect against same-interpreter code, local evidence fabrication, package
monkeypatching, or staleness of the underlying files.

**Coverage.** Nine A cases cover the intended public boundary, but no test calls the
directly importable signer or removes/changes sources after issuance.

**Required correction.** Do not use object possession as strict authorization. Make one
trusted operation re-assess sources and immediately build/publish the run, or require a
fresh replay before setting strict/investable. Treat malicious same-process code as
explicitly out of scope, and word the claim accordingly. Remove the module-global signer
to reduce accidental misuse, while acknowledging that Python cannot isolate hostile code
inside the interpreter.

### B. Atomic universe rejection — PASS

**Original failure.** Studio and CLI ran surviving symbols from a strict request.

**Implemented remedy.** `UniverseRequest` rejects empty/oversized, duplicate,
non-canonical, whitespace, invalid and non-string entries (`m5/models.py:255-336`).
`assess_universe` releases no strict data unless every member is eligible
(`m5/adapter.py:504-535`). Studio returns 400 for malformed lists and 422 when an assessed
universe releases nothing (`m5/studio_api.py:62-94`); CLI exits 2
(`m5/cli.py:123-138`).

**Independent verification.** Source tracing and synthetic tests confirm that an absent
member makes the entire strict universe `REJECTED`, while every per-member reason remains
visible. Duplicate and invalid entries fail before assessment. Diagnostic subsets require
`diagnostic_subset=true`, are recorded with exclusions and cannot be investable.

**Coverage.** Nine collected B cases exercise library, Studio and CLI paths, including
duplicates, case, whitespace, non-string entries and explicit diagnostic subset behavior.

**Residual limitation.** The browser UI trims comma-separated input before sending it,
so UI whitespace is normalized client-side; the server API itself remains strict. This
does not create partial strict execution.

### C. Corporate-action consistency — PARTIAL

**Original failure.** Matching used only type/date and ignored UNKNOWN status, ratio
contradictions, unmatched events and ambiguity.

**Implemented remedy.** The adapter normalizes a small allowlist, compares status,
reduced ratios, record dates, amounts and event IDs, rejects unsupported fields,
duplicates and unmatched events, and applies share events only on a registered
`RAW_UNADJUSTED` basis (`m5/adapter.py:54-64,98-145,377-502`). Strict caller-supplied
event arguments add `CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED`
(`m5/adapter.py:202-208`).

**Independent verification.** UNKNOWN/POLICY_REQUIRED block; ADJUSTED-vs-raw, ratio and
detail contradictions reject; duplicate catalogs/events and unmatched events reject.
Caller arguments cannot make a strict decision eligible. Externally created files can,
however, enter through the registered-evidence path:

- `load_adjustment_events` trusts `validation_state` and
  `licence_status=APPROVED_FOR_DERIVATION` stated inside the JSON; there is no separate
  signed/approved manifest or source-document hash verification (`m5/evidence.py:262-286`).
- M2.1 manifests hash the payload but accept arbitrary nonempty `source` and
  `source_reference` strings (`m2/reference_data.py:73-136,212-224`). A synthetic-test
  source is accepted structurally.
- The evidence-paths document is itself an unhashed path map from the caller's chosen
  file (`m5/evidence.py:348-390`). Its process seal proves only that the loader parsed it.

Thus external events cannot override already loaded evidence through the `assess`
argument, but a caller controlling the evidence registration files can create the strict
evidence. That is acceptable for synthetic tests, not an authority boundary for
investment-grade status.

**Coverage.** Eighteen C cases cover contradiction and normalization behavior well. They
do not test an approval registry or distinguish synthetic from approved real evidence.

**Required correction.** Introduce a versioned, checksum-bound evidence bundle whose
source type, acquisition method, document bytes, licence decision and reviewer approval
are independently verified. Synthetic fixtures must carry an explicit environment tag
that can never authorize production strict mode.

### D. Result fabrication and ranking — PASS

**Original failure.** Readers trusted `run.json`; a fabricated 9,900% return ranked first.

**Implemented remedy.** `load_run` enforces directory identity, exact regular-file
inventory, hashes, manifest/run/environment identity, classification, holdout counters,
performance recomputation, evaluation range and report rerendering
(`m5/run_store.py:112-222`). Replay reconstructs the universe and strategy, re-runs the
adapter and compares hashes of all analytical files: `run.json`, signals, orders, trades,
equity curve and report (`m5/run_store.py:241-268`). Ranking requires
`REPLAY_VERIFIED` (`m5/run_store.py:275-311`). Studio uses the verified loader and a real
replayer (`m5/studio_api.py:101-115`).

**Independent verification.** The two M5.1 genuine-data artifacts load as
`INTEGRITY_VERIFIED` and are not rankable. The four M5.0 runs load only as
`LEGACY_UNVERIFIED`. Missing manifests, altered/rehashed metrics, substituted files,
symlinks, missing raw data and registry hash disagreement fail integrity or replay.
Replay compares economically important outputs byte-for-byte, not only metadata.

**Coverage.** Fifteen D cases cover naive and self-consistent artifact fabrication,
source mismatch, inventory attacks, Studio and CLI consumers, legacy runs and replay.

**Residual limitation.** Integrity hashes are not authentication. An attacker who can
also change raw data, registered evidence or executing code can construct a replayable
world. This limitation is correctly documented. The reports directory should still be
permissioned to the owning user.

### E. Provenance and reproducibility — PARTIAL

**Implemented remedy.** `content_identity` fingerprints all Python files under M1
ingestion, M2, M3.9-B, V1 and M5, plus package init, adapter version and Python
implementation/major.minor (`m5/provenance.py:27-58`). Environment metadata records full
Python version/build, platform, Git status, `pyproject.toml` and lock hashes, and loaded
third-party versions (`m5/provenance.py:61-89`). Run identity also binds strategy ID,
version, canonical config hash, dataset fingerprint, requested universe, settings,
simulator version and evidence digests (`m5/runner.py:266-305`).

**Independent verification.** Relocation is stable and changing M3.9-B changes only the
expected component hash. The exact exported source was imported from `/tmp/.../src`, not
another checkout. Strategy configuration/feature expressions and costs are snapshotted
and hash-bound. M5 has no model artifact because it runs declarative V1 strategies, not
ML. Relevant M3 model artifacts are not inputs to an M5 run.

Remaining gaps:

- Segment payload hashes and normalized bar content are bound, but `.done` and metadata
  sidecar bytes are not independently listed in run provenance. They are validated at
  assessment time, and a later change causes replay failure, but the original sidecars
  cannot be reconstructed from the run.
- Evidence sources are hashes of the supplied local files, not proof that an approved
  authority supplied them. Synthetic manifests can pass strict structure.
- Python micro version, build and platform are recorded but not analytical identity.
  Replay detects resulting byte differences, but equal `run_id` is possible before that
  check.
- The environment records whichever of the two expected lockfiles exist; it does not
  require both.

**Coverage.** Three E cases validate component coverage, portability, environment
separation and standard-library-only imports. They do not test sidecar completeness,
source authority or runtime-induced numerical divergence.

### F. Local API security — PARTIAL

**Implemented remedy.** Only literal `127.0.0.1` and `localhost` are accepted and both
bind IPv4 loopback; non-loopback values fail before the registry opens
(`v1/studio.py:30-49,309-330`). Each handler verifies peer IP, exact Host and Origin, and
rejects cross-site `Sec-Fetch-Site` when Origin is absent (`v1/studio.py:167-182`). Bodies
are JSON and size-bounded (`v1/studio.py:193-205`).

**Independent verification.** Non-loopback bind attempts, spoofed remote peers, DNS-
rebinding Host values, foreign/null Origin and browser cross-site fetch metadata are
rejected. An absent Origin plus absent `Sec-Fetch-Site` is accepted from loopback, which
is necessary for non-browser clients but is not authentication.

The service has no bearer/session secret, CSRF nonce, OS-user peer credential or TLS.
Any process or other user able to connect to host loopback can read reports and mutate
the strategy registry through POST endpoints. A local reverse proxy can accept a remote
connection, originate a loopback connection and supply allowed headers; the Studio sees
only the trusted proxy peer. Browser extensions or compromised local applications are
also within reach. Therefore this is **loopback-restricted access, not authenticated local
access**.

**Coverage.** Ten F cases cover binds, peer literals, Host/Origin and live cross-site GETs.
They do not cover an allowed-header loopback proxy, other local users, authentication,
state-changing browser POSTs, or authorization boundaries.

**Required correction.** Use an unpredictable per-launch token for every API request and
state-changing action, bind it into the served page, reject missing tokens, and restrict
registry/report filesystem permissions. If OS-user identity is required, prefer a Unix
domain socket with peer credentials or an authenticated desktop bridge. Document that a
user-configured loopback reverse proxy expands exposure.

### G. Consistent accounting — PASS

`liquidation_unit_value` applies sell slippage and then sell fees
(`v1/metrics.py:36-38`). `performance_metrics` deducts that estimate only for positions
still open; closed trades already include exit costs (`v1/metrics.py:50-126`). The
benchmark uses the same entry and terminal convention (`v1/metrics.py:129-162`). Tests
reconcile:

`liquidation equity - initial capital = net realized P&L + unrealized P&L - estimated
terminal exit costs`.

Independent synthetic checks confirm no terminal double charge and equality at zero
cost. The M5.1 genuine diagnostic artifact reports -2.4619% marked-to-market and -2.4891%
liquidation-estimate return, as documented. `gross_pnl` and `net_pnl_realized` cover
closed trades; open-position economics are separately represented by unrealized P&L and
terminal exit estimate. The final-close estimate does not model market impact beyond the
configured slippage and is not an executable liquidation price.

### H. Holdout isolation — PARTIAL

The existing M5.1 artifact records 960 decoded protected rows, 0 released and 0 scored or
trained. Static inspection confirms the meanings:

- `validate_daily` decompresses and parses the whole overlapping segment, validates every
  row and only then returns normalized candles (`ingestion/daily.py:176-224,227-279`).
- The adapter counts rows at/after cutoff and excludes them before constructing
  `CanonicalDailyBar` objects (`m5/adapter.py:262-297`).
- `VerifiedHistoricalDataset.verify` rejects any released bar at/after cutoff
  (`m5/models.py:232-246`). The runner independently counts released protected rows and
  asserts zero (`m5/runner.py:275-321`).
- M5 contains no model-fitting path; it evaluates only released bars. The scored/trained
  count is therefore a constant zero backed by architecture, not a dynamic model audit.

No protected row reaches feature generation, strategy evaluation, portfolio simulation,
ranking, model fitting or an M5 cache. However, protected content affects publication and
content validation. A malformed protected row, duplicate date, chronology reversal,
out-of-range date or OHLCV violation can fail/quarantine the whole segment and alter
whether pre-holdout bars are released. Full-segment hash, record count, disposition and
the protected-row count enter decision/run summaries. Error messages can also disclose
protected dates or validation facts. This is validation-level holdout influence, even
though it is not scoring leakage.

The 960 count was verified from the existing immutable run and source code. The audit did
not decode additional protected rows merely to reproduce it.

**Coverage.** One H test verifies positive decoded and zero released/scored counts. It
does not inject a failure only after the cutoff and prove the development run unchanged.

**Required correction.** Separate byte-integrity verification from protected content
validation, or use segment boundaries that end before the holdout. Add a metamorphic test
showing that changes confined to protected content cannot affect development features,
signals, metrics, eligibility or logs except a segregated protected-integrity alarm.

## 4. Independent test results

The supplied command uses the primary worktree's `.venv` and explicitly imports source
from another checkout. That can be valid, but it is vulnerable to accidental path order,
current-directory modules and dependency drift.

This audit exported exact Git trees with `git archive` into separate temporary
directories and ran with:

```text
PYTHONPATH=<0a25f86-export>/src \
  /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest \
  <0a25f86-export>/tests -ra
```

Import inspection showed `stock_gplm.__file__` under the exported tree. Results:

| Revision | Collected | Result | Runtime |
| --- | ---: | --- | ---: |
| `a9d0c57` | 668 | collection verified | 1.09 s collection |
| `0a25f86` | 736 | **736 passed** | 39.09 s |
| M5.1 file only | 68 | collection verified | 0.05 s collection |

Environment: CPython 3.12.16, pytest 8.4.2, pluggy 1.7.0. No skip, xfail or
deselection summary appeared. M5.1 adds 68 collected cases; its file has 33 test
functions expanded by parameterization. The full suite includes M4, M5 and M5.1 tests.
Dependencies did not change from M5. The exported lock hashes were:

- `pyproject.toml`: `39f3cd6519f5929b159184e750bd2e74fa062ef85e2fe81be60cdd831b28addf`
- `requirements-dev.lock`: `912081a0c76469b883eac802e68e82d7af4f0d08fa2f07130c71a360a73df95c`
- `requirements-m35.lock`: `748f97fd952521ce956456e87b3a95dd1c43d58d27342adc3374915aefcb5106`

### Reproduction scripts

The scripts were found only under `/tmp/claude-1000/.../scratchpad/repro/`, not in the
commit or durable evidence store. They are useful demonstrations but not permanent audit
evidence. The pre- and post-fix outputs record different script hashes
(`be83c38a...` versus `eab2fe40...`), so “the same script” was not byte-identical between
runs. The script checks only A-G. Its E and G checks search source text rather than verify
behavior, and its D attack lacks a manifest; the committed D tests are materially stronger
because they cover self-consistent/rehashed artifacts and replay. The before/after outputs
do demonstrate the original simple exploits, but they do not establish closure of the
residual issues found here.

## 5. Configurable strategy architecture capability matrix

The existing architecture should be extended, not replaced.

| Capability | Status | Evidence and limit |
| --- | --- | --- |
| 1. Declarative JSON/YAML strategies | **IMPLEMENTED** | Bounded JSON and a deliberately small, safe YAML subset (`v1/codec.py:23-62`); Studio editor and CLI use the same validator. |
| 2. Versioned strategy schemas | **IMPLEMENTED** | `schema_version == 1`, canonical JSON/config hash, immutable SQLite versions (`v1/schema.py:219-305`, `v1/registry.py:41-90,105-143`). |
| 3. Composable indicators/conditions | **IMPLEMENTED** | Allowlisted indicators plus nested logical, comparison, arithmetic and rolling expression trees (`v1/schema.py:17-30,102-205`; `v1/evaluator.py:104-206`). |
| 4. Extensible custom features | **MISSING** | Only six hardcoded indicator types are accepted; unknown types fail. There is no versioned plugin/custom-feature interface. This is a bounded-safety choice, not a defect requiring replacement. |
| 5. Optional ML models | **MISSING** | V1/M5 has no model node or artifact interface. M3 LightGBM research is separate and must remain gated. |
| 6. Multiple simultaneous strategies | **PARTIAL** | Registry stores many immutable strategies and activations by ID, but one M5 run evaluates one `StrategyVersion`; there is no simultaneous capital-allocation engine across strategies. |
| 7. Configurable entry/exit/sizing/risk | **PARTIAL** | Entry/exit trees are rich; max holding, percent stop/target, fixed-fraction sizing and costs are configurable. Risk types, sizing types, timeframe and instrument type are intentionally limited (`v1/schema.py:27-30,208-216,269-299`). |
| 8. Shared semantics for backtest/scanner/paper | **PARTIAL** | V1 backtest, M5 and paper decisions share `evaluate_strategy`; M5 portfolio uses shared execution rules. No scanner exists, and paper intent does not share portfolio allocation/order lifecycle (`v1/paper.py:23-68`). |
| 9. Auditability and provenance | **IMPLEMENTED** | Canonical config hashes, immutable versions, recorded execution/data hashes, M5 run manifests and replay. Evidence authority limitations remain as described in E. |
| 10. Validation without arbitrary code | **IMPLEMENTED** | Exact fields, allowlists, finite numbers, bounded tree complexity, no positive offsets, safe JSON/YAML; no `eval`, dynamic import or user code execution. |

The core engine is configuration-driven. The two example strategies are configurations,
not engine branches. There is no demonstrated reason to replace it.

## 6. Data and research integrity findings

### Existing real-data evidence

Existing reports, without re-downloading or altering raw data, support:

- Current file count under `data/`: **31,704**.
- Existing M5.1 discovery artifact: **2,701 symbols, 10,355 complete segments,
  10,346 ACCEPT, 9 QUARANTINE_CONTENT, 0 incomplete**. Its per-symbol list has 2,701
  entries.
- Existing strict 20-symbol result: all 20 `BLOCKED`; no run directory produced.
- Existing M5.1 diagnostic run: 20 symbols, 960 protected rows decoded, zero released or
  scored; both copies have the same run ID and load as `INTEGRITY_VERIFIED`.

The claim that all 31,704 files were unchanged before/after cites digest
`51398f56...3edd6`, but no before-and-after hash manifest was located in durable reports.
The present count is verified; historical byte identity is **unverified from retained
evidence**. `/tmp` output is not accepted as permanent proof.

The four M5.0 runs' manifest hashes verify, but M5.1 correctly labels them
`LEGACY_UNVERIFIED`. Diagnostic artifacts cannot enter M5.1 ranking: they are non-strict,
non-investable and replay cannot change that classification. Scanner output does not
exist; paper eligibility is fixed false in M5 run records and verified by the loader.

### M3.8 scientific evidence

The M3.8 manifest hashes for `decomposition.json`, `ohlcv-audit.json`,
`quarantine-events.csv` and `replay-check.json` match current bytes. The source evidence
supports 65 probable unadjusted corporate actions (44 in eligible equities), 13 possible
actions and 260 unexplained large moves. Flagged label windows have extreme negative
returns and enter model top deciles; the report correctly treats this as contamination,
not genuine negative selection.

The recorded LightGBM top-decile results distinguish:

- gross excess;
- a full 0.4932% round-trip charge every period; and
- cost multiplied by actual turnover.

For 1-7d, gross excess +0.33% becomes -0.16% under full turnover and +0.07% under
53% actual turnover. The last figure is not statistically established and is not a
profitability claim. Longer horizons do not show a robust investable top-tail edge.
Different M3 and M5 cost conventions must not be compared as if identical.

### M5 strategy evidence

Legacy run hashes support 40 swing trades and 234 mean-reversion trades. Exit records
support 3 plus 15 gap-through-stop exits: **18 of 274**. Gross P&L is approximately
-10,998 and +39,859; net realized P&L approximately -32,386 and -19,630. Mean reversion's
recorded turnover is 22.38 versus swing's 8.15. These are diagnostic results on a
survivorship-biased, small fixed universe with unverified mixed adjustment basis and
assumed costs. They are not performance evidence and must not drive optimization.

M5.1 changes terminal headline return by estimating exit costs on the one remaining open
position. Comparisons must use the v2 terminal convention consistently; old v1 headline
returns are not directly rankable.

Historical survivorship remains unresolved: the current Shoonya master proves current
identity only, and the dataset omits delisted names. No performance claim is suitable for
promotion until point-in-time membership and missing-history accounting exist.

## 7. Integration compatibility assessment

### Compatibility

- **Code/ancestry:** M5.1 is one commit directly on M5; M5 is already on M4/V1/M3.9-B.
  No cherry-pick ordering problem exists on that line.
- **Dependencies:** unchanged.
- **Run schema:** M5 run/manifest move from v1 to v2. `load_run` retains read-only legacy
  support but legacy runs are never ranked. New v2 runs add `environment.json`, requested
  universe, evidence versions, terminal valuation and three holdout counters.
- **Registry:** no SQL migration in M5.1. Existing immutable V1 strategy versions remain
  usable. Replay requires the exact registered config hash.
- **CLI/API:** backtests now operate on complete universes; strict/default diagnostic
  partial execution becomes exit 2 / HTTP 422. Compare ranking now needs evidence and
  registry replay. This is a deliberate breaking safety change and should be noted in
  release notes.
- **Adapter:** v2 decisions add evidence, price-basis, corporate-action and issuance
  fields. Persisted decision objects are not portable authorization; replay reassesses.
- **Evidence:** real `evidence-paths` v1 remains loadable. It contains only M3.2 holdout,
  ETF heuristic and current master, so strict remains blocked.

### Smallest safe integration sequence

1. Preserve and separately resolve the dirty M3.4 worktree; do not use it as an
   integration target.
2. Add a focused M5.1a corrective commit on top of `0a25f86` that closes the acceptance
   criteria below. Keep strict classification disabled until it lands.
3. Re-run the exact exported-tree suite and the new adversarial tests. Require no skips,
   no dependency drift and no writes under `data/` or existing report roots.
4. Review the complete linear range from `main` through M5.1a. Because `main` is far
   behind, integrating only `0a25f86` onto `main` is not meaningful; either fast-forward/
   merge the reviewed complete line or first integrate its ancestors in order. Do not
   cherry-pick M5.1 without M5/M4/V1/M3.9-B.
5. Tag/record the pre-integration SHA. Merge only after a clean-tree check and staged-index
   audit. Do not migrate or rewrite existing legacy run directories.

Rollback is a branch-pointer/revert operation before any new v2 artifacts are relied on.
Existing immutable v1 reports require no rollback migration. Any v2 reports created after
integration should remain immutable and be marked superseded, not deleted or rewritten.

## 8. Remaining strict-research blockers

| Blocker | Required evidence | Current repository | Why blocked / safe failure | Approved path and future work |
| --- | --- | --- | --- | --- |
| Official trading calendar | Browser-downloaded NSE schedule/holiday/special-session documents, immutable bytes, SHA-256 manifest, complete daily transcription and reviewer approval | No registered real calendar; M3.6 source store empty | Adapter emits `CALENDAR_EVIDENCE_MISSING`; no weekend/holiday inference | Existing M2.5-R loader can be used after manual browser acquisition, but must verify referenced document/record bytes, not only embedded hashes. |
| Point-in-time listing history | Listing, delisting/suspension and membership-through evidence for every symbol/date | Current Shoonya master only | `PIT_MEMBERSHIP_MISSING`; current identity cannot establish historical universe | Manually acquired approved NSE files plus a complete versioned adapter; include dead/delisted names and count missing prices. |
| Symbol aliases | Complete symbol-change intervals with canonical identity/ISIN where available | None | `ALIAS_EVIDENCE_MISSING`; no stitching is inferred | M2.1 mapping contract can import a hash-bound payload; validate source authority and complete range. |
| Complete corporate actions | Complete catalog plus independently licensed/validated M3.9-B events with ex-date, type and ratios | No real catalog/event bundle | `CORPORATE_ACTION_COVERAGE_MISSING`; UNKNOWN stays unknown | Manual approved source bundle, reviewed licence decision, immutable source document and explicit unsupported-action handling. |
| Provider price basis | Provider statement or reviewed evidence proving RAW_UNADJUSTED/back-adjusted/mixed by symbol and range | None; observed Shoonya series appears mixed | `PROVIDER_PRICE_BASIS_UNVERIFIED`; no events applied | New reviewed contract must verify the underlying document and scope. Mixed basis may require a different licensed raw source. |
| Authoritative ETF classification | Exchange equity and ETF lists covering the universe/date | M3.7 naming heuristic only | **Not currently a strict failure.** Strict synthetic probe passed with evidence status `HEURISTIC` | Add an evidence failure unless authoritative classification covers every member/date. Keep all uncertain instruments out. |
| Verified transaction-cost schedule | Versioned brokerage, exchange, tax/fee schedule plus slippage/liquidity policy effective for the evaluation dates | User-supplied bps and free-text basis | **Not currently a strict failure.** Strict probe passed with all-zero costs and `caller assertion only` | Add checksum-bound cost-profile registration, effective dates and nonzero/zero justification; bind profile ID into the run and comparability rules. |

No existing approved source closes these gaps today. NSE data acquisition must remain
manual browser download under the established terms; no automated NSE collection is
authorized. Until evidence is registered, the safe state is `BLOCKED`/`UNKNOWN`, never a
heuristic substitution.

## 9. Prioritized corrective actions

1. **P0 — strict authority gate:** add explicit authoritative ETF and verified cost-
   schedule failures; prohibit synthetic/unapproved evidence from production strict mode.
2. **P0 — strict orchestration/freshness:** replace caller-held sealed objects as the
   authorization mechanism with one re-assess-and-run operation, and re-open/hash all raw
   and evidence inputs immediately before strict publication. Remove the directly callable
   signer and narrow the documented threat model.
3. **P0 — holdout validation isolation:** ensure protected content cannot affect
   development eligibility, logs or summaries; add protected-only mutation tests.
4. **P1 — evidence authority:** make calendar, listing, alias, action, price-basis and cost
   bundles verify actual referenced source bytes, acquisition method, licence decision and
   reviewer approval. Introduce an explicit synthetic-only trust domain.
5. **P1 — local API authentication:** per-launch unpredictable token, CSRF binding,
   owner-only filesystem permissions and proxy-exposure documentation.
6. **P1 — provenance completeness:** record hashes for payload, `.done` and metadata
   sidecars and require expected lockfiles. Preserve Python micro/platform in environment;
   require replay before any comparison.
7. **P2 — durable audit evidence:** move sanctioned reproduction scripts and outputs from
   `/tmp` into an immutable, checksum-bound audit-evidence location; record one script
   hash for before/after runs.
8. **P2 — architecture gaps:** design versioned custom-feature and optional-model
   interfaces only when needed; preserve safe allowlists and do not introduce arbitrary
   code execution. A scanner/shared multi-strategy allocator is a separate milestone.

## 10. Release acceptance criteria and next milestone

### Smallest safe next milestone: M5.1a strict authorization closure

M5.1a is complete only when all of the following are measurable and pass:

1. A caller cannot turn a constructed decision into strict authorization by importing any
   package symbol; a synthetic adversarial test attempts the current `_seal` path and
   fails.
2. Moving, deleting or changing any raw payload, marker, metadata sidecar or evidence file
   after assessment causes strict build/publish to fail before an investable run object is
   returned.
3. Strict assessment with `HEURISTIC` instrument classification is `BLOCKED`.
4. Strict assessment with an unregistered/free-text/zero cost profile is `BLOCKED`; a
   zero-cost profile is allowed only when authoritative evidence explicitly establishes
   zero for that component.
5. Evidence tagged synthetic, fixture, test or unapproved can produce only diagnostic,
   never strict/investable, output.
6. A mutation confined to protected rows cannot change development bars, features,
   signals, metrics or ordinary logs. If byte integrity must alarm, it is segregated and
   does not reveal protected content or silently alter development research.
7. Loopback API state changes require a high-entropy per-launch token; live tests show
   unauthenticated local and proxy-originated requests receive 403.
8. New adversarial tests are added to the existing 736 and the full Python 3.12 suite
   passes with 0 failures/skips/deselections from an exact exported tree.
9. Existing real evidence remains blocked with the seven explicit blocker categories;
   no gate is relaxed and no original holdout is decoded or scored for acceptance.
10. Raw-data and existing-report tree hashes are captured before and after in a retained,
    checksum-bound manifest and are identical.

After M5.1a, code integration may become GO as an engineering baseline. Strict real-data
research remains independently blocked until actual approved evidence closes every gate.
Live/paper execution requires a later, separately authorized milestone and must not be
inferred from backtest readiness.
