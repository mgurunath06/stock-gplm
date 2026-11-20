"""Owner swing exit (2026-11-20): stop 0.2% beyond the last swing low/high, target 2x risk."""
import numpy as np

from stock_gplm.app.strategy_engine import Exit, swing_level


def test_swing_low_is_latest_confirmed_pivot_below_entry():
    low = np.array([10, 9, 8, 9, 10, 9.5, 9.2, 9.6, 10, 10.5, 11.0])
    high = low + 1
    # pivots: 8 at k=2, 9.2 at k=6; at t=10 the latest confirmed pivot below entry 11 is 9.2
    assert swing_level(low, high, 10, 11.0, False, 20) == 9.2
    # entry below the pivot (gap down): next older pivot below entry (8.0) is used
    assert swing_level(low, high, 10, 9.0, False, 20) == 8.0
    # nothing below entry at all -> no trade
    assert swing_level(low, high, 10, 7.0, False, 20) is None


def test_swing_high_for_shorts_and_fallback():
    high = np.array([10, 11, 12, 11, 10, 10.5, 10.8, 10.4, 10.0, 9.8, 9.5])
    low = high - 1
    assert swing_level(low, high, 10, 9.4, True, 20) == 10.8
    # no pivot in a 3-bar lookback -> highest high of the last 5 bars (10.8 at t-4)
    assert swing_level(low, high, 10, 9.4, True, 3) == 10.8


def test_exit_from_profile():
    ex = Exit.of({"max_hold_bars": 25, "swing_stop": {"buffer": 0.002, "rr": 2.0, "lookback": 20}})
    assert (ex.swing_buffer, ex.swing_rr, ex.swing_lookback) == (0.002, 2.0, 20)
    assert "swing:0.002:2:20" in ex.key()
    assert "0.2% below the last swing low, target 2x risk" in ex.text(None, False)
    assert Exit.of({"max_hold_bars": 10, "stop_atr": 2.0}).swing_buffer is None


def test_per_stock_settings_precedence_and_candle_fallback_field(tmp_path):
    import json
    from stock_gplm.app.strategy_engine import swing_settings
    f = tmp_path / "exits.json"
    f.write_text(json.dumps({"default": {"buffer": 0.002, "rr": 2.0, "lookback": 20, "max_swing_pct": 0.025},
                             "stocks": {"RELIANCE": {"*": {"rr": 3.0}, "1d": {"max_swing_pct": 0.04, "learned": {}}}}}))
    assert swing_settings("RELIANCE-EQ", "1d", f) == {"buffer": 0.002, "rr": 3.0, "lookback": 20, "max_swing_pct": 0.04}
    assert swing_settings("RELIANCE", "5m", f)["max_swing_pct"] == 0.025
    assert swing_settings("INFY", "1d", f)["rr"] == 2.0
    ex = Exit.of({"max_hold_bars": 25, "swing_stop": {"buffer": 0.002, "rr": 2.0, "lookback": 20, "max_swing_pct": 0.025}})
    assert ex.swing_max == 0.025 and ":max0.025" in ex.key()
    assert "signal candle low when the swing is more than 2.5% away" in ex.text(None, False)


def test_strategy_level_setting_wins(tmp_path):
    import json
    from stock_gplm.app.strategy_engine import swing_settings
    f = tmp_path / "exits.json"
    f.write_text(json.dumps({"default": {"buffer": 0.002, "rr": 2.0, "lookback": 20, "max_swing_pct": 0.025},
                             "stocks": {"RELIANCE": {"1d": {"rr": 1.5, "max_swing_pct": 0.01, "strategies": {
                                 "O2-066": {"rr": 3.0, "max_swing_pct": None, "learned": {"trades": 9}}}}}}}))
    assert swing_settings("RELIANCE", "1d", f, strategy_id="O2-066") == {
        "buffer": 0.002, "rr": 3.0, "lookback": 20, "max_swing_pct": None}
    assert swing_settings("RELIANCE", "1d", f, strategy_id="O2-001")["rr"] == 1.5
