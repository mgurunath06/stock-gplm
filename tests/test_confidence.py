"""Confidence / RR / Edge per stock x TF x strategy and ACTIVE thresholds (owner 2026-11-20)."""
import pandas as pd

from stock_gplm.app.confidence import active_map, score, thresholds


def _trades(rows):
    return pd.DataFrame(rows, columns=["stock", "timeframe", "strategy_id", "name", "side", "signal", "net_pct"])


def test_confidence_shrinks_few_trades_and_thresholds_per_stock_tf():
    rows = [("RELIANCE", "1d", "A", "a", "long", f"2026-03-{d:02d}", 2.0 if d % 3 else -1.0) for d in range(1, 31)]
    rows += [("RELIANCE", "1d", "B", "b", "long", "2026-03-05", 5.0), ("RELIANCE", "1d", "B", "b", "long", "2026-03-06", 5.0)]
    rows += [("RELIANCE", "1d", "C", "c", "long", f"2026-03-{d:02d}", -1.0) for d in range(1, 15)]
    doc = {"default": {"min_confidence": 0.45, "min_edge": 0.0, "min_trades": 10},
           "stocks": {"RELIANCE": {"1d": {"min_confidence": 0.6}}}}
    sc = score(_trades(rows), doc).set_index("strategy_id")
    # B: 2 trades, both winners -> pulled toward the base rate, not 100%
    assert sc.loc["B", "win_rate"] == 1.0 and sc.loc["B", "confidence"] < 0.8
    assert sc.loc["B", "status"] == "INACTIVE" and "trades 2 < 10" in sc.loc["B", "why"]
    # A: ~67% winners with RR ~2 -> edge positive and above the RELIANCE 1d confidence floor of 60%
    assert sc.loc["A", "edge"] > 0 and sc.loc["A", "status"] == "ACTIVE" and sc.loc["A", "min_confidence"] == 0.6
    assert sc.loc["C", "status"] == "INACTIVE"
    assert active_map(sc.reset_index()) == {"RELIANCE": {"1d": ["A"]}}
    assert thresholds(doc, "INFY-EQ", "5m")["min_confidence"] == 0.45
