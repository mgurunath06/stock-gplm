# M5.1.3 code acceptance — 2026-11-14

- Branch: `fix/m513-acceptance-remediation`
- Accepted commit: `102198a`
- Previously audited baseline: `f29af79`
- Closing commit: the commit containing this document

This record only closes M5.1.3 code acceptance. It merges nothing, deploys nothing
and changes no security control, holdout rule, run contract or migration
specification.

## Verdict

**M5.1.3 code acceptance: GO.** This is the verdict of an independent Claude Code
Web review of the diff from `f29af79` to `102198a`.

The review covered only the two-file closure diff:

- `tests/test_m51_integrity.py`: the stdlib-only dependency probe now ignores
  modules loaded at interpreter startup, still reports genuine third-party
  imports, and asserts which source tree it imported; 8 synthetic negative tests
  were added.
- `docs/m513-conditional-acceptance-closure.md`: the closure evidence.

No production source changed between `f29af79` and `102198a`.

| Component | Status |
| --- | --- |
| M5.1.3 code acceptance | **GO** |
| Holdout isolation (H) | Code PASS |
| Evidence authority (C) | Code PASS |
| Run contract v4 | PASS |
| 8 additional negative tests | PASS |
| Full suite, developer run (CPython 3.12.16) | 878 passed, 2 skipped, 0 failed |
| Strict real-data research | **BLOCKED** |
| Paper trading | **BLOCKED** |
| Live trading | **BLOCKED** |
| Protected holdout | **LOCKED** |

Reviewer evidence:

- The reviewer reproduced the startup-import checks on its own, under Python
  3.12.3.
- It did not rerun all 880 tests, and recorded that this does not block code
  acceptance.
- The full-suite figure above is the developer run from
  `docs/m513-conditional-acceptance-closure.md`.

## Evidence status of the audit verdict

The GO verdict is **user-supplied evidence**. The user relayed it on 2026-11-14 as
"M5.1.3 CODE ACCEPTANCE: GO, accepted commit `102198a`". The original audit report
is not in this repository or on this host, so its text, its reviewer identity and
the environment it describes couldn't be checked here.
`docs/reviews/m51-independent-audit.md` is the earlier M5.1 audit, not this one.
Keep the original report next to this record if it becomes available.

Checked locally on 2026-11-14:

- `102198a` is an ancestor of the closing commit, and its direct parent.
- `main` is an ancestor of `102198a`.
- Only this document changed between `102198a` and the closing commit.

## Code acceptance is not operational authorization

GO means the code at `102198a` meets the M5.1.3 audit conditions. It authorizes
none of the following, each of which needs its own explicit approval:

- running strict research on real data;
- installing a trust root;
- migrating protected partitions;
- scoring or reading the protected holdout;
- paper or live trading;
- merging, pushing or deploying;
- starting M5.2 or M6.

## Conditions that remain open

Code acceptance doesn't make the system ready for operational research. These
conditions are still open, and strict research stays blocked until each one is
met:

1. **Operational trust root.** No administrative trust root is installed, so every
   assessment is rejected with `HOLDOUT_BOUNDARY_UNVERIFIED`.
2. **Real-data evidence.** No genuine, registered market-data evidence has been
   accepted for strict use.
3. **Genuine-fixture tests.** Two tests are still skipped because their real data
   isn't present locally:
   - `tests/test_m22_master_validation.py:95`: Shoonya NSE instrument master;
   - `tests/test_m2_quality.py:272`: accepted RELIANCE segment.
4. **Protected-partition migration.** The migration has been specified but not
   carried out.

## Next

M5.2 (Data & Training Readiness) needs its own dated scope rule in `AGENTS.md`
before it reads any real raw market data.
