# M5.1.3 conditional-acceptance closure

Status: the audit conditions are closed with test evidence. This report doesn't
certify operational research readiness. M5.2, M6, real-data migration and
research execution haven't started.

- Baseline: `fix/m513-acceptance-remediation` at `f29af79`, clean worktree.
- Closure commit: the next commit on this branch, which contains this report.

## 1. Conditions and outcomes

| # | Audit condition | Outcome |
| --- | --- | --- |
| 1 | Reproduce the full 872-test suite on Python 3.12 as a non-root Linux user | Reproduced on the baseline: 872 collected, **870 passed, 2 skipped, 0 failed** (uid 1000) |
| 2 | The real ownership-rejection test must execute, not skip | `test_C_user_owned_trust_root_is_rejected_when_root_ownership_is_required` **PASSED** as uid 1000 (its `skipif` applies only to uid 0) |
| 3 | Investigate the `sitecustomize` dependency-test failure | Root cause found (§2) and reproduced |
| 4 | Make the smallest safe correction | Test-only change in `tests/test_m51_integrity.py` (§3) |
| 5 | Still detect genuine third-party imports | Proven by negative tests and mutation (§4) |
| 6 | Run targeted negative tests | 8 new tests, all passing; 2 weaker fixes killed (§4) |
| 7 | Rerun the complete suite | 880 collected: **878 passed, 2 skipped, 0 failed** |
| 8 | Verify source provenance | All 89 `stock_gplm` modules load from the m45 tree (§5) |

## 2. Root cause

`test_E_execution_path_imports_only_the_standard_library` starts `python -I`,
imports the M5 execution path, and fails on any top-level module in
`sys.modules` that isn't in `sys.stdlib_module_names`.

`-I` ignores `PYTHONPATH` and user site-packages, but `site` still runs:

- it imports `sitecustomize` from the interpreter's own site-packages or stdlib
  directory;
- it runs `import` lines in `.pth` files.

Distribution Pythons ship these hooks: Debian/Ubuntu's apport `sitecustomize`,
and setuptools' `distutils-precedence.pth`. Their modules were reported as
`stock_gplm` dependencies, which is a false positive. The workspace interpreter
has no such hooks, which is why the suite passed here and failed for the auditor.

Reproduction on this host, with the probe unchanged from `f29af79`:

| Interpreter | Probe output |
| --- | --- |
| workspace `.venv` (CPython 3.12.16, no startup hooks) | `[]` |
| scratch 3.12 venv with a stdlib-only `sitecustomize.py` | `['sitecustomize']` |
| system `/usr/bin/python3` (3.14.4, Ubuntu) | `['_distutils_hack', 'apport_python_hook', 'sitecustomize']` |

## 3. Correction

The fix changes the probe only. No production source, trust-root protection,
holdout rule, run-v4 contract or migration specification was changed.

The new probe:

1. records the non-stdlib top-level modules already loaded when interpreter
   startup finishes (`startup`);
2. **removes them from `sys.modules`**;
3. imports `stock_gplm`, `stock_gplm.m5`, `stock_gplm.m5.cli` and
   `stock_gplm.v1.studio`;
4. reports every non-stdlib top-level module now loaded, other than
   `stock_gplm` and `__main__`.

Startup runs before any project code, so step 2 can't hide anything
`stock_gplm` imports. If `stock_gplm` imports a module that startup had already
loaded, the purge forces it to load again and it is reported.

The probe also asserts that `stock_gplm.__file__` lies under the requested
source tree. Without that check, a stale installed copy could be probed instead
of the tree under review.

## 4. Negative tests and mutation evidence

All new tests are synthetic. Each builds a throwaway venv under pytest's
`tmp_path` whose startup injects modules through `sitecustomize.py` and a
`.pth` file:

| Test | Proves |
| --- | --- |
| `test_E_startup_injected_modules_are_not_reported_as_dependencies` | The real m45 execution path reports `[]` even when startup preloads `sitecustomize`, a `.pth` hook and a third-party module |
| `test_E_probe_still_reports_genuine_third_party_imports` (6 cases) | A fake tree importing `fake_dep` through `import`, `from … import`, or an optional `try/except ImportError` import is reported as `['fake_dep']`, whether startup preloaded `fake_dep` or not |
| `test_E_probe_rejects_a_stock_gplm_imported_from_another_tree` | An installed copy shadowing the requested tree fails the provenance assertion |

Mutation, run on a scratch copy and never in the worktree. The unmutated
control passed 13/13 E-tests. Two weaker corrections were tried:

| Mutant | Result |
| --- | --- |
| M1: subtract the startup set without purging | **Killed**, 3 failures (`[] == ['fake_dep']`) in exactly the `preloaded-by-startup` cases. This variant would let startup hide a real dependency. |
| M2: allow only the names `sitecustomize`/`usercustomize` | **Killed**, 7 failures. `.pth` hooks and modules imported by hooks remain false positives. |

## 5. Provenance and environment

- The m45 worktree has no `.venv`. The shared interpreter at
  `/home/mgurunath/projects/stock-gplm/.venv` resolves `stock_gplm` to the
  **main checkout** (`/home/mgurunath/projects/stock-gplm/src`) unless
  `PYTHONPATH=src` is set. Every command below sets it.
- With `PYTHONPATH=src`, a full `--collect-only` session loaded 89 `stock_gplm`
  modules, and none came from outside `stock-gplm-m45/src`.
- The subprocess dependency probes don't depend on the environment: they insert
  the m45 `src` path explicitly and assert where `stock_gplm` was imported from.

| Item | Value |
| --- | --- |
| OS | Linux 6.18.33.1-microsoft-standard-WSL2 (WSL2) |
| User | `mgurunath`, uid 1000 (non-root) |
| Interpreter | CPython 3.12.16, `/home/mgurunath/projects/stock-gplm/.venv/bin/python` |
| Test tools | pytest 8.4.2, pluggy 1.7.0 |
| `requirements-dev.lock` | `912081a0c76469b883eac802e68e82d7af4f0d08fa2f07130c71a360a73df95c` |
| `requirements-m35.lock` | `748f97fd952521ce956456e87b3a95dd1c43d58d27342adc3374915aefcb5106` |
| `pyproject.toml` | `39f3cd6519f5929b159184e750bd2e74fa062ef85e2fe81be60cdd831b28addf` |

## 6. Reproducible commands

Run these from `/home/mgurunath/projects/stock-gplm-m45`:

```sh
PY=/home/mgurunath/projects/stock-gplm/.venv/bin/python

# full suite
PYTHONPATH=src $PY -m pytest -ra -p no:cacheprovider

# ownership rejection must report PASSED, not SKIPPED (run as a non-root user)
PYTHONPATH=src $PY -m pytest -p no:cacheprovider -v -rs \
  tests/test_m512_remediation.py::test_C_user_owned_trust_root_is_rejected_when_root_ownership_is_required

# dependency probe and its negative tests
PYTHONPATH=src $PY -m pytest -p no:cacheprovider -v tests/test_m51_integrity.py -k test_E_
```

Results:

| Run | Result |
| --- | --- |
| Baseline `f29af79` | 872 collected; 870 passed, 2 skipped, 0 failed (43.6 s) |
| After correction | 880 collected; 878 passed, 2 skipped, 0 failed (43.8 s) |
| E-tests | 13 passed |

The 2 skips are unchanged genuine-data tests:

- `tests/test_m22_master_validation.py:95`: no local real Shoonya NSE master;
- `tests/test_m2_quality.py:272`: no local accepted RELIANCE segment.

No synthetic substitute replaces them, and no real-data acceptance is claimed.

## 7. Scope kept

- Changed files: `tests/test_m51_integrity.py` (probe correction and new tests)
  and this report.
- Nothing was written to raw data, existing reports, other worktrees or `/etc`.
  Scratch venvs and mutants were created only under the session scratch
  directory and pytest `tmp_path`.
- Nothing was pushed or merged. No protected holdout was scored, and there was
  no paper or live trading.
- What this evidence covers: the full suite passes on this host's workspace
  interpreter, and the dependency probe is now independent of interpreter
  startup hooks.
- Not run here: the full suite under a distribution Python 3.12. That
  interpreter isn't installed; the system Python is 3.14. The auditor's
  environment should rerun §6 to confirm.
