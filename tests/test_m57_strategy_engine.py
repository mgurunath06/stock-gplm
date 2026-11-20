"""Synthetic tests for the M5.7 declarative strategy library, DSL and generic engine (no real candles)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_gplm.app import strategy_dsl as dsl
from stock_gplm.app.strategy_library import StrategyConfigError, StrategyLibrary

REPO = Path(__file__).resolve().parents[1]


def _ctx(n: int = 600, seed: int = 3) -> dsl.Context:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = c * (1 + rng.normal(0, 0.003, n))
    f = pd.DataFrame({"session": pd.date_range("2020-01-01", periods=n).strftime("%Y-%m-%d"), "open": o,
                      "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c,
                      "volume": rng.integers(10_000, 100_000, n).astype(float),
                      "end_ns": np.arange(n, dtype=np.int64) * 86_400 * 10**9})
    return dsl.Context.from_frame(f, "1d", False)


def test_parser_rejects_code_and_unknown_names() -> None:
    for bad in ("__import__('os')", "close > foo(1)", "open.__class__", "close[-1] > 0"):
        with pytest.raises(dsl.DSLError):
            dsl.signal(bad, _ctx())


def test_signals_are_causal() -> None:
    full, cut = _ctx(600), _ctx(600)
    for k in ("open", "high", "low", "close", "volume", "session", "end_ns"):
        setattr(cut, k, getattr(cut, k)[:400])
    cut.cache.clear()
    expr = "cross_above(ema(close, 9), ema(close, 21)) and rsi(close, 14) > 50 and close > swing_high(3)"
    assert np.array_equal(dsl.signal(expr, full)[:400], dsl.signal(expr, cut))


def test_chained_comparison_and_params() -> None:
    ctx = _ctx()
    a = dsl.signal("close > sma(close, $n) > sma(close, 50)", ctx, {"n": 20})
    b = dsl.signal("close > sma(close, 20) and sma(close, 20) > sma(close, 50)", ctx)
    assert np.array_equal(a, b)


def test_shipped_library_covers_every_catalogue_reference(tmp_path: Path) -> None:
    from stock_gplm.app.m56_catalogue import load_catalogue

    # 2026-11-20 revamp: dead/rare/blocked translations live in config/strategies/parked (not loaded by runs);
    # every reference must still be active or parked, and parked files must stay valid for restore.
    lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], tmp_path / "w").load()
    parked = StrategyLibrary([REPO / "config" / "strategies" / "parked"], tmp_path / "p").load()
    assert not lib.invalid and not parked.invalid
    assert not set(lib.strategies) & set(parked.strategies)
    refs = {r.reference_id for r in load_catalogue(REPO / "config" / "strategies" /
                                                     "reference-300-strategies.txt").references}
    assert refs <= {s.reference_id for s in [*lib.strategies.values(), *parked.strategies.values()]}


def test_config_only_strategy_versioning_edit_restore(tmp_path: Path) -> None:
    w = tmp_path / "w"
    src = tmp_path / "mine.json"
    src.write_text(json.dumps({"id": "USR-ONE", "origin": "USER_DEFINED", "name": "one-stock rule",
                               "entry": "close > highest(high, 10)[1]", "exit": {"max_hold_bars": 5, "stop_atr": 2},
                               "applicability": {"symbols": ["RELIANCE"], "timeframes": ["1d"]}}))
    lib = StrategyLibrary([], w)
    lib.add_file(src)
    s = lib.load().strategies["USR-ONE"]
    assert s.applies("RELIANCE-EQ", "1d") and not s.applies("TCS-EQ", "1d") and s.version == 1
    lib.copy_for_edit("USR-ONE", {"exit.max_hold_bars": 7})
    with pytest.raises(StrategyConfigError):
        lib.copy_for_edit("USR-ONE", {"entry": "close > nosuch(1)"})
    assert lib.load().strategies["USR-ONE"].doc["exit"]["max_hold_bars"] == 7
    assert len(lib.history_of("USR-ONE")) == 2


def test_discoveries_are_deduplicated(tmp_path: Path) -> None:
    lib = StrategyLibrary([], tmp_path)
    doc = {"id": "ML-X-1d-a", "origin": "ML_DISCOVERED", "name": "x", "entry": "rsi(close, 2) < 10",
           "exit": {"max_hold_bars": 5}, "applicability": {"symbols": ["X"], "timeframes": ["1d"]}}
    assert lib.save_discovered(doc) == ("ML-X-1d-a", True)
    assert lib.save_discovered({**doc, "id": "ML-X-1d-b"}) == ("ML-X-1d-a", False)


def test_rolling_median() -> None:
    ctx = _ctx(30)
    med = dsl.evaluate(dsl.parse("median(close, 5)"), ctx)
    assert np.isnan(med[:4]).all() and abs(med[10] - np.median(ctx.close[6:11])) < 1e-9
