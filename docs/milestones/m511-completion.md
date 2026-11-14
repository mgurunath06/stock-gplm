# M5.1.1 integrity remediation completion — 2026-11-14

Branch: `fix/m511-strict-authorization`

Starting checkpoint: `7902f1c`

Closing commit: the commit containing this document

This milestone is local research hardening only. It performed no network access,
credential access, broker operation, Oracle change, deployment, raw-data write,
prior-result rewrite or protected-holdout scoring.

## A–H status

| Finding | Implementation status | Evidence availability / residual limit |
| --- | --- | --- |
| A strict authorization | **FIXED** | Imported signer, stale sources, synthetic/unapproved authority, heuristic classification and unregistered costs fail closed. Deliberately hostile same-interpreter code can still inspect closures or patch Python and remains outside the stated boundary. |
| B atomic universe | **FIXED / retained PASS** | Strict and default diagnostic requests remain all-or-nothing; explicit diagnostic subsets remain non-investable. |
| C evidence and corporate actions | **FIXED IN CODE** | Registrations verify actual source bytes and dependencies; authority needs a separately hash-pinned trust policy. No genuine production approval bundle is available, so real strict research remains blocked. |
| D fabrication and ranking | **FIXED / retained PASS** | Verified loading and replay-gated ranking remain mandatory. Local writers of code, raw data and evidence remain outside a hash-only local threat boundary. |
| E provenance | **FIXED** | Run v3 binds payload, marker, metadata, bars, evidence, strategy, cost and code identities. Both lockfiles are required. Python micro/build/platform remain environmental and replay remains mandatory. Immutable v2 runs remain integrity-readable. |
| F Studio security | **FIXED** | Per-launch token, constant-time API authentication, authenticated bootstrap and private local files added. Same-user compromise or a proxy deliberately given the token remains a host-security risk. |
| G accounting | **FIXED / retained PASS** | Strategy and benchmark retain one liquidation convention; no new accounting change was required. |
| H holdout isolation | **FIXED** | Whole-source byte integrity is checked, while protected candle content is not decoded or used for development validation. Protected-only synthetic mutation leaves development research unchanged. Full-source hash changes still invalidate stale assessments. |

## Checkpoint commits

- `ca3bd4e` — isolate development validation from holdout content
- `5578057` — structured evidence authority, classification and cost schedules
- `3483107` — authenticate every Studio API request
- `a8de3df` — complete run input and environment provenance
- closing commit — CLI trust-policy wiring, retained closing evidence and final documentation

## Verification

The required command uses the target worktree source explicitly:

```text
PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -ra -p no:cacheprovider
```

Imported package:
`/home/mgurunath/projects/stock-gplm-m45/src/stock_gplm/__init__.py`.
Environment: CPython 3.12.16, pytest 8.4.2, pluggy 1.7.0. Final result:
**767 passed, 2 skipped, 0 failed, 0 deselected** in 41.30 seconds.

Final result is recorded in `docs/handover/m511-progress.md`. The imported source
was `/home/mgurunath/projects/stock-gplm-m45/src/stock_gplm/__init__.py` under
CPython 3.12.16. Two tests skip because the target worktree has neither a local
real Shoonya NSE instrument master nor a local accepted RELIANCE segment. Those
cannot be replaced with equivalent real-data fixtures without a person supplying
the immutable source files; synthetic coverage is not equivalent real-data
evidence.

The target worktree had no `data/` or `reports/` tree before or after the task.
The checksum-bound absence record is
`docs/evidence/m511-integrity-closing-2026-11-14.json`. No claim is made about an
external checkout's trees.

## Remaining authoritative-data blockers

Strict real-data research still lacks genuine approved evidence for the official
trading calendar, point-in-time listing/membership history, symbol aliases,
complete corporate actions, provider price basis, authoritative ETF/equity
classification and verified effective-dated transaction costs. The trust-policy
framework does not create those facts and no approval was fabricated.

## Readiness decisions

- **M5.1.1 code integration: CONDITIONAL GO.** The code and synthetic adversarial
  suite pass, but integration should retain strict mode disabled until an
  independent review and the two local real-fixture checks can run without skips.
- **Strict real-data research: BLOCKED.** Required authoritative evidence is
  unavailable.
- **Paper/live execution: BLOCKED.** M5 remains research-only and has no broker
  route; this milestone does not authorize paper or live execution.

The next milestone is an independent integration review of the linear branch and,
separately, manual acquisition/reviewer approval of source evidence under the new
trust policy. It is not M6, Strategy Studio expansion, ML discovery, scanning or
branch consolidation.
