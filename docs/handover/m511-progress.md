# M5.1.1 remediation progress

Worktree: `/home/mgurunath/projects/stock-gplm-m45`
Branch: `fix/m511-strict-authorization` (created from `feature/m51-integrity-hardening` @ `0a25f86`)
HEAD: see `git log -1` (authority-gates commit)
Audit: `docs/reviews/m51-independent-audit.md`
Handover: `docs/handover/m511-to-codex.md`

Test command (the worktree has no .venv; PYTHONPATH is required or the main checkout is imported):

    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider

## Log

| Step | Status | Commit | Files | Tests |
| --- | --- | --- | --- | --- |
| Baseline | — | `0a25f86` | — | 734 passed, 2 skipped |
| A.1 remove importable `_seal` signer | FIXED | `667ab61` | `m5/issuance.py`, `m5/adapter.py`, `m5/evidence.py`, `tests/test_m51_integrity.py`, `docs/m51-integrity-hardening.md`, `docs/reviews/m51-independent-audit.md` | 736 passed, 2 skipped, 0 failed |
| A.3/A.4 strict authority gates (synthetic, unapproved, heuristic ETF, unregistered costs) | FIXED (blocking gates) | authority-gates commit | `m5/adapter.py`, `m5/models.py`, `tests/m5_fixtures.py`, `tests/test_m511_authority.py` (new), `tests/test_m51_integrity.py`, `tests/test_m5_historical_adapter.py`, `docs/m51-integrity-hardening.md`, handover docs | 745 passed, 2 skipped, 0 failed |

## Remaining (priority order; designs in the handover §6)

1. A.2 freshness: re-assess sources before strict build (P0)
2. H holdout validation isolation (P0)
3. C evidence authority bundle; also the positive path for the authority gates (P1)
4. F per-launch API token (P1)
5. E provenance completeness (P1)
6. Positive paths: registered cost schedule + authoritative classification loaders
7. Closing: before/after tree-hash manifest, docs matrix

## Known blockers

No code blockers. Strict real-data research remains blocked by missing evidence (audit §8).
No incomplete or failing changes are in the working tree.
