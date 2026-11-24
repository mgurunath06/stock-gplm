"""Small deterministic checks for owner_plans (retest ordering, swing-stop selection, EMA200 / EMA20 exits)."""
import numpy as np

from stock_gplm.app.owner_plans import (Bars, order_in_bar, pullback_low, retest_fill, simulate_plan,
                                        swing_stop)

TF = 15 * 60 * 10 ** 9
CFG = {"management": {"target_r": 2.0, "partial_frac": 0.5, "max_hold_bars": 100},
       "costs": {"slip_bps": 0.0, "stop_slip_bps": 0.0}}


def mk(o, h, l, c, e13=None, e20=None, e200=None, **kw):
    n = len(c)
    f = lambda x, d: np.asarray(x if x is not None else [d] * n, float)  # noqa: E731
    return Bars(f(o, 0), f(h, 0), f(l, 0), f(c, 0), np.ones(n), f(e13, 0), f(e20, 0), f(e200, 1e9), np.ones(n),
                np.zeros(n, np.int64), np.arange(1, n + 1, dtype=np.int64) * TF, [f"t{i}" for i in range(n)],
                0.0, 0.0, TF, **kw)


def test_retest_fills_before_candle_close_undoes_cross():
    # cross at t=0; candle 1 dips to EMA13[0]=100 and closes with EMA13 <= EMA20 (old code cancelled it)
    B = mk(o=[101, 102, 99], h=[102, 103, 100], l=[100, 99.5, 98], c=[101.5, 99.8, 99],
           e13=[100, 99.9, 99.5], e20=[99.5, 100, 99.8])
    assert retest_fill(B, 0, 95.0, 5) == (1, 100.0, "filled")


def test_retest_cancels_on_completed_candle_only():
    # candle 1 does not reach the limit and completes with EMA13 <= EMA20 -> cancelled at candle 2's open
    B = mk(o=[101, 102, 101], h=[102, 103, 101], l=[100.5, 101, 90], c=[101.5, 102, 95],
           e13=[100, 99.9, 99.5], e20=[99.5, 100, 99.8])
    j, px, st = retest_fill(B, 0, 95.0, 5)
    assert (j, st) == (1, "cancel_undo") and np.isnan(px)


def test_retest_gap_below_limit_fills_at_open():
    B = mk(o=[101, 99], h=[102, 99.5], l=[100.5, 98], c=[101.5, 99], e13=[100, 100], e20=[99, 99])
    assert retest_fill(B, 0, 95.0, 5) == (1, 99.0, "filled")


def test_swing_stop_uses_only_confirmed_pivots():
    lows = [12, 11, 10, 9, 8, 9, 10, 11, 10, 9.5, 10, 9.8, 7.0]  # candle 12 is lowest but unconfirmed at t=12
    B = mk(o=lows, h=[x + 1 for x in lows], l=lows, c=[x + 0.5 for x in lows])
    lvl, i = swing_stop(B, 12, 2, 50, ref=20.0)
    assert i == 9 and lvl == 9.5  # last pivot with 2 completed candles each side
    lvl, i = swing_stop(B, 12, 3, 50, ref=20.0)
    assert i == 4 and lvl == 8.0  # k=3: candle 9 is not a 3-each-side pivot


def test_pullback_low_is_lowest_of_last_ema13_below_ema20_run():
    e13 = [5, 5, 1, 1, 1, 5, 5, 1, 1, 5]
    e20 = [3] * 10
    lows = [9, 8, 7, 6, 7, 8, 9, 7.5, 8, 8.5]
    B = mk(o=lows, h=lows, l=lows, c=lows, e13=e13, e20=e20)
    assert pullback_low(B, 9, 50) == (7.5, 7, 7)


def test_ema200_target_live_and_ema20_touch_never_loosens():
    # entry 100 at candle 1, stop 98; EMA200 target from the previous completed candle
    B = mk(o=[100, 100, 101, 102], h=[100, 101, 103, 104], l=[99, 99.5, 100.5, 101], c=[100, 100.8, 102.5, 103],
           e20=[99, 99, 99, 99], e200=[104, 103, 102.9, 102])
    x = simulate_plan(B, 0, (1, 100.0, "open"), 98.0, "T200", CFG)
    # candle 1 targets EMA200[0]=104 (not reached); candle 2 targets EMA200[1]=103 -> hit, +3 / 2 = 1.5R
    assert x["exit_j"] == 2 and x["reason"] == "TARGET" and abs(x["net_r"] - 1.5) < 1e-9
    # EMA20 touch trail: EMA20 rises to 101 then falls to 99 - the stop stays at 101
    B = mk(o=[100, 100, 102, 103, 102], h=[100, 102, 104, 104, 102.5], l=[99, 99.5, 101.5, 101.2, 100.5],
           c=[100, 101.5, 103.5, 102, 101], e20=[99, 99, 101, 99, 99])
    x = simulate_plan(B, 0, (1, 100.0, "open"), 98.0, "TRAIL_TOUCH", CFG)
    assert x["exit_j"] == 4 and x["reason"] == "TRAIL" and abs(x["net_r"] - 0.5) < 1e-9


def test_trail_close_exits_next_open():
    B = mk(o=[100, 100, 101, 99.5], h=[100, 102, 101.5, 100], l=[99, 99.5, 99.2, 99], c=[100, 101, 99.4, 99.8],
           e20=[99, 99.5, 100, 100])
    x = simulate_plan(B, 0, (1, 100.0, "open"), 98.0, "TRAIL_CLOSE", CFG)
    assert x["exit_j"] == 3 and x["reason"] == "EMA_CLOSE" and abs(x["net_r"] + 0.25) < 1e-9


def test_same_candle_stop_and_target_stop_first_unless_lower_tf():
    B = mk(o=[100, 100], h=[100, 105], l=[99, 97], c=[100, 101])
    x = simulate_plan(B, 0, (1, 100.0, "open"), 98.0, "T2R", CFG)
    assert x["reason"] == "STOP" and x["ambiguous"] == 1
    # 5m candles inside candle 1: high 105 first, then the low
    lo_end = TF + np.array([1, 2, 3]) * 5 * 60 * 10 ** 9  # three 5m candles ending inside 15m candle 1
    B = mk(o=[100, 100], h=[100, 105], l=[99, 97], c=[100, 101], lo_h=np.array([105, 101, 101.0]),
           lo_l=np.array([99.5, 97, 99.0]), lo_end=lo_end)
    assert order_in_bar(B, 1, 98.0, 104.0) == "target"
    x = simulate_plan(B, 0, (1, 100.0, "open"), 98.0, "T2R", CFG)
    assert x["reason"] == "TARGET" and x["lower_tf"] == 1 and abs(x["net_r"] - 2.0) < 1e-9
