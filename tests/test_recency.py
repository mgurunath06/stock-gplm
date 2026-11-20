"""Offline checks of the recency walk-forward core (synthetic trades, no data files)."""
import numpy as np

from stock_gplm.app.recency import Policy, Window, lane_status, verdict, walk


def _trades(rng, n_bars, variants):
    """variants: list of callables day -> mean net. One trade every 2 bars per variant, held 1 bar."""
    vid, t, e, net = [], [], [], []
    for v, f in enumerate(variants):
        for b in range(0, n_bars - 2, 2):
            vid.append(v)
            t.append(b)
            e.append(b + 1)
            net.append(f(b) + rng.normal(0, 0.002))
    return [np.asarray(x) for x in (vid, t, e, net)]


def test_persistent_winner_found_and_no_lookahead():
    rng = np.random.default_rng(1)
    n = 400
    day = np.arange(n, dtype=np.int64) + 19000
    win, pol = Window(30, 7, 5), Policy(top_k=1, min_steps=4, min_fwd_trades=10)
    vid, t, e, net = _trades(rng, n, [lambda b: 0.004, lambda b: -0.002, lambda b: 0.0])
    group = np.arange(3)
    w = walk(vid, t, e, net, day, group, win, pol)
    assert w["steps"] and all(s["picks"] == [0] for s in w["steps"])
    tr = verdict(w["steps"], pol)
    assert tr["verdict"] == "WORKS" and lane_status(tr) == "ASSIGNED"
    assert w["current"] == [0]

    # Changing the future must not change any earlier decision.
    net2 = net.copy()
    net2[t >= 300] = -net2[t >= 300]
    w2 = walk(vid, t, e, net2, day, group, win, pol)
    early = [s["picks"] for s in w["steps"] if s["date"] <= str(np.datetime64(int(day[299]), "D"))]
    early2 = [s["picks"] for s in w2["steps"] if s["date"] <= str(np.datetime64(int(day[299]), "D"))]
    assert early == early2


def test_regime_flip_fails_and_rotates():
    rng = np.random.default_rng(2)
    n = 400
    day = np.arange(n, dtype=np.int64) + 19000
    win, pol = Window(30, 7, 5), Policy(top_k=1)
    # variant 0 wins early then loses; variant 1 the opposite -> the pick must rotate to 1.
    vid, t, e, net = _trades(rng, n, [lambda b: 0.004 if b < 200 else -0.004,
                                      lambda b: -0.004 if b < 200 else 0.004])
    w = walk(vid, t, e, net, day, np.arange(2), win, pol)
    assert w["steps"][0]["picks"] == [0] and w["steps"][-1]["picks"] == [1]
    assert w["current"] == [1]


def test_random_noise_is_not_assigned():
    rng = np.random.default_rng(3)
    n = 400
    day = np.arange(n, dtype=np.int64) + 19000
    pol = Policy(top_k=3)
    vid, t, e, net = _trades(rng, n, [lambda b: -0.001] * 20)
    w = walk(vid, t, e, net, day, np.arange(20), Window(30, 7, 5), pol)
    assert verdict(w["steps"], pol)["verdict"] != "WORKS"
