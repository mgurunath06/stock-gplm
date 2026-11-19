"""M6.3 — PRD opportunity horizons, learning targets and baselines.

Seven horizon buckets (PRD §3). Each has a documented label-maturity rule, a cost profile and an
evaluation method; the defaults live in :data:`DEFAULT_BUCKETS` and may be overridden by the config
section ``horizon_buckets``.

Labels for a decision at bar ``t`` with a holding period of ``h`` bars (entry at the open of ``t+1``,
the same convention as every backtest):

* ``gross`` / ``net`` -- forward return to the exit (net uses the lane's per-side costs);
* ``direction`` -- UP / DOWN / NEUTRAL using a cost-aware neutral band; ``NO_TRADE`` when |net| does
  not clear the band;
* ``mfe`` / ``mae`` -- maximum favourable / adverse excursion over the holding path;
* ``outcome`` -- TARGET / STOP / TIME (ATR target/stop; stop is checked first when both are touched
  on the same bar = conservative; ``INDETERMINATE`` is recorded when both touch on the entry bar);
* ``bars_held``.

A label is ``PENDING`` until its exit bar has closed (and, for weekly/monthly lanes, until that bar is
final); pending and invalid labels must never update a model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

HORIZON_CONTRACT = "stock-gplm.horizons.m63.v1"
MATURE, PENDING, INVALID = "MATURE", "PENDING", "INVALID"

DEFAULT_BUCKETS: dict[str, dict[str, Any]] = {
    "intraday": {"timeframe": "15m", "bars": [4, 8, 24], "cost_profile": "intraday", "session_bound": True,
                 "maturity": "exit bar closed; positions never cross the session close (SESSION_CLOSE exit)",
                 "evaluation": "purged walk-forward by session, embargo 1 session"},
    "d1_7": {"timeframe": "1d", "bars": [1, 3, 5, 7], "cost_profile": "delivery",
             "maturity": "exit session closed (daily candle published)", "evaluation": "purged walk-forward, embargo = horizon"},
    "d7_15": {"timeframe": "1d", "bars": [10, 15], "cost_profile": "delivery",
              "maturity": "exit session closed", "evaluation": "purged walk-forward, embargo = horizon"},
    "d15_30": {"timeframe": "1d", "bars": [20, 30], "cost_profile": "delivery",
               "maturity": "exit session closed", "evaluation": "purged walk-forward, embargo = horizon"},
    "m1_3": {"timeframe": "1w", "bars": [4, 8, 13], "cost_profile": "delivery",
             "maturity": "exit week final (registered calendar proves every session present)",
             "evaluation": "purged walk-forward, embargo = horizon; small samples reported"},
    "m3_6": {"timeframe": "1w", "bars": [13, 26], "cost_profile": "delivery",
             "maturity": "exit week final", "evaluation": "purged walk-forward; few independent samples (disclosed)"},
    "m6_12": {"timeframe": "1mo", "bars": [6, 12], "cost_profile": "delivery",
              "maturity": "exit month final (registered calendar)",
              "evaluation": "expanding-window; very few independent samples (always disclosed, never sole evidence)"},
}


def buckets(cfg: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    out = {k: dict(v) for k, v in DEFAULT_BUCKETS.items()}
    for k, v in (cfg or {}).items():
        out.setdefault(k, {}).update(v)
    return out


@dataclass(frozen=True)
class LabelSpec:
    horizon: int
    stop_atr: float | None = 2.0
    target_atr: float | None = 3.0
    band: float = 0.0  # neutral band on |net return|
    session_bound: bool = False


def labels(m: Any, spec: LabelSpec, buy: float, sell: float, final: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Vectorised-per-bar labels for a :class:`~stock_gplm.m54.backtest.LaneMarket`-like object ``m``.

    ``final[i]`` False marks bars that are not final (incomplete weekly/monthly bars); a label whose
    path touches a non-final bar is PENDING. Blocked bars make the label INVALID."""
    n = len(m.close)
    h = int(spec.horizon)
    out = {k: np.full(n, np.nan) for k in ("gross", "net", "mfe", "mae", "bars_held", "exit_price")}
    outcome = np.full(n, "", dtype="U13")
    status = np.full(n, PENDING, dtype="U8")
    direction = np.full(n, "", dtype="U8")
    final = np.ones(n, bool) if final is None else np.asarray(final, bool)
    sid = getattr(m, "session_id", None)
    for t in range(n):
        e_bar = t + 1
        if e_bar >= n:
            continue
        last = t + h
        if spec.session_bound and sid is not None:
            sess_last = e_bar
            while sess_last + 1 < n and sid[sess_last + 1] == sid[e_bar]:
                sess_last += 1
            if sid[e_bar] != sid[t]:
                status[t] = INVALID
                outcome[t] = "CROSSES_SESSION"
                continue
            if last > sess_last:
                if sess_last + 1 >= n:
                    continue  # session not finished in the data: PENDING
                last = sess_last  # flat at the session close
        if last >= n:
            continue
        if not final[e_bar:last + 1].all():
            continue
        if m.blocked[e_bar:last + 1].any():
            status[t] = INVALID
            outcome[t] = "BLOCKED_PATH"
            continue
        entry = float(m.open[e_bar])
        atr = float(m.natr[t]) * entry if math.isfinite(float(m.natr[t])) else math.nan
        stop = entry - spec.stop_atr * atr if spec.stop_atr and math.isfinite(atr) else -math.inf
        tgt = entry + spec.target_atr * atr if spec.target_atr and math.isfinite(atr) else math.inf
        px, why, ex = float(m.close[last]), "TIME", last  # time exit at the close of the horizon bar
        for j in range(e_bar, last + 1):
            lo, hi, op = float(m.low[j]), float(m.high[j]), float(m.open[j])
            hit_s, hit_t = lo <= stop, hi >= tgt
            if j > e_bar and op <= stop:
                px, why, ex = op, "STOP_GAP", j
                break
            if hit_s and hit_t:
                px, why, ex = stop, "STOP" if j > e_bar else "INDETERMINATE", j
                break
            if hit_s:
                px, why, ex = stop, "STOP", j
                break
            if j > e_bar and op >= tgt:
                px, why, ex = op, "TARGET_GAP", j
                break
            if hit_t:
                px, why, ex = tgt, "TARGET", j
                break
        path_h = float(np.nanmax(m.high[e_bar:ex + 1]))
        path_l = float(np.nanmin(m.low[e_bar:ex + 1]))
        g = px / entry - 1
        nr = px * (1 - sell) / (entry * (1 + buy)) - 1
        out["gross"][t], out["net"][t] = g, nr
        out["mfe"][t], out["mae"][t] = path_h / entry - 1, path_l / entry - 1
        out["bars_held"][t], out["exit_price"][t] = ex - t, px
        outcome[t] = why
        status[t] = MATURE
        if abs(nr) <= spec.band:
            direction[t] = "NO_TRADE" if abs(g) > spec.band else "NEUTRAL"
        else:
            direction[t] = "UP" if nr > 0 else "DOWN"
    return {**out, "outcome": outcome, "status": status, "direction": direction}


def baselines(net: np.ndarray, status: np.ndarray, train_end: int, test: tuple[int, int]) -> dict[str, Any]:
    """Simple baselines on the test segment: flat (0), always-long (mean net of every mature label) and the
    training base rate as a probability forecast (Brier). Only labels mature by ``train_end`` train it."""
    a, b = test
    mt = status[:train_end] == MATURE
    te = (status[a:b] == MATURE)
    y_tr = net[:train_end][mt]
    y_te = net[a:b][te]
    if y_te.size == 0:
        return {"n_test": 0}
    p = float((y_tr > 0).mean()) if y_tr.size else 0.5
    up = (y_te > 0).astype(float)
    return {"n_train": int(y_tr.size), "n_test": int(y_te.size),
            "flat_mean_net": 0.0, "always_long_mean_net": round(float(y_te.mean()), 6),
            "always_long_win_rate": round(float(up.mean()), 4), "base_rate_p_up": round(p, 4),
            "base_rate_brier": round(float(((p - up) ** 2).mean()), 6)}


def label_summary(lab: Mapping[str, np.ndarray]) -> dict[str, Any]:
    st = lab["status"]
    mature = st == MATURE
    net = lab["net"][mature]
    oc, cnt = np.unique(lab["outcome"][mature], return_counts=True)
    dr, dc = np.unique(lab["direction"][mature], return_counts=True)
    return {"labels": int(len(st)), "mature": int(mature.sum()), "pending": int((st == PENDING).sum()),
            "invalid": int((st == INVALID).sum()),
            "mean_net": round(float(net.mean()), 6) if net.size else None,
            "mean_mfe": round(float(np.nanmean(lab["mfe"][mature])), 6) if net.size else None,
            "mean_mae": round(float(np.nanmean(lab["mae"][mature])), 6) if net.size else None,
            "outcomes": {str(k): int(v) for k, v in zip(oc, cnt)},
            "directions": {str(k): int(v) for k, v in zip(dr, dc)}}


__all__ = ["DEFAULT_BUCKETS", "HORIZON_CONTRACT", "INVALID", "LabelSpec", "MATURE", "PENDING", "baselines", "buckets",
           "label_summary", "labels"]
