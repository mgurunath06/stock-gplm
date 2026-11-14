"""M5.1.1 strict authority gates (audit criteria 3, 4 and 5).

This module deliberately does NOT use the ``waive_strict_authority`` fixture:
every test here runs the real policy over synthetic evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from m5_fixtures import build_world, settings_dict, strategy_config

from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    EligibilityMode,
    HistoricalDataAdapter,
    UniverseRequest,
    run_historical_backtest,
)
from stock_gplm.m5.adapter import asserted_sources, strict_authority_failures
from stock_gplm.m5.authority import load_evidence_registration, load_trust_policy
from stock_gplm.m5.evidence import file_sha256, load_reference_evidence
from stock_gplm.m5.models import canonical_hash
from stock_gplm.m5.runner import _strict_cost_profile
from stock_gplm.v1.registry import StrategyRegistry

AUTHORITY_CODES = {"EVIDENCE_SYNTHETIC", "EVIDENCE_AUTHORITY_UNAPPROVED",
                   "INSTRUMENT_CLASSIFICATION_UNVERIFIED", "COST_SCHEDULE_UNREGISTERED"}


def _codes(decision) -> set[str]:
    return {f.code for f in decision.failures}


def test_synthetic_evidence_never_authorizes_a_strict_decision(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    decision, dataset = HistoricalDataAdapter(raw, evidence).assess(
        DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", "STRICT_RESEARCH"))
    assert decision.adapter_issued  # genuinely issued by the adapter from loaded files...
    assert decision.status == DecisionStatus.BLOCKED  # ...and still not authoritative
    assert AUTHORITY_CODES <= _codes(decision)
    assert dataset is None and not decision.investable
    assert AUTHORITY_CODES <= set(decision.to_dict()["missing_evidence"])


def test_strict_universe_and_run_are_blocked_including_zero_cost_strategies(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(
        UniverseRequest(("ALPHA-EQ", "BETA-EQ"), "2024-01-01", "2024-09-30", "STRICT_RESEARCH"))
    assert assessment.status == DecisionStatus.BLOCKED and not assessment.released
    assert not assessment.investable
    config = strategy_config()
    config["trading_costs"] = {**config["trading_costs"], "commission_bps": 0, "slippage_bps": 0}
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(config)
    settings = BacktestSettings.from_dict(settings_dict(buy_statutory_bps=0, sell_statutory_bps=0,
                                                        cost_basis="caller assertion only"))
    with pytest.raises(ValueError, match="no data released"):
        run_historical_backtest(version, assessment, settings, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_diagnostic_mode_still_runs_on_synthetic_evidence(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(
        UniverseRequest(("ALPHA-EQ", "BETA-EQ"), "2024-01-01", "2024-09-30", "DIAGNOSTIC_ONLY"))
    assert assessment.status == DecisionStatus.DIAGNOSTIC_ALLOWED and assessment.released
    assert not any(AUTHORITY_CODES & _codes(d) for d in assessment.decisions)
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    run = run_historical_backtest(version, assessment, BacktestSettings.from_dict(settings_dict()),
                                  tmp_path / "out")
    assert run and run.get("investable") is False and "DIAGNOSTIC" in run.get("classification", "")


def test_clean_looking_sources_are_still_unapproved_heuristic_and_uncosted() -> None:
    """Removing synthetic markers is not enough: self-assertion never grants authority."""
    codes = {code for code, _ in strict_authority_failures({"source": "NSE", "source_reference": "https://x"})}
    assert codes == AUTHORITY_CODES - {"EVIDENCE_SYNTHETIC"}


@pytest.mark.parametrize("value", ["synthetic://not-market-evidence", "Synthetic-Test-Fixture", "unit TEST data",
                                   "example.org sample", "mock provider"])
def test_synthetic_markers_are_found_anywhere_in_nested_evidence(value: str) -> None:
    nested = {"bundle": [{"meta": {"source_reference": value}}]}
    assert asserted_sources(nested) == [value]
    assert "EVIDENCE_SYNTHETIC" in {code for code, _ in strict_authority_failures(nested)}


def _registration(directory: Path, *, environment: str = "SYNTHETIC", name: str = "registration",
                  evidence_type: str = "COST_SCHEDULE", dependencies: dict | None = None) -> tuple[Path, dict]:
    document = directory / f"{name}-source-document.bin"
    document.write_bytes(b"synthetic authority test document")
    value = {
        "contract": "stock-gplm.m511-evidence-registration.v1", "schema_version": 1,
        "evidence_type": evidence_type, "source_identity": "synthetic-authority-test",
        "source_document_file": document.name,
        "source_document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
        "acquisition_method": "MANUAL_REVIEW", "acquired_at_utc": "2024-10-02T00:00:00Z",
        "effective_start": "2024-01-01", "effective_end": "2024-12-31",
        "licensing_decision": "APPROVED_FOR_RESEARCH_USE", "reviewer_identity": "test-reviewer",
        "approval_record": "synthetic-test-approval", "approval_status": "APPROVED",
        "evidence_schema": "synthetic.v1", "evidence_version": 1, "environment": environment,
        "dependencies": dependencies or {"payload": "a" * 64},
    }
    provenance = {key: value[key] for key in sorted(value) if key not in {
        "approval_record", "approval_status", "reviewer_identity"
    }}
    value["provenance_hash"] = canonical_hash(provenance)
    value["registration_id"] = canonical_hash({key: value[key] for key in sorted(value)})
    path = directory / f"{name}.json"
    path.write_text(json.dumps(value))
    return path, value


def _policy(directory: Path, registration: dict) -> tuple[Path, str]:
    approval = {key: registration[key] for key in (
        "registration_id", "provenance_hash", "reviewer_identity", "approval_record"
    )} | {"status": "APPROVED"}
    value = {"contract": "stock-gplm.m511-approval-trust-policy.v1", "schema_version": 1,
             "trusted_reviewers": ["test-reviewer"], "approvals": [approval]}
    value["policy_id"] = canonical_hash(value)
    path = directory / "trust-policy.json"
    path.write_text(json.dumps(value))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_self_asserted_or_synthetic_registration_never_confers_authority(tmp_path: Path) -> None:
    registration_path, raw = _registration(tmp_path)
    registration = load_evidence_registration(registration_path)
    policy_path, policy_hash = _policy(tmp_path, raw)
    policy = load_trust_policy(policy_path, policy_hash)
    assert registration.approval_status == "APPROVED"  # self-asserted metadata is parsed...
    assert not policy.approves(registration)  # ...but SYNTHETIC can never enter the trusted domain.


def test_trust_policy_requires_the_out_of_band_exact_hash(tmp_path: Path) -> None:
    registration_path, raw = _registration(tmp_path)
    assert load_evidence_registration(registration_path)
    policy_path, _ = _policy(tmp_path, raw)
    with pytest.raises(ValueError, match="out-of-band"):
        load_trust_policy(policy_path, "not-a-hash")
    with pytest.raises(ValueError, match="does not match"):
        load_trust_policy(policy_path, "0" * 64)


def test_registration_verifies_actual_source_document_bytes(tmp_path: Path) -> None:
    registration_path, _ = _registration(tmp_path)
    (tmp_path / "registration-source-document.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="source document"):
        load_evidence_registration(registration_path)


def test_renamed_synthetic_classification_and_cost_files_remain_unapproved(tmp_path: Path) -> None:
    _, evidence = build_world(tmp_path)
    evidence_dir = Path(evidence.config_path).parent
    classification = {"contract": "stock-gplm.m511-instrument-classification.v1", "schema_version": 1,
                      "effective_start": "2024-01-01", "effective_end": "2024-12-31",
                      "records": [{"symbol": "ALPHA-EQ", "classification": "EQUITY"},
                                  {"symbol": "BETA-EQ", "classification": "EQUITY"}]}
    classification["classification_id"] = canonical_hash(classification)
    classification_path = evidence_dir / "official-exchange-equities.json"
    classification_path.write_text(json.dumps(classification))
    class_registration_path, class_registration = _registration(
        evidence_dir, name="official-classification-approval", evidence_type="INSTRUMENT_CLASSIFICATION",
        dependencies={"payload": file_sha256(classification_path)},
    )
    costs = {"contract": "stock-gplm.m511-cost-schedule.v1", "schema_version": 1,
             "effective_start": "2024-01-01", "effective_end": "2024-12-31",
             "components": {"commission_bps": 5, "slippage_bps": 10,
                            "buy_statutory_bps": 12.41, "sell_statutory_bps": 10.91}}
    costs["profile_id"] = canonical_hash(costs)
    costs_path = evidence_dir / "official-market-costs.json"
    costs_path.write_text(json.dumps(costs))
    cost_registration_path, cost_registration = _registration(
        evidence_dir, name="official-cost-approval", evidence_type="COST_SCHEDULE",
        dependencies={"payload": file_sha256(costs_path)},
    )
    approvals = []
    for registration in (class_registration, cost_registration):
        approvals.append({key: registration[key] for key in (
            "registration_id", "provenance_hash", "reviewer_identity", "approval_record"
        )} | {"status": "APPROVED"})
    policy = {"contract": "stock-gplm.m511-approval-trust-policy.v1", "schema_version": 1,
              "trusted_reviewers": ["test-reviewer"], "approvals": approvals}
    policy["policy_id"] = canonical_hash(policy)
    policy_path = evidence_dir / "trust-policy.json"
    policy_path.write_text(json.dumps(policy))
    paths = json.loads(Path(evidence.config_path).read_text())
    paths["instrument_classification"] = {
        "payload": classification_path.name, "registration": class_registration_path.name}
    paths["cost_schedule"] = {"payload": costs_path.name, "registration": cost_registration_path.name}
    Path(evidence.config_path).write_text(json.dumps(paths))

    loaded = load_reference_evidence(
        Path(evidence.config_path), trust_policy_path=policy_path, trust_policy_sha256=file_sha256(policy_path))
    assert loaded.instrument_classification and not loaded.instrument_classification.authority_approved
    assert loaded.cost_schedule and not loaded.cost_schedule.authority_approved
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", "STRICT_RESEARCH")
    failure_codes = {code for code, _ in strict_authority_failures(loaded, request)}
    assert {"EVIDENCE_SYNTHETIC", "EVIDENCE_AUTHORITY_UNAPPROVED",
            "INSTRUMENT_CLASSIFICATION_UNVERIFIED", "COST_SCHEDULE_UNREGISTERED"} <= failure_codes


def test_strict_execution_requires_exact_registered_cost_identity_and_components() -> None:
    profile = {"profile_id": "approved-profile", "commission_bps": 5, "slippage_bps": 10,
               "buy_statutory_bps": 12.41, "sell_statutory_bps": 10.91}
    datasets = [SimpleNamespace(decision=SimpleNamespace(cost_profile=profile))]
    config = strategy_config()
    approved = BacktestSettings.from_dict(settings_dict(cost_basis="approved-profile"))
    approved_mode = EligibilityMode.STRICT_RESEARCH
    assert _strict_cost_profile(datasets, approved_mode, config, approved) == profile
    with pytest.raises(ValueError, match="components"):
        _strict_cost_profile(datasets, approved_mode, config,
                             BacktestSettings.from_dict(settings_dict(cost_basis="approved-profile",
                                                                      sell_statutory_bps=0)))
    with pytest.raises(ValueError, match="profile_id"):
        _strict_cost_profile(datasets, approved_mode, config, BacktestSettings.from_dict(settings_dict()))
