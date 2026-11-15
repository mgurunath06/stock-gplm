"""Synthetic tests for the MVP indicators, pinned loader and resumable experiments.

Every series here is synthetic; nothing is market evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from m5_fixtures import price_path, sessions, write_segment, write_symbol
from stock_gplm.mvp import data as mvp_data
from stock_gplm.mvp.data import load_symbol, pin_holdout
from stock_gplm.mvp.experiment import run_experiment
from stock_gplm.v1.codec import load_document
from stock_gplm.v1.evaluator import _ema, compute_indicators, normalize_candles
from stock_gplm.v1.schema import StrategyValidationError, validate_strategy

ROOT = Path(__file__).parents[1]


def _candles(closes: list[float], spread: float = 1.0) -> tuple[dict, ...]:
    rows = [{"date": f"2024-01-{i + 1:02d}" if i < 31 else f"2024-02-{i - 30:02d}", "open": c, "high": c + spread,
             "low": c - spread, "close": c, "volume": 1000.0} for i, c in enumerate(closes)]
    return normalize_candles(rows)


def _config(*indicators: dict) -> dict:
    return {"indicators": [validate_strategy(_strategy(list(indicators))).config["indicators"]][0]}


def _strategy(indicators: list[dict], symbols: list[str] | None = None, sid: str = "synthetic-v1") -> dict:
    first = indicators[0]["id"]
    return {
        "schema_version": 1,
        "strategy": {"id": sid, "name": "Synthetic", "description": "test", "enabled": True, "timeframe": "1d"},
        "universe": {"exchange": "NSE", "instrument_type": "EQ", "symbols": symbols or []},
        "indicators": indicators,
        "entry": {"operator": "GT", "left": {"field": "close"}, "right": {"indicator": first}},
        "exit": {"condition": {"operator": "LT", "left": {"field": "close"}, "right": {"indicator": first}},
                 "max_holding_sessions": 10},
        "risk": {"stop_loss": {"type": "percent", "value": 5}, "take_profit": {"type": "percent", "value": 10}},
        "position_sizing": {"type": "fixed_fraction", "value": 0.2},
        "trading_costs": {"profile": "synthetic", "commission_bps": 5, "slippage_bps": 10},
    }


# ---------------------------------------------------------------- indicators

def test_rsi_extremes_and_flat_series() -> None:
    up = compute_indicators(_config({"id": "r", "type": "RSI", "period": 5}), _candles([10.0 + i for i in range(12)]))
    assert up["r"][:5] == [None] * 5 and all(v == 100.0 for v in up["r"][5:])
    flat = compute_indicators(_config({"id": "r", "type": "RSI", "period": 5}), _candles([10.0] * 12))
    assert all(v == 50.0 for v in flat["r"][5:])


def test_rsi_matches_wilder_hand_computation() -> None:
    closes = [10.0, 11.0, 10.5, 11.5, 11.0, 12.0, 11.0]
    value = compute_indicators(_config({"id": "r", "type": "RSI", "period": 3}), _candles(closes))["r"]
    gain, loss = (1.0 + 0 + 1.0) / 3, (0 + 0.5 + 0) / 3
    expected = [None, None, None, 100 - 100 / (1 + gain / loss)]
    for change in (-0.5, 1.0, -1.0):
        gain, loss = (gain * 2 + max(change, 0)) / 3, (loss * 2 + max(-change, 0)) / 3
        expected.append(100 - 100 / (1 + gain / loss))
    assert value[:3] == [None] * 3
    assert value[3:] == pytest.approx(expected[3:])


def test_macd_outputs_are_consistent() -> None:
    closes = [100 + 5 * math.sin(i / 3) + 0.2 * i for i in range(60)]
    candles = _candles(closes)
    base = {"type": "MACD", "period": 26, "fast": 12, "signal": 9}
    out = compute_indicators(_config({"id": "l", **base, "output": "line"}, {"id": "s", **base, "output": "signal"},
                                     {"id": "h", **base, "output": "histogram"}), candles)
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    assert out["l"][24] is None and out["l"][25] == pytest.approx(fast[25] - slow[25])
    assert out["s"][32] is None and out["s"][33] is not None  # 26 + 9 - 2
    for line, signal, hist in zip(out["l"], out["s"], out["h"]):
        if hist is not None:
            assert hist == pytest.approx(line - signal)


def test_atr_and_volatility() -> None:
    candles = _candles([100.0] * 20, spread=2.0)
    atr = compute_indicators(_config({"id": "a", "type": "ATR", "period": 5}), candles)["a"]
    assert atr[:5] == [None] * 5 and all(v == pytest.approx(4.0) for v in atr[5:])
    growth = _candles([100.0 * 1.01 ** i for i in range(20)])
    vol = compute_indicators(_config({"id": "v", "type": "VOLATILITY", "period": 5}), growth)["v"]
    assert vol[:5] == [None] * 5 and all(v == pytest.approx(0.0, abs=1e-12) for v in vol[5:])


@pytest.mark.parametrize("indicator", [
    {"id": "x", "type": "RSI", "period": 14, "fast": 3},
    {"id": "x", "type": "MACD", "period": 12, "fast": 12},
    {"id": "x", "type": "MACD", "period": 26, "output": "bogus"},
    {"id": "x", "type": "ATR", "period": 14, "field": "high"},
    {"id": "x", "type": "VOLATILITY", "period": 1},
])
def test_invalid_new_indicators_are_rejected(indicator: dict) -> None:
    with pytest.raises(StrategyValidationError):
        validate_strategy(_strategy([indicator]))


def test_existing_default_strategy_hashes_are_unchanged() -> None:
    expected = {"swing-breakout-v1.json": "7458ff27", "mean-reversion-v1.json": "a0fb58a5"}
    for name, prefix in expected.items():
        text = (ROOT / "config/v1/strategies" / name).read_text()
        assert validate_strategy(load_document(text, "json")).config_hash.startswith(prefix)


def test_yaml_default_strategy_loads() -> None:
    text = (ROOT / "config/v1/strategies/macd-momentum-v1.yaml").read_text()
    config = validate_strategy(load_document(text, "yaml")).config
    assert config["indicators"][0] == {"id": "macd-hist", "type": "MACD", "period": 26, "field": "close",
                                       "fast": 12, "signal": 9, "output": "histogram"}


# ---------------------------------------------------------------- pinned loader

def _m32_run(directory: Path, cutoffs: list[str]) -> tuple[Path, str]:
    directory.mkdir(parents=True)
    plan = json.dumps({"schema_version": 1, "plans": [
        {"horizon": f"h{i}", "locked_holdout": {"first_as_of": c, "last_as_of": "2024-12-31"}}
        for i, c in enumerate(cutoffs)]}).encode()
    (directory / "split-plan.json").write_bytes(plan)
    digest = hashlib.sha256(plan).hexdigest()
    (directory / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m32-research-run.v1", "run_id": "synthetic-run",
        "outputs": {"split-plan.json": digest}}))
    return directory, digest


def test_pin_uses_earliest_holdout_and_rejects_tampering(tmp_path: Path) -> None:
    run, digest = _m32_run(tmp_path / "m32", ["2024-11-01", "2024-10-01"])
    pin = pin_holdout(run, "synthetic-run", digest)
    assert pin.cutoff == "2024-10-01"
    with pytest.raises(ValueError):
        pin_holdout(run, "other-run", digest)
    (run / "split-plan.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        pin_holdout(run)


def test_loader_never_opens_straddling_or_later_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw"
    write_symbol(raw, "AAA-EQ")
    opened: list[str] = []
    real = mvp_data.validate_daily
    monkeypatch.setattr(mvp_data, "validate_daily", lambda p: (opened.append(Path(p).name), real(p))[1])
    data = load_symbol(raw, "AAA-EQ", "2024-09-30")  # the Jul–Sep segment ends ON the cutoff: withheld
    assert opened == ["2024-01-01_2024-06-30.json.gz"]
    assert {s["file"] for s in data.segments_withheld} == {"2024-07-01_2024-09-30.json.gz",
                                                          "2024-10-01_2024-12-31.json.gz"}
    assert data.status == "ACCEPTED" and data.bars[-1]["date"] <= "2024-06-30"


def test_corrupt_segment_quarantines_symbol_without_repair(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    write_symbol(raw, "AAA-EQ")
    (raw / "AAA-EQ" / "2024-01-01_2024-06-30.json.gz").write_bytes(b"corrupt")
    data = load_symbol(raw, "AAA-EQ", "2024-10-01")
    assert data.status == "QUARANTINED" and "REJECT_PUBLICATION" in data.issues[0]
    assert load_symbol(raw, "ZZZ-EQ", "2024-10-01").status == "MISSING"


# ---------------------------------------------------------------- experiments

def _experiment(tmp_path: Path, evaluation_end: str = "2024-09-30") -> Path:
    raw = tmp_path / "raw"
    for i, symbol in enumerate(["AAA-EQ", "BBB-EQ", "CCC-EQ"]):
        write_symbol(raw, symbol, phase=i * 0.7)
    write_segment(raw, "BAD-EQ", "2024-01-01", "2024-06-30", price_path(sessions())[:5], done=False)
    run, digest = _m32_run(tmp_path / "m32", ["2024-10-01"])
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    ema = [{"id": "ema5", "type": "EMA", "period": 5}, {"id": "rsi5", "type": "RSI", "period": 5}]
    (strategies / "a.json").write_text(json.dumps(_strategy(ema, sid="all-v1")))
    (strategies / "b.json").write_text(json.dumps(_strategy(ema, symbols=["AAA-EQ"], sid="only-aaa-v1")))
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps({
        "contract": "stock-gplm.mvp-experiment.v1", "name": "synthetic", "m32_run": "m32",
        "m32_run_id": "synthetic-run", "split_plan_sha256": digest, "raw_root": "raw",
        "symbols": ["AAA-EQ", "BBB-EQ", "CCC-EQ", "BAD-EQ"], "strategies": ["strategies/a.json", "strategies/b.json"],
        "horizons": {"short": 3, "long": 20}, "evaluation_start": "2024-03-01", "evaluation_end": evaluation_end,
        "portfolio": {"initial_capital": 100000.0, "max_open_positions": 3, "max_position_fraction": 0.2,
                      "max_gross_exposure": 1.0},
        "costs": {"buy_statutory_bps": 12.41, "sell_statutory_bps": 10.91}}))
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix != ".sqlite3"}


def test_experiment_is_deterministic_resumable_and_quarantines(tmp_path: Path) -> None:
    path = _experiment(tmp_path)
    out = tmp_path / "out"
    logs: list[str] = []
    report = run_experiment(path, out, log=logs.append)
    assert len(report["runs"]) == 4 and report["accepted_symbols"] == ["AAA-EQ", "BBB-EQ", "CCC-EQ"]
    assert "BAD-EQ" in report["quarantined_symbols"]
    only = [r for r in report["runs"] if r["strategy_id"] == "only-aaa-v1"]
    assert {r["symbols"] for r in only} == {1}
    assert all(r["classification"] == "DIAGNOSTIC_ONLY_NON_INVESTABLE" for r in report["runs"])
    first = _snapshot(out)
    logs.clear()
    run_experiment(path, out, log=logs.append)
    assert sum(m.startswith("skip ") for m in logs) == 4
    assert _snapshot(out) == first
    # A fresh root reproduces byte-identical run directories.
    run_experiment(path, tmp_path / "out2", log=lambda m: None)
    second = _snapshot(tmp_path / "out2")
    assert {k: v for k, v in second.items() if k.startswith("runs/")} == \
        {k: v for k, v in first.items() if k.startswith("runs/")}
    header = (out / "summary.csv").read_text().splitlines()[0]
    assert header.startswith("strategy_id,strategy_version,horizon")


def test_experiment_refuses_to_evaluate_into_the_holdout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="locked holdout"):
        run_experiment(_experiment(tmp_path, evaluation_end="2024-10-01"), tmp_path / "out", log=lambda m: None)


# ---------------------------------------------------------------- research interfaces

from stock_gplm.mvp.research import (  # noqa: E402
    NoIntradayData, TrainingWindow, apply_window, comparison_grid, join_timeframes, recency_weights)


def test_rolling_window_and_recency_weights() -> None:
    days = sessions()[:30]
    window = TrainingWindow("exponential", 10, 5.0)
    assert apply_window(days, window) == days[-10:]
    weights = recency_weights(days[-10:], days, window)
    assert sum(weights) == pytest.approx(10.0)
    assert weights[-1] / weights[-6] == pytest.approx(2.0)  # one half-life apart
    assert recency_weights(days[-3:], days, TrainingWindow()) == [1.0, 1.0, 1.0]
    with pytest.raises(ValueError, match="outside"):
        recency_weights(days[:1], days, window)
    with pytest.raises(ValueError):
        TrainingWindow("exponential")
    labels = [w.label for w in comparison_grid([None, 60], [20.0])]
    assert labels == ["expanding-eq", "expanding-hl20", "roll60-eq", "roll60-hl20"]


class _LateSource:
    def features(self, symbol: str, session: str) -> dict[str, float]:
        return {"vwap_gap": 0.01}

    def available_at(self, symbol: str, session: str) -> str:
        return session + "T11:00:00Z"


def test_timeframe_join_is_causal_and_never_fills() -> None:
    rows = [{"date": "2024-01-02", "close": 10.0}]
    assert join_timeframes("AAA-EQ", rows, NoIntradayData()) == [
        {"date": "2024-01-02", "close": 10.0, "intraday_present": False}]
    with pytest.raises(ValueError, match="after the decision time"):
        join_timeframes("AAA-EQ", rows, _LateSource())
