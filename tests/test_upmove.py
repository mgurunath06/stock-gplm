"""Offline checks of the up-move learner core (synthetic candles)."""
import numpy as np

from stock_gplm.app.upmove import Params, firings, outcomes, summarize, walk_forward


def test_outcomes_target_stop_timeout_and_stop_first():
    p = Params(target=0.03, stop=0.015, horizon=3)
    o = np.array([100, 100, 100, 100, 100, 100, 100.0])
    h = np.array([100, 104, 100, 100, 104, 100, 100.0])   # t=0 -> bar1 hits +3% (target)
    lo = np.array([100, 99.9, 98, 100, 98, 100, 100.0])   # t=1 -> bar2 hits -1.5% (stop); t=3 -> bar4 both: stop first
    c = np.array([100, 100, 100, 100, 100, 100, 101.0])
    hit, net = outcomes(o, h, lo, c, p, cost=0.001)
    assert hit[0] == 1 and abs(net[0] - 0.029) < 1e-9
    assert hit[1] == 0 and abs(net[1] + 0.016) < 1e-9
    assert hit[3] == 0 and abs(net[3] + 0.016) < 1e-9
    assert np.isnan(net[4]) and np.isnan(net[6])          # no full 3-candle path left


def test_firings_edge_and_spacing():
    m = np.array([0, 1, 1, 0, 1, 0, 0, 0, 1, 1], bool)
    assert firings(m, np.ones(10, bool), 5).tolist() == [1, 8]


def test_edge_ranks_better_trigger_and_walk_forward_is_causal():
    p = Params(prior=2)
    w = np.ones(20)
    good = summarize(np.r_[np.full(12, 0.03), np.full(8, -0.015)], np.r_[np.ones(12), np.zeros(8)], w, 0.35, p)
    bad = summarize(np.r_[np.full(5, 0.03), np.full(15, -0.015)], np.r_[np.ones(5), np.zeros(15)], w, 0.35, p)
    assert good["edge"] > 0 > bad["edge"] and good["rr"] > 1.9
    t = np.arange(0, 400, 10)
    net = np.where(np.arange(t.size) % 2 == 0, 0.03, -0.015)
    hit = (net > 0).astype(float)
    a = walk_forward(t, net, hit, np.full(t.size, 0.35), p, 400)
    net2 = net.copy()
    net2[-5:] = -0.015  # changing later results must not change which earlier firings were taken
    b = walk_forward(t, net2, hit, np.full(t.size, 0.35), p, 400)
    assert a["taken_t"][: len(a["taken_t"]) - 5] == b["taken_t"][: len(a["taken_t"]) - 5]


def test_gap_through_stop_fills_at_open_and_atr_levels():
    p = Params(target=0.03, stop=0.015, horizon=3)
    o = np.array([100, 100, 95, 100, 100.0])                # t=0: entry 100, bar2 opens at 95 (gap below the 98.5 stop)
    h = np.array([100, 100.5, 96, 100, 100.0])
    lo = np.array([100, 99.0, 94, 100, 100.0])
    c = np.array([100, 100, 95, 100, 100.0])
    hit, net = outcomes(o, h, lo, c, p, cost=0.0)
    assert hit[0] == 0 and abs(net[0] + 0.05) < 1e-9        # filled at the 95 open, not the 98.5 stop
    hit2, _ = outcomes(o, h, lo, c, p, 0.0, target=np.full(5, 0.004), stop=np.full(5, 0.5))
    assert hit2[0] == 1                                     # per-candle (ATR-style) target of 0.4% hit on bar1


def test_session_rules_square_off_1515_and_last_entry_1445():
    import pandas as pd
    from stock_gplm.m54.backtest import LaneMarket

    ends = pd.date_range("2026-11-02 09:30", "2026-11-02 15:30", freq="15min", tz="Asia/Kolkata")
    n = len(ends)
    z = np.ones(n)
    m = LaneMarket("15m", np.array(["2026-11-02"] * n), np.zeros(n, np.int64), ends.asi8, z, z, z, z, z, z,
                   np.zeros(n, bool), np.zeros(n, bool), True, 252 * 25)
    labels = [m.label(i)[-5:] for i in range(n)]
    assert labels[int(m.exit_last[0])] == "15:15" and labels[m.session_last[0]] == "15:30"
    assert [lb for lb, ok in zip(labels, m.entry_ok) if ok][-1] == "14:45"


def test_short_side_mirrors_target_stop_and_gap():
    p = Params(target=0.03, stop=0.015, horizon=3, side="short")
    o = np.array([100, 100, 100, 103, 100, 100.0])
    h = np.array([100, 100.5, 100, 103.5, 100, 100.0])
    lo = np.array([100, 96.5, 100, 102, 100, 100.0])        # t=0: bar1 falls 3.5% -> short target
    c = np.array([100, 97, 100, 103, 100, 100.0])
    hit, net = outcomes(o, h, lo, c, p, cost=0.0)
    assert hit[0] == 1 and abs(net[0] - 0.03) < 1e-9
    assert hit[1] == 0 and abs(net[1] + 0.03) < 1e-9        # t=1: entry 100, bar3 gaps up to 103 -> stop filled at open
