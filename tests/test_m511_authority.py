"""M5.1.1 strict authority gates (audit criteria 3, 4 and 5).

This module deliberately does NOT use the ``waive_strict_authority`` fixture:
every test here runs the real policy over synthetic evidence.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from m5_fixtures import build_world, settings_dict, strategy_config

from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    HistoricalDataAdapter,
    UniverseRequest,
    run_historical_backtest,
)
from stock_gplm.m5.adapter import asserted_sources, strict_authority_failures
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
