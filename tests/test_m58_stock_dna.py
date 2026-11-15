"""Deterministic synthetic tests for Stock DNA discovery."""
from __future__ import annotations

import numpy as np
import pytest
import pandas as pd

from stock_gplm.app.ingest import SymbolData, window_data
from stock_gplm.app.lanes import all_bars, build_lane
from stock_gplm.app.stock_dna import discover_lane
from stock_gplm.verify import fixtures as fx


COSTS = {"delivery": {"commission_bps": 0, "slippage_bps": 0, "buy_statutory_bps": 0,
                       "sell_statutory_bps": 0},
         "intraday": {"commission_bps": 0, "slippage_bps": 0, "buy_statutory_bps": 0,
                      "sell_statutory_bps": 0}}


def _data(n: int = 520) -> SymbolData:
    frame = fx.ohlcv(n, seed=818)
    daily = frame.rename(columns={"session": "date"}).drop(columns="end_ns")
    daily["source"] = "SYNTHETIC"
    return SymbolData("SYN-EQ", daily, pd.DataFrame(), "synthetic-fp", {"minute_bars": 0})


def _lane(data: SymbolData):
    bars = all_bars(data)
    return build_lane(data, "1d", COSTS, bars=bars)


def test_stock_dna_is_deterministic_quantified_and_fdr_classified() -> None:
    lane = _lane(_data())
    assert lane is not None
    cfg = {"discovery_fraction": 0.6, "quantiles": [0.2, 0.8], "max_features": 12,
           "min_validation_observations": 8, "fdr_q": 0.10}
    a = discover_lane(lane, cfg, [1])
    b = discover_lane(lane, cfg, [1])
    assert a == b and a["patterns"]
    p = a["patterns"][0]
    assert {"observations", "successes", "success_rate", "success_ci95_low", "baseline_success_rate",
            "mean_forward_gross_return", "mean_forward_long_net_return", "p_value", "q_value", "regimes",
            "currently_holds", "status"} <= set(p)
    assert all(x["status"] in {"VALIDATED", "EXPERIMENTAL", "REJECTED", "INSUFFICIENT_EVIDENCE"}
               for x in a["patterns"])


def test_stock_dna_cutoff_is_unchanged_when_future_candles_are_mutated() -> None:
    original = _data()
    cutoff = str(original.daily["date"].iloc[419])
    mutated = _data()
    mutated.daily.loc[420:, ["open", "high", "low", "close", "volume"]] *= 7.0
    left = _lane(window_data(original, end=cutoff))
    right = _lane(window_data(mutated, end=cutoff))
    assert left is not None and right is not None
    cfg = {"discovery_fraction": 0.6, "quantiles": [0.2, 0.8], "max_features": 10,
           "min_validation_observations": 8, "fdr_q": 0.10}
    assert discover_lane(left, cfg, [1])["patterns"] == discover_lane(right, cfg, [1])["patterns"]


REAL_COSTS = {"delivery": {"commission_bps": 3.0, "slippage_bps": 5.0, "buy_statutory_bps": 12.41,
                            "sell_statutory_bps": 10.91},
              "intraday": {"commission_bps": 3.0, "slippage_bps": 5.0, "buy_statutory_bps": 3.0,
                           "sell_statutory_bps": 3.0}}


def test_pattern_direction_is_cost_invariant_and_not_biased_to_down() -> None:
    """Regression: v1 labelled UP as 'beats round-trip cost', so nearly every pattern became DOWN."""
    data = _data()
    free = build_lane(data, "1d", COSTS, bars=all_bars(data))
    costly = build_lane(data, "1d", REAL_COSTS, bars=all_bars(data))
    cfg = {"discovery_fraction": 0.6, "quantiles": [0.2, 0.8], "max_features": 40,
           "min_validation_observations": 8, "fdr_q": 0.10}
    a, b = discover_lane(free, cfg, [1]), discover_lane(costly, cfg, [1])
    key = lambda r: (r["pattern_id"], r["direction"], r["success_rate"], r["status"])
    assert [key(r) for r in a["patterns"]] == [key(r) for r in b["patterns"]]
    directions = {r["direction"] for r in b["patterns"] if r["observations"]}
    assert directions == {"UP", "DOWN"}
    r = next(r for r in b["patterns"] if r["observations"])
    assert r["mean_forward_long_net_return"] == pytest.approx(r["mean_forward_gross_return"] - r["round_trip_cost"])
    assert r["round_trip_cost"] == pytest.approx(0.003932)
