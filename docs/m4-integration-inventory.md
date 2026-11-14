# M4.1 integration inventory — 2026-11-14

This is a read-only inventory, taken before any M4 change. It records the actual
repository state, which differed from the state the M4/M5 mandate described.

## Worktrees and branches at inventory time

| Path | Branch | HEAD | State |
| --- | --- | --- | --- |
| `/home/mgurunath/projects/stock-gplm` | `feature/m39b-corporate-actions` | `35bdc06` | dirty: V1 uncommitted (4 modified, 4 untracked paths) |
| `/home/mgurunath/projects/stock-gplm-m25r` | `feature/m25r-cloud-recovery` | `dbba2b1` | clean |
| `/home/mgurunath/projects/stock-gplm-m25` | `fix/m25-auth-safety` | `fb050d9` | clean, tracks `origin/fix/m25-auth-safety` |
| `/home/mgurunath/projects/stock-gplm-m25-browser` | `fix/m25-browser-diagnostics` | `cb31f9a` | untracked `transfer/` (not touched) |
| `/home/mgurunath/projects/stock-gplm-m3` | `feature/m3-data-readiness` | `0307786` | clean |
| `/home/mgurunath/projects/stock-gplm-m34` | `feature/m34-minute-fusion` | `0307786` | dirty: pre-existing M3.4 work (not touched) |

Other local branches: `main` (`1158925`, equals `origin/main`), `feature/m35-daily-training`
(`873d086`), `feature/m36-reference-evidence` (`84f0727`),
`feature/m37-exploratory-biased-training` (`9f97431`), `feature/m38-signal-discovery`
(`aea2fcb`), `feature/m39-nse-mcp` (`df8324b`), `fix/m24-discovery-enumeration` (`8fe69a8`).

## Commit references checked against the mandate

| Mandate reference | Actual finding |
| --- | --- |
| M2.5-R commit `2f43d75…` on `feature/m25r-cloud-recovery` | Exists. The branch has one more commit, `dbba2b1` (documentation sync). |
| M3.9-B commit `35bdc06…` on `feature/m39b-corporate-actions` | Exists and is the branch HEAD. |
| M3.9-A baseline `df8324b` | Exists (`feature/m39-nse-mcp`) and is the direct parent of `35bdc06`. |
| "Primary workspace on M3.8" (session snapshot) | Incorrect: the primary worktree was on `feature/m39b-corporate-actions`. |

Ancestry: the M3 line is linear
`main 1158925 → … → m38 aea2fcb → m39 df8324b → m39b 35bdc06 → (V1, uncommitted)`.
M2.5-R branches from `main` (`merge-base = 1158925`) through the M2.5 browser line
and shares no commits with M3.x. It differs from `main` by 27 files and about
4.7k lines, mostly `stock_gplm.m25` browser/OAuth code plus the new `stock_gplm.m25r`.

## V1 uncommitted file inventory

Modified: `README.md`, `docs/README.md`, `docs/architecture.md`, `pyproject.toml`
(two console scripts). Untracked: `config/v1/strategies/{swing-breakout-v1,mean-reversion-v1}.json`,
`docs/v1-strategy-engine.md`, `src/stock_gplm/v1/` (9 modules plus ignored `__pycache__`),
`tests/test_v1_strategy_engine.py`. The tracked diffs touch only documentation
index lines and script entries; no completed milestone's code was modified.

## Test results reproduced

| Tree | Command | Result |
| --- | --- | --- |
| Primary (`35bdc06` + uncommitted V1) | `.venv/bin/python -m pytest` | 618 passed |
| Primary, V1 focused | `.venv/bin/python -m pytest tests/test_v1_strategy_engine.py` | 19 passed |
| Clean `35bdc06` (detached temporary worktree, no `data/`) | `PYTHONPATH=src .venv/bin/python -m pytest` | 597 passed, 2 skipped |

The two skips are real-data regressions that resolve `data/…` relative to the
working directory. In the primary checkout they run and pass, so 618 = 597 + 2 + 19.

## Data paths and immutable boundaries

| Path | Content | Policy |
| --- | --- | --- |
| `data/raw/shoonya/NSE/<SYMBOL>-EQ/` | 2,701 symbol directories; 10,355 `.json.gz` + 10,355 `.meta.json` + 10,355 `.json.gz.done` triplets | immutable, read-only |
| `data/raw/shoonya/NSE/.stock-gplm-m24.lock` | M2.4 publication lock file (not held at inventory) | read-only probe only |
| `data/raw/shoonya-minute/NSE/` | RELIANCE-EQ, TCS-EQ minute data | out of M4/M5 scope |
| `data/reference/source/shoonya/NSE_symbols.txt.zip` | current Shoonya master (M2.2 accepted) | immutable |
| `reports/m3…m39_nse_mcp`, `reports/m35/trial-ledger.jsonl` | prior research outputs and the trial ledger | immutable |

A SHA-256 baseline of every file under `data/` and `reports/` (31,928 files) was
taken before any change, and is re-verified at each acceptance step.

## Module relationships relevant to M4/M5

- `stock_gplm.ingestion.daily.validate_daily` is the established per-segment
  publication and `ShoonyaDailyV1` content contract (`.done` marker, SHA-256,
  byte and record counts, chronology, duplicates, OHLC bounds).
- `stock_gplm.m2.reference_data` defines the manifest-verified M2.1 contracts for
  listing history, historical symbol mappings and corporate-action catalogs.
- `stock_gplm.m3` readiness runs bind every raw input hash; the M3.2 run
  `26e41d00…` holds the hash-bound split plan with the locked holdouts
  (`2026-04-29` for horizons up to 6 months, `2025-10-21` for 6–12 months).
- `stock_gplm.m39b` is a dependency-free corporate-action calculator with
  point-in-time `knowledge_at` enforcement. It has no file loader.
- `stock_gplm.v1` imports nothing from M1–M3.9; it is a self-contained boundary.
- `stock_gplm.m25r.calendar` (M2.5-R worktree only) defines the
  `stock-gplm.m25r-registered-calendar.v1` contract: a self-verifying registration
  document with a canonical-hash ID and an explicit status for every date.

## Reference evidence actually present

The M3.6 bundle `f2d17cff…` records `"sources": []`. No registered calendar,
listing history, symbol-change list, ETF/equity list or corporate-action file
exists. `config/m2/nse-trading-calendar.json` is empty (`coverage_complete: false`),
and `config/m2/instrument-inventory.json` holds only RELIANCE-EQ with null listing
fields. Every M3.6 component is `BLOCKED`.

## V1 review findings (before remediation)

| ID | Area | Finding | Severity |
| --- | --- | --- | --- |
| D1 | Next-open semantics | `EXIT_CONDITION` filled at the same close that produced the decision, so the signal and its fill used one price. Entries were next-open, so the two were inconsistent. | High (look-ahead in exits) |
| D2 | Stop handling | A gap below the stop filled at the stop price, not the worse open, which understated losses. | High (optimistic bias) |
| D3 | Risk levels | Stop/target levels were computed from the cost-inflated entry price, not the observed fill. | Low |
| D4 | Holding period | `MAX_HOLDING` exits filled at the deciding close (same issue as D1). | Medium |
| D5 | Eligibility | `data_quality_eligible` was a caller-supplied boolean (a Studio checkbox). It is a UI claim, not provenance. Results carried no data classification. | High for real data; acceptable only for synthetic |
| D6 | Registry | Version allocation used `SELECT MAX+1` and then `INSERT` without a write lock. Concurrent saves raised `IntegrityError` (reproduced: 7 of 8 threads failed). | Medium |
| D7 | Registry | Immutability was a convention only. `UPDATE`/`DELETE` were possible through SQLite. | Medium |
| D8 | Registry/Studio | Re-recording an identical backtest raised an unhandled `IntegrityError`, which reached the HTTP handler as a dropped connection. | Low |
| D9 | Studio | No `Host`/`Origin` validation. A DNS-rebinding page could reach the loopback API. | Medium |
| D10 | Portfolio | The backtest is a per-trade compounding approximation. It has no cash, exposure, equity curve or drawdown. | Scope gap (addressed by M5 simulator) |
| D11 | Studio UI | `renderStructured` inserts unvalidated, user-typed indicator IDs into `innerHTML` (self-XSS only). `/api/export` ignores the selected output format. | Low (recorded, not remediated) |

Confirmed sound: the schema allowlists, rejects unknown keys and bounds depth,
node count, lookback and size. Positive offsets are rejected. Indicators are
trailing-only. `NOT`/`AND`/`OR` propagate unavailable values as unknown. The YAML
subset rejects tags, anchors, aliases, merge keys and block scalars. JSON rejects
duplicate keys and NaN/Infinity. No `eval`/`exec`/pickle is used. Configuration
hashes are canonical. Paper intents are constrained to `SIMULATED_NOT_ROUTABLE`
by a CHECK constraint, and no broker client exists.

## Overlap analysis

- V1 vs M3.9-B: no file overlap. V1 only appends to `README.md`, `docs/README.md`,
  `docs/architecture.md` and `pyproject.toml`, after the M3.9-B lines.
- V1/M3.9 vs M2.5-R: M2.5-R rewrites `README.md`, `docs/README.md`,
  `docs/architecture.md`, `docs/m21-reference-data.md`, `docs/m24-secure-sync.md`,
  `pyproject.toml` and `src/stock_gplm/m25/*` from an older base. A merge would
  conflict in those documentation files and would import the browser/OAuth stack
  into the research line.

## Integration risks

1. Merging M2.5-R would bring browser/OAuth/deployment code into the research
   engine and conflict in shared docs. It is not required for M5.
2. Changing V1 execution semantics changes historical V1 result hashes. Mitigated:
   the policy string is versioned (`…_v2`), and stored rows are immutable.
3. Without any registered reference evidence, strict real-data eligibility cannot
   pass. M5 must fail closed, not fabricate evidence.

## Recommended integration sequence

1. Commit V1 exactly as found on `feature/v1-configurable-strategy-engine`
   (parent `35bdc06`; leaves `feature/m39b-corporate-actions` untouched).
2. Create the isolated worktree `/home/mgurunath/projects/stock-gplm-m45` on
   `feature/m4-v1-consolidation` from that commit. M3.9-B is already an ancestor,
   so no cherry-pick or merge is needed.
3. Remediate D1–D9 with regression tests that fail on the original V1.
4. Branch `feature/m5-historical-adapter` from the M4 commit for M5. Consume the
   M2.5-R calendar contract by format only. Do not merge M2.5-R.
