# M5 acceptance report — 2026-11-14

Statuses follow the mandate: PASS, FAIL, BLOCKED or NOT_APPLICABLE. PASS is used
only for checks that were executed. "Synthetic" means verified with
deterministic test fixtures only. "Genuine" means executed on the local Shoonya
files.

## Overall

| Milestone outcome | Status |
| --- | --- |
| M5 adapter, eligibility policies, simulator, runner, Studio and CLI implemented and tested | PASS (synthetic + genuine integrity/coverage) |
| Strict real-data eligibility for any genuine dataset | **BLOCKED** (calendar, PIT listing, alias, corporate-action and provider price-basis evidence missing) |
| Strict real-data backtest and fair strategy comparison | **BLOCKED** (follows from the above) |
| Diagnostic real-data demonstration | PASS as a pipeline demonstration; non-investable by construction |
| Overall real-data acceptance | **BLOCKED**, not PASS |

## Eligibility gates (mandate M5.2 items 1–14)

| # | Requirement | Implementation | Status | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Valid source identity | metadata `source == shoonya`, symbol equals the directory, dates equal the filename | PASS (synthetic + genuine) | `test_invalid_or_quarantined_metadata_rejected`; 10,355 genuine segments without metadata rejection |
| 2 | Exchange and series | `-EQ` only; ETF heuristic exclusion | PASS | `test_unsupported_series_etf_and_unknown_symbol_rejected`; 309 genuine ETF exclusions |
| 3 | Metadata/manifest compatibility | `validate_daily` plus metadata checks | PASS | as 1 |
| 4 | SHA-256 publication integrity | `validate_daily` `.done` hash, bytes and records | PASS | `test_tampered_payload_is_rejected_in_every_mode` |
| 5 | Completion markers | `UNPUBLISHED_SEGMENT` / `ORPHAN_MARKER` | PASS | `test_missing_marker_rejected`; genuine incomplete triplets: 0 |
| 6 | OHLCV structure | `ShoonyaDailyV1` content gate | PASS | 9 genuine quarantined segments rejected |
| 7 | Chronology/session ordering | `validate_daily` chronology plus sorted canonical bars | PASS | existing M1 tests; canonical bars ascending |
| 8 | No duplicate/contradictory observations | cross-segment comparison | PASS | `test_contradictory_overlapping_segments_rejected` |
| 9 | Explicit calendar support | M2.5-R registration format; closed-date contradiction; missing sessions | PASS (synthetic, plus M2.5-R cross-check) / BLOCKED (genuine: no registered calendar) | `test_calendar_missing_gap_and_contradiction`, `test_calendar_registration_tamper_is_rejected` |
| 10 | PIT identity and listing | M2.1 listing history; current master for current identity only | PASS (synthetic) / BLOCKED (genuine) | `test_missing_pit_membership_blocks_strict…`, `test_listing_contradicting_observations…`, `test_symbol_token_mismatch…` |
| 11 | Symbol rename/alias handling | complete M2.1 mapping coverage required; overlapping alias blocks | PASS (synthetic) / BLOCKED (genuine) | `test_unverified_alias_evidence_and_overlapping_alias_block` |
| 12 | Corporate-action compatibility | M2.1 coverage plus validated M3.9-B events; as-of regime evaluation; provider-basis gate | PASS (synthetic) / BLOCKED (genuine) | `test_corporate_actions_apply_only_from_their_known_effective_date`, `test_unverified_provider_basis_blocks_strict…` |
| 13 | Quarantine and missing-data rules | quarantined source/segment rejected; publication gaps and missing sessions recorded, never filled | PASS | `test_publication_gap_is_evidence_not_fabrication`, `test_missing_session_cancels_entry…` |
| 14 | No sealed-holdout access | hash-verified M3.2 cutoff; overlap rejected; straddling rows withheld | PASS (synthetic + genuine) | `test_holdout_overlap_and_unverified_boundary_rejected`; genuine RELIANCE: 48 rows withheld, overlap rejected |

## Policies, normalization, execution and reporting

| Requirement | Status | Evidence |
| --- | --- | --- |
| M5.3 STRICT_RESEARCH requires all evidence | PASS | 0 of 2,701 genuine symbols eligible; synthetic complete world is ELIGIBLE |
| M5.3 DIAGNOSTIC_ONLY labelled, unrankable, unpromotable, not for paper trading or ML, missing evidence listed | PASS | `test_diagnostic_runs_cannot_be_ranked_or_promoted`; genuine compare returned no ranking |
| M5.3 No force/skip escape hatch; diagnostic does not bypass integrity | PASS | no such parameter; tampered payload rejected in diagnostic; Studio refuses extra fields |
| M5.4 Canonical, versioned bars; no forward fill, synthetic volume or mutation | PASS | `CanonicalDailyBar`; `test_adapter_and_backtest_leave_historical_files_unchanged`; genuine hash baseline unchanged |
| M5.5 Immutable version + dataset + explicit range, settings, costs and capital | PASS | `run_historical_backtest`; `BacktestSettings` requires every field |
| M5.5 One generic engine for both strategies | PASS | `test_both_sample_strategies_share_one_generic_engine`; genuine runs share simulator and policy |
| M5.5 Causality (warm-up, future candles, next open, corporate-action boundaries) | PASS (synthetic) | `test_future_candles_never_change_earlier_signals`, `test_warmup…`, `test_exit_signal_fills_at_next_open`, `test_corporate_actions_apply_only…` |
| M5.5 Gaps, stops, targets, intrabar ambiguity, missing sessions, end-of-range positions | PASS (synthetic) | `test_gap_stop_target_and_ambiguity_in_portfolio`, `test_missing_session…`, `test_entry_next_open_costs…` |
| M5.6 Sizing, cash, exposure, costs, no negative cash, no duplicates, MTM, drawdown | PASS (synthetic) | `test_cash_exposure_and_duplicate_position_limits`, `test_entry_next_open_costs_and_slippage_are_exact`, `test_split_while_holding…` |
| M5.7 Provenance, atomic and non-overwriting output, deterministic reruns | PASS (synthetic + genuine) | `test_reruns_are_deterministic_and_never_overwrite`; genuine byte-identical reruns |
| §6 Metrics with explicit undefined handling and benchmark | PASS | `performance_metrics`, `equal_weight_buy_and_hold` |
| §6 Fair swing vs mean-reversion comparison | BLOCKED | only diagnostic runs exist; comparison correctly refuses to rank |
| §7 Studio workflow (discover, eligibility, display, run, compare, export), server-side eligibility, loopback, bounded | PASS (synthetic) | `tests/test_m5_studio.py`; Host/Origin tests in M4 |
| §7 Editing creates a new version; old runs keep theirs | PASS | `test_diagnostic_workflow_runs_lists_exports_and_refuses_ranking` |

## Mandate §8 test map (selected)

Security and provenance (tampered hash, missing marker, invalid metadata,
symbol/token mismatch, missing PIT, unverified alias, missing calendar,
quarantined source, unsupported series, diagnostic not promotable, Studio cannot
forge eligibility, artifacts unchanged, unsafe YAML/JSON) are covered by
`tests/test_m5_historical_adapter.py`, `tests/test_m5_studio.py` and the existing
V1 codec tests. Integration (no broker, credential, Oracle or timer dependency)
is covered by `test_m5_has_no_network_broker_credential_or_scheduler_dependency`,
which imports M5, the M5 CLI and the Studio in a clean interpreter. It asserts
that no M2.4, M2.5, M3.9 network or LLM module, and none of `requests`,
`urllib3`, `selenium` or `paramiko`, is loaded. It also asserts that M5 source
contains no `urlopen`, environment-variable access, scheduler commands, order
placement, `eval` or `exec`. The Studio itself uses only the standard-library
`http.server`, bound to loopback.

## Commands and results

| Command | Result |
| --- | --- |
| `cd stock-gplm && PYTHONPATH=../stock-gplm-m45/src .venv/bin/python -m pytest ../stock-gplm-m45/tests` | 668 passed, 0 skipped (the real-data regressions ran) |
| `PYTHONPATH=src .venv/bin/python -m pytest tests/test_m5_historical_adapter.py tests/test_m5_studio.py` | 42 passed |
| `python -m compileall -q src` | PASS |
| `node --check` on the Studio script | PASS |
| unsafe-construct scan of `v1` and `m5` | none found |
| `git diff --check` | PASS |
| `stock-gplm-m5 discover --verify` (genuine) | 10,355 triplets; 10,346 ACCEPT; 9 QUARANTINE_CONTENT |
| eligibility audit (genuine, 2,701 symbols, both modes) | strict 0 eligible / 1,980 blocked / 721 rejected |
| immutability: SHA-256 of 31,928 pre-existing `data/` + `reports/` files | unchanged; 0 files added under `data/` |

## Unresolved dependencies and approvals required

1. **Manual NSE evidence** (user action; NSE terms forbid automated collection):
   trading-calendar document and transcription; listing history; symbol-change
   list; corporate-action export; equity and ETF lists.
2. **Provider price-basis contract** (design decision): define and review how a
   per-symbol adjustment basis is evidenced, or obtain an unadjusted source.
   Until then, M3.9-B cannot be applied to Shoonya history.
3. **Corporate-action licence**: M3.9-B requires `APPROVED_FOR_DERIVATION` for any
   real event.
4. **Cost schedule verification**: statutory costs are the M3.5 policy's labelled
   assumptions.
5. **Merge decision**: `feature/m5-historical-adapter` (which contains V1 and M4)
   is local only and not merged into `main`.
6. **M2.5-R** stays isolated and not approved for activation; nothing in M5
   needs it activated.
