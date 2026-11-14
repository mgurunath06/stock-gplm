# M5.1.1 remediation progress

Worktree: `/home/mgurunath/projects/stock-gplm-m45`
Branch: `fix/m511-strict-authorization` (created from `feature/m51-integrity-hardening` @ `0a25f86`)
HEAD: see `git log -1` (docs commit after `621e39e`)
Audit: `docs/reviews/m51-independent-audit.md`
Handover: `docs/handover/m511-to-codex.md`

Test command (the worktree has no .venv; PYTHONPATH is required or the main checkout is imported):

    PYTHONPATH=src /home/mgurunath/projects/stock-gplm/.venv/bin/python -m pytest -q -p no:cacheprovider

## Log

| Step | Status | Commit | Files | Tests |
| --- | --- | --- | --- | --- |
| Baseline | — | `0a25f86` | — | 734 passed, 2 skipped |
| A.1 remove importable `_seal` signer | FIXED | `667ab61` | `m5/issuance.py`, `m5/adapter.py`, `m5/evidence.py`, `tests/test_m51_integrity.py`, `docs/m51-integrity-hardening.md`, `docs/reviews/m51-independent-audit.md` | 736 passed, 2 skipped, 0 failed |
| A.3/A.4 strict authority gates (synthetic, unapproved, heuristic ETF, unregistered costs) | FIXED (blocking gates) | `2cd419a` | `m5/adapter.py`, `m5/models.py`, `tests/m5_fixtures.py`, `tests/test_m511_authority.py` (new), `tests/test_m51_integrity.py`, `tests/test_m5_historical_adapter.py`, `docs/m51-integrity-hardening.md`, handover docs | 745 passed, 2 skipped, 0 failed |
| A.2 assessment freshness (payload/.done/meta/evidence revalidation, atomic) | FIXED | `621e39e` | `m5/adapter.py`, `m5/models.py`, `m5/evidence.py`, `m5/runner.py`, `m5/__init__.py`, `tests/test_m511_freshness.py` (new), docs | 758 passed, 2 skipped, 0 failed, 0 deselected |
| H holdout validation isolation | FIXED | `ca3bd4e` | `m5/holdout_validation.py` (new), `m5/adapter.py`, `m5/runner.py`, `tests/test_m51_integrity.py`, `tests/test_m5_historical_adapter.py`, docs | 759 passed, 2 skipped, 0 failed, 0 deselected |
| C evidence authority + classification/cost registration framework | FIXED IN CODE (authoritative data unavailable) | `5578057` | `m5/authority.py` (new), `m5/evidence.py`, `m5/adapter.py`, `m5/models.py`, `m5/runner.py`, tests, docs | 764 passed, 2 skipped, 0 failed, 0 deselected |
| F Studio per-launch authentication and private files | FIXED | `3483107` | `v1/studio.py`, `m5/runner.py`, `tests/test_m4_v1_remediation.py`, `tests/test_m51_integrity.py`, `tests/test_m5_studio.py`, docs | 765 passed, 2 skipped, 0 failed, 0 deselected |
| E complete input/environment provenance | FIXED (pending commit) | — | `m5/provenance.py`, `m5/runner.py`, `m5/run_store.py`, `tests/test_m51_integrity.py`, docs | 767 passed, 2 skipped, 0 failed, 0 deselected |

## Remaining (priority order; designs in the handover §6)

1. Closing: before/after tree-hash manifest, docs matrix

## Known blockers

No code blockers. Strict real-data research remains blocked by missing evidence (audit §8).
No incomplete or failing changes are in the working tree.
