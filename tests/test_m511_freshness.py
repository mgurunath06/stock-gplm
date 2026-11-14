"""M5.1.1 assessment freshness (audit criterion 2).

The strict authority gates block every synthetic strict request, so this module
opts into the TEST-ONLY ``waive_strict_authority`` fixture to reach the strict
execution path. Freshness itself is never waived.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
from m5_fixtures import build_world, settings_dict, strategy_config
from m5_fixtures import waive_strict_authority  # noqa: F401 - fixture

from stock_gplm.m5 import (
    BacktestSettings,
    DecisionStatus,
    HistoricalDataAdapter,
    StaleAssessmentError,
    UniverseRequest,
    run_historical_backtest,
)
from stock_gplm.v1.registry import StrategyRegistry

pytestmark = pytest.mark.usefixtures("waive_strict_authority")
SYMBOLS = ("ALPHA-EQ", "BETA-EQ")


def _assess(tmp_path: Path):
    raw, evidence = build_world(tmp_path, SYMBOLS)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(
        UniverseRequest(SYMBOLS, "2024-01-01", "2024-09-30", "STRICT_RESEARCH"))
    assert assessment.status == DecisionStatus.ELIGIBLE and assessment.investable
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    assert evidence.config_path is not None
    return raw, Path(evidence.config_path), assessment, version


def _run(tmp_path: Path, assessment, version):
    return run_historical_backtest(version, assessment, BacktestSettings.from_dict(settings_dict()),
                                   tmp_path / "out")


def _assert_nothing_written(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert not out.exists() or not any(out.iterdir())


def test_unchanged_sources_stay_valid_and_run(tmp_path: Path) -> None:
    raw, _, assessment, version = _assess(tmp_path)
    for decision in assessment.decisions:
        decision.revalidate()
        assert all({"sha256", "done_sha256", "meta_sha256"} <= set(s) for s in decision.segments)
    assert _run(tmp_path, assessment, version)["investable"] is True


def _first(raw: Path, symbol: str, suffix: str) -> Path:
    return sorted((raw / symbol).glob(f"*{suffix}"))[0]


def _append(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b" ")


RAW_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "payload_modified": lambda raw: _append(_first(raw, "BETA-EQ", ".json.gz")),
    "payload_replaced": lambda raw: _first(raw, "BETA-EQ", ".json.gz").write_bytes(
        _first(raw, "ALPHA-EQ", ".json.gz").read_bytes()),
    "done_modified": lambda raw: _append(_first(raw, "BETA-EQ", ".json.gz.done")),
    "done_removed": lambda raw: _first(raw, "BETA-EQ", ".json.gz.done").unlink(),
    "meta_modified": lambda raw: _append(_first(raw, "BETA-EQ", ".meta.json")),
    "meta_removed": lambda raw: _first(raw, "BETA-EQ", ".meta.json").unlink(),
    "symbol_removed": lambda raw: (raw / "BETA-EQ").rename(raw / "BETA-EQ.moved"),
}


@pytest.mark.parametrize("mutation", sorted(RAW_MUTATIONS))
def test_raw_change_in_one_member_blocks_the_whole_strict_universe(tmp_path: Path, mutation: str) -> None:
    raw, _, assessment, version = _assess(tmp_path)
    RAW_MUTATIONS[mutation](raw)
    assessment.decisions[0].revalidate()  # ALPHA is untouched and still fresh...
    with pytest.raises(StaleAssessmentError, match="BETA-EQ"):
        _run(tmp_path, assessment, version)  # ...but one stale member fails the atomic run
    _assert_nothing_written(tmp_path)


def _rewrite_paths(config: Path, change: Callable[[dict], None]) -> None:
    value = json.loads(config.read_text())
    change(value)
    config.write_text(json.dumps(value))


EVIDENCE_MUTATIONS: dict[str, Callable[[Path], None]] = {
    "master_modified": lambda config: _append(config.parent / json.loads(config.read_text())["instrument_master"]),
    "paths_file_removed": lambda config: config.unlink(),
    "source_unregistered": lambda config: _rewrite_paths(config, lambda v: v.pop("calendar_registration")),
}


@pytest.mark.parametrize("mutation", sorted(EVIDENCE_MUTATIONS))
def test_evidence_change_invalidates_the_strict_run(tmp_path: Path, mutation: str) -> None:
    _, config, assessment, version = _assess(tmp_path)
    EVIDENCE_MUTATIONS[mutation](config)
    with pytest.raises(StaleAssessmentError, match="evidence"):
        _run(tmp_path, assessment, version)
    _assert_nothing_written(tmp_path)


def test_stale_dataset_list_is_rejected_too(tmp_path: Path) -> None:
    raw, _, assessment, version = _assess(tmp_path)
    _append(_first(raw, "ALPHA-EQ", ".json.gz"))
    with pytest.raises(StaleAssessmentError, match="ALPHA-EQ"):
        run_historical_backtest(version, list(assessment.released), BacktestSettings.from_dict(settings_dict()),
                                tmp_path / "out")
    _assert_nothing_written(tmp_path)


def test_diagnostic_runs_are_not_subject_to_strict_freshness(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, SYMBOLS)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(
        UniverseRequest(SYMBOLS, "2024-01-01", "2024-09-30", "DIAGNOSTIC_ONLY"))
    first = _run(tmp_path, assessment, StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config()))
    assert first["investable"] is False
