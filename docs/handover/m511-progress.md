# M5.1.1 remediation progress

Worktree: `/home/mgurunath/projects/stock-gplm-m45`
Branch: `fix/m511-strict-authorization` (created from `feature/m51-integrity-hardening` @ `0a25f86`)
HEAD: `667ab61`
Audit: `docs/reviews/m51-independent-audit.md`
Handover: `docs/handover/m511-to-codex.md`

Test command (the worktree has no .venv; PYTHONPATH is required or the main checkout is imported):

    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider

## Log

| Step | Status | Commit | Files | Tests |
| --- | --- | --- | --- | --- |
| Baseline | — | `0a25f86` | — | 734 passed, 2 skipped |
| A.1 remove importable `_seal` signer | FIXED | `667ab61` | `m5/issuance.py`, `m5/adapter.py`, `m5/evidence.py`, `tests/test_m51_integrity.py`, `docs/m51-integrity-hardening.md`, `docs/reviews/m51-independent-audit.md` | 736 passed, 2 skipped, 0 failed |

## Remaining (priority order; designs in the handover §6)

1. A.3/A.4 strict authority gates: heuristic ETF, unregistered costs, synthetic evidence (P0)
2. A.2 freshness: re-assess sources before strict build (P0)
3. H holdout validation isolation (P0)
4. C evidence authority bundle (P1)
5. F per-launch API token (P1)
6. E provenance completeness (P1)
7. Closing: before/after tree-hash manifest, docs matrix

## Known blockers

No code blockers. Strict real-data research remains blocked by missing evidence (audit §8).
No incomplete or failing changes are in the working tree.
