from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from stock_gplm.v1.backtest import run_backtest
from stock_gplm.v1.codec import dump_yaml, load_document, load_yaml
from stock_gplm.v1.demo import run_demo
from stock_gplm.v1.evaluator import evaluate_strategy
from stock_gplm.v1.paper import generate_paper_decision
from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.schema import ALLOWED_OPERATORS, StrategyValidationError, validate_strategy
from stock_gplm.v1.studio import _HTML

ROOT = Path(__file__).parents[1]


def load_example(name: str = "mean-reversion-v1.json") -> dict:
    return json.loads((ROOT / "config" / "v1" / "strategies" / name).read_text(encoding="utf-8"))


def candles(count: int = 280) -> list[dict]:
    start = date(2025, 1, 1)
    result = []
    for index in range(count):
        close = 100.0 + index * 0.1
        result.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 0.1,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000.0,
            }
        )
    return result


def short_strategy(threshold: float = 5.0) -> dict:
    config = load_example()
    config["strategy"] = {
        "id": "configured-test",
        "name": "Configured Test",
        "description": "Created in configuration only.",
        "enabled": True,
        "timeframe": "1d",
    }
    config["indicators"] = [{"id": "ma2", "type": "SMA", "period": 2, "field": "close"}]
    config["entry"] = {
        "operator": "AND",
        "conditions": [
            {"operator": "GT", "left": {"field": "close"}, "right": {"constant": threshold}},
            {
                "operator": "OR",
                "conditions": [
                    {"operator": "GT", "left": {"field": "close"}, "right": {"indicator": "ma2"}},
                    {"operator": "NOT", "conditions": [
                        {"operator": "GTE", "left": {"field": "volume"}, "right": {"constant": 1_000_000}}
                    ]},
                ],
            },
        ],
    }
    config["exit"] = {
        "condition": {"operator": "LT", "left": {"field": "close"}, "right": {"constant": 1}},
        "max_holding_sessions": 3,
    }
    config["risk"] = {
        "stop_loss": {"type": "percent", "value": 50},
        "take_profit": {"type": "percent", "value": 50},
    }
    return config


def test_both_examples_validate_and_use_one_generic_engine() -> None:
    first = validate_strategy(load_example("swing-breakout-v1.json"))
    second = validate_strategy(load_example("mean-reversion-v1.json"))
    assert first.config_hash != second.config_hash
    assert first.strategy_id == "swing-breakout-v1"
    assert second.strategy_id == "mean-reversion-v1"
    assert set(ALLOWED_OPERATORS) >= {"GT", "AND", "MULTIPLY", "CROSS_ABOVE", "HIGHEST", "PERCENT_CHANGE"}
    assert len(evaluate_strategy(first, candles()).entry_signals) == 280
    assert len(evaluate_strategy(second, candles()).entry_signals) == 280


def test_configuration_changes_signals_without_python_edits() -> None:
    rows = candles(8)
    low = evaluate_strategy(short_strategy(99), rows)
    high = evaluate_strategy(short_strategy(10_000), rows)
    assert any(low.entry_signals)
    assert not any(high.entry_signals)


def test_nested_and_or_not_semantics() -> None:
    result = evaluate_strategy(short_strategy(99), candles(4))
    assert result.entry_signals == (True, True, True, True)
    config = short_strategy(99)
    config["entry"]["conditions"][1]["conditions"][1]["operator"] = "NOT"
    config["entry"]["conditions"][1]["conditions"][1]["conditions"][0]["operator"] = "LT"
    assert evaluate_strategy(config, candles(4)).entry_signals == (False, True, True, True)


def test_not_does_not_turn_unavailable_lookback_into_true() -> None:
    config = short_strategy()
    config["entry"] = {
        "operator": "NOT",
        "conditions": [
            {"operator": "GT", "left": {"field": "close"}, "right": {"indicator": "ma2"}}
        ],
    }
    assert evaluate_strategy(config, candles(4)).entry_signals == (False, False, False, False)


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda c: c["entry"]["conditions"][0]["left"].update(offset=1), "future-session"),
        (lambda c: c["entry"].update(operator="IMPORT_OS"), "unsupported condition operator"),
        (lambda c: c["indicators"][0].update(type="PYTHON"), "unsupported indicator"),
        (lambda c: c["universe"].update(instrument_type="ETF"), "ETFs remain excluded"),
    ],
)
def test_unsafe_or_unsupported_config_fails_closed(mutation, match: str) -> None:
    config = short_strategy()
    mutation(config)
    with pytest.raises(StrategyValidationError, match=match):
        validate_strategy(config)


def test_yaml_round_trip_preserves_canonical_semantics() -> None:
    validated = validate_strategy(load_example())
    document = dump_yaml(validated.config)
    reloaded = validate_strategy(load_document(document, "yaml"))
    assert reloaded.config == validated.config
    assert reloaded.config_hash == validated.config_hash
    assert "!!python" not in document


@pytest.mark.parametrize("payload", ["x: !!python/object:os.system ['id']\n", "x: &a 1\ny: *a\n", "x: |\n  code\n", "x: 1\nx: 2\n"])
def test_yaml_cannot_construct_or_execute_objects(payload: str) -> None:
    with pytest.raises(ValueError):
        load_yaml(payload)


def test_strategy_versions_are_immutable_and_hash_bound(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "strategy.sqlite3")
    original = short_strategy(5)
    first = registry.save(original)
    changed = short_strategy(500)
    second = registry.save(changed)
    assert (first.version, second.version) == (1, 2)
    assert registry.get(first.strategy_id, 1).config_hash == first.config_hash
    assert first.config_hash != second.config_hash
    with pytest.raises(ValueError, match="already registered"):
        registry.save(copy.deepcopy(original))


def test_backtest_and_paper_intent_reference_exact_version_hash(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "strategy.sqlite3")
    record = registry.save(short_strategy(99))
    result = run_backtest(record.config, candles(8), strategy_version=record.version, data_quality_eligible=True)
    assert result.strategy_hash == record.config_hash
    assert result.strategy_version == record.version
    assert result.execution_policy == "completed_close_signal_next_session_open_v2"
    assert result.data_classification == "CALLER_SUPPLIED_UNVERIFIED"
    assert result.trades[0].entry_signal_date < result.trades[0].entry_date
    registry.record_backtest(result)
    active = registry.activate(record.strategy_id, record.version)
    assert active.activation_status == "ACTIVE"
    decision = generate_paper_decision(
        registry,
        record.strategy_id,
        record.version,
        symbol="TEST-EQ",
        rows=candles(4),
        position_open=False,
        quantity=1,
        data_quality_eligible=True,
    )
    assert decision is not None
    intent = registry.paper_intent(decision.intent_id)
    assert intent["config_hash"] == record.config_hash
    assert intent["strategy_version"] == record.version
    assert intent["status"] == "SIMULATED_NOT_ROUTABLE"


def test_backtest_data_quality_gate_fails_closed() -> None:
    with pytest.raises(ValueError, match="data-quality"):
        run_backtest(short_strategy(), candles(8), data_quality_eligible=False)


def test_arbitrary_fields_and_python_source_are_rejected() -> None:
    config = short_strategy()
    config["entry"]["conditions"][0]["left"] = {"field": "__import__('os').system('id')"}
    with pytest.raises(StrategyValidationError):
        validate_strategy(config)
    package = ROOT / "src" / "stock_gplm" / "v1"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    assert "eval(" not in source
    assert "exec(" not in source


def test_two_strategy_demo_produces_distinct_signals_and_backtests(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    report = run_demo(output, ROOT / "config" / "v1" / "strategies")
    assert report["classification"] == "SYNTHETIC_ENGINEERING_DEMO_NOT_MARKET_EVIDENCE"
    assert [item["strategy_id"] for item in report["strategies"]] == [
        "swing-breakout-v1",
        "mean-reversion-v1",
    ]
    assert [item["entry_signal_dates"] for item in report["strategies"]] == [
        ["2025-09-18"],
        ["2025-09-28"],
    ]
    assert [item["trade_count"] for item in report["strategies"]] == [1, 1]
    assert (output / "strategy-registry.sqlite3").is_file()
    assert (output / "demo-report.json").is_file()
    with pytest.raises(FileExistsError):
        run_demo(output, ROOT / "config" / "v1" / "strategies")


def test_studio_exposes_structured_builder_and_synthetic_gate() -> None:
    for capability in (
        "Structured editor and rule builder",
        "Add indicator",
        "Add rule",
        "Save immutable version",
        "Activate research config",
        "syntheticEligible",
    ):
        assert capability in _HTML
