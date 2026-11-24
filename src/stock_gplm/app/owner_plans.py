"""Owner EMA13 x EMA20 trade-plan research (owner 2026-11-24): when is the cross worth trading, and which entry /
stop / target / trailing plan fits the price action at that moment.

    $env:PYTHONPATH="src"; python -m stock_gplm.app.owner_plans --symbol RELIANCE
    ... --stocks 200 --workers 20      (first 200 of universe.txt + RELIANCE; per-stock checkpoints; --fresh redoes)

Steps (config/research/owner_plans.json):
1. AUDIT vs the owner's chart marks (owner_trades*.csv, read-only): candle start IST, OHLC, EMA13/20/200, our cross
   (exact / one-bar / none), fill, stop candle under both stop definitions, exit; data checks (alignment, bars per
   session, EMA warm-up, EMA13-EMA20 gap at the owner's candle).
2. PLANS: every long cross simulated under entry {NEXT_OPEN, RETEST13} x stop {SWING, PBLOW} x management
   {T2R, T200, PT2R, PT200, TRAIL_TOUCH, TRAIL_CLOSE}; SKIP = 0 R. Only completed-candle information is used for each
   decision. Gaps fill at the open; same-candle stop/target order resolved on lower-TF candles, else stop first
   (flagged). Lane costs + slippage. V2 (old engine version) kept as a named baseline.
3. LEARN (chronological walk-forward, training rows must have CLOSED before the decision; recent rows weigh more):
   RULE selector (cell = near/far EMA13 x above/below EMA200) and MODEL (per-plan regressor of net R). Compared with
   always next open, always retest, fixed 2R, EMA20 trail, V2. Only the plan actually chosen is scored.
4. REPORT  <reports>/tuning/owner-plans-<tf>/REPORT.md (+ decisions.csv, per stock stocks/<SYM>/plans.csv, audit.md).
Times = TradingView candle START (IST). Longs only for now.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "config" / "research" / "owner_plans.json"
OWNER_SPEC = REPO / "config" / "research" / "owner_strategy.json"
TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}
ENTRIES = ("NEXT_OPEN", "RETEST13")
STOPS = ("SWING", "PBLOW")
STOP_REASONS = ("STOP", "STOP_GAP", "TRAIL", "TRAIL_GAP")


# ====================================================================== bars (pure arrays: easy to test)
@dataclass
class Bars:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    e13: np.ndarray
    e20: np.ndarray
    e200: np.ndarray
    atr: np.ndarray
    sid: np.ndarray
    end_ns: np.ndarray
    times: list[str]
    buy: float = 0.0
    sell: float = 0.0
    tf_ns: int = 15 * 60 * 10 ** 9
    lo_h: np.ndarray | None = None  # lower timeframe (5m) highs / lows / candle end times
    lo_l: np.ndarray | None = None
    lo_end: np.ndarray | None = None
    pdh: np.ndarray = field(default=None)  # previous session's high, per candle
    tod: np.ndarray = field(default=None)  # candle number inside its session (0 = first)

    def __post_init__(self) -> None:
        n = len(self.c)
        if self.pdh is None or self.tod is None:
            pdh, tod = np.full(n, np.nan), np.zeros(n, np.int64)
            cur_hi, prev_hi, k = -np.inf, np.nan, 0
            for i in range(n):
                if i and self.sid[i] != self.sid[i - 1]:
                    prev_hi, cur_hi, k = cur_hi, -np.inf, 0
                pdh[i], tod[i] = prev_hi, k
                cur_hi = max(cur_hi, float(self.h[i]))
                k += 1
            self.pdh, self.tod = pdh, tod

    def __len__(self) -> int:
        return len(self.c)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Plain EMA seeded with the first value (tests only; real runs use the DSL's ema via strategy_engine._ema_of)."""
    out = np.empty(len(x))
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


# ====================================================================== decision-time pieces
def crosses(B: Bars, a0: int) -> list[int]:
    """Long crosses: EMA13 above EMA20 at the close of t, not above at t-1 (completed candles)."""
    return [t for t in range(max(a0, 1), len(B)) if B.e13[t] > B.e20[t] and B.e13[t - 1] <= B.e20[t - 1]]


def swing_stop(B: Bars, t: int, k: int, lookback: int, ref: float) -> tuple[float, int] | None:
    """SWING: most recent swing low with k candles on each side, all of them completed by t (pivot i <= t-k),
    within lookback, below ref (the cross close)."""
    for i in range(t - k, max(k, t - lookback) - 1, -1):
        v = float(B.l[i])
        if v < ref and v <= float(B.l[i - k:i + k + 1].min()):
            return v, i
    return None


def pullback_low(B: Bars, t: int, lookback: int) -> tuple[float, int, int] | None:
    """PBLOW: lowest low of the last run of candles with EMA13 < EMA20 (ending before the cross), through the cross
    candle. Returns (level, candle, run start)."""
    k = t
    lo = max(0, t - lookback)
    while k >= lo and not B.e13[k] < B.e20[k]:
        k -= 1
    if k < lo:
        return None
    s = k
    while s - 1 >= lo and B.e13[s - 1] < B.e20[s - 1]:
        s -= 1
    i = s + int(np.argmin(B.l[s:t + 1]))
    return float(B.l[i]), i, s


def retest_fill(B: Bars, t: int, stop: float, max_wait: int) -> tuple[int, float, str]:
    """RETEST13 limit (owner: extended cross -> wait for EMA13). At candle j's OPEN only completed candles are known:
    cancel if candle j-1 closed with EMA13 <= EMA20 (setup undone) or below the stop (invalidated); else a limit at
    EMA13[j-1] works through candle j: gap below -> filled at the open, low touches -> filled at the limit.
    Returns (candle, price, 'filled' | 'cancel_undo' | 'cancel_stop' | 'expired' | 'data_end')."""
    n = len(B)
    for j in range(t + 1, t + 1 + max_wait):
        if j >= n:
            return n - 1, float("nan"), "data_end"
        if j - 1 > t:
            if B.e13[j - 1] <= B.e20[j - 1]:
                return j - 1, float("nan"), "cancel_undo"
            if B.c[j - 1] < stop:
                return j - 1, float("nan"), "cancel_stop"
        lv = float(B.e13[j - 1])
        if B.o[j] <= lv:
            return j, float(B.o[j]), "filled"
        if B.l[j] <= lv:
            return j, lv, "filled"
    return min(n - 1, t + max_wait), float("nan"), "expired"


def _subs(B: Bars, j: int) -> range | None:
    if B.lo_end is None or not len(B.lo_end):
        return None
    a = int(np.searchsorted(B.lo_end, B.end_ns[j] - B.tf_ns, "right"))
    b = int(np.searchsorted(B.lo_end, B.end_ns[j], "right"))
    return range(a, b) if b > a else None


def order_in_bar(B: Bars, j: int, stop: float, target: float, fill_limit: float | None = None) -> str | None:
    """Which comes first inside candle j on the lower timeframe: 'stop' / 'target', or None (no data / still
    ambiguous). fill_limit: the entry was a limit inside candle j, so events count only from its fill (a stop in
    the fill sub-candle is certain - price passes the limit on its way down; a target there is not credited)."""
    idx = _subs(B, j)
    if idx is None:
        return None
    filled = fill_limit is None
    for k in idx:
        lo, hi = float(B.lo_l[k]), float(B.lo_h[k])
        if not filled:
            if lo <= fill_limit:
                filled = True
                if lo <= stop:
                    return "stop"
            continue
        hs, ht = lo <= stop, hi >= target
        if hs and ht:
            return None
        if hs:
            return "stop"
        if ht:
            return "target"
    return None


# ====================================================================== one plan, one cross
def simulate_plan(B: Bars, t: int, fill: tuple[int, float, str], stop: float, mgmt: str, cfg: dict[str, Any]
                  ) -> dict[str, Any]:
    """Long trade from fill (candle, price, how) with initial stop. Management:
    T2R / T200 flat at target; PT2R / PT200 partial at target, rest trailed on EMA20 touch; TRAIL_TOUCH stop =
    max(stop, EMA20 of the previous completed candle) from the candle after entry (never loosens); TRAIL_CLOSE exit
    at the next open after a close below EMA20 (initial stop stays). Same-candle stop+target: lower TF, else stop
    first (ambiguous=1)."""
    mg = cfg["management"]
    cs = cfg["costs"]
    n = len(B)
    j0, E, how = fill
    R = E - stop
    out = {"fill_j": j0, "entry": E, "stop": stop, "risk": R, "legs": [], "ambiguous": 0, "lower_tf": 0,
           "open_end": 0, "fill_bar_target_skipped": 0}
    T2 = E + float(mg.get("target_r", 2.0)) * R
    pf = float(mg.get("partial_frac", 0.5))
    flat = mgmt in ("T2R", "T200")
    partial = mgmt in ("PT2R", "PT200")
    has_target = flat or partial
    trail = stop
    left = 1.0
    part_done = False
    part_bar = -1
    last = min(n - 1, j0 + int(mg.get("max_hold_bars", 400)))
    limit_fill = how == "limit"
    hi_seen, lo_seen = E, E
    j = j0
    done = False
    while j <= last and not done:
        o, h, lo, c = float(B.o[j]), float(B.h[j]), float(B.l[j]), float(B.c[j])
        first = j == j0
        # trailing stop update at the open (previous completed EMA20), never loosens
        if not first and (mgmt == "TRAIL_TOUCH" or (partial and part_done and j > part_bar)):
            trail = max(trail, float(B.e20[j - 1]))
        tgt = None
        if has_target and not part_done:
            tgt = T2 if mgmt in ("T2R", "PT2R") else (float(B.e200[j - 1]) if j - 1 >= t else float(B.e200[t]))
        # MFE/MAE: on a limit-fill candle the part before the fill is unknown - its high is not counted
        hi_seen, lo_seen = max(hi_seen, E if (first and limit_fill) else h), min(lo_seen, lo)
        # gaps at the open (entry candle: the open is the entry for NEXT_OPEN; limit fills handled below)
        if not first:
            if o <= trail:
                out["legs"].append((left, o, "STOP_GAP" if trail <= stop else "TRAIL_GAP"))
                left, done = 0.0, True
                break
            if tgt is not None and o >= tgt:
                out["legs"].append((left if flat else pf, o, "TARGET_GAP" if flat else "PART_GAP"))
                left = 0.0 if flat else left - pf
                if flat:
                    done = True
                    break
                part_done, part_bar, tgt = True, j, None
        hs = lo <= trail
        ht = tgt is not None and h >= tgt
        if ht and first and limit_fill:  # did the high come after the limit fill? only the lower TF can say
            od = order_in_bar(B, j, trail, tgt, fill_limit=E)
            if od == "target":
                out["lower_tf"] = 1
            else:
                ht = False
                out["fill_bar_target_skipped"] = 1
        if hs and ht:
            od = order_in_bar(B, j, trail, tgt, fill_limit=E if (first and limit_fill) else None)
            if od == "target":
                out["lower_tf"] = 1
                hs_first = False
            else:
                hs_first = True
                if od is None:
                    out["ambiguous"] = 1
                else:
                    out["lower_tf"] = 1
            if hs_first:
                out["legs"].append((left, trail, "STOP" if trail <= stop else "TRAIL"))
                left, done = 0.0, True
                break
            # target first: book it; the rest is still exposed to the same candle's stop (conservative)
            if flat:
                out["legs"].append((left, tgt, "TARGET"))
                left, done = 0.0, True
                break
            out["legs"].append((pf, tgt, "PART"))
            left -= pf
            part_done, part_bar = True, j
            out["legs"].append((left, trail, "STOP" if trail <= stop else "TRAIL"))
            left, done = 0.0, True
            out["ambiguous"] = 1
            break
        if hs:
            out["legs"].append((left, trail, "STOP" if trail <= stop else "TRAIL"))
            left, done = 0.0, True
            break
        if ht:
            if flat:
                out["legs"].append((left, tgt, "TARGET"))
                left, done = 0.0, True
                break
            out["legs"].append((pf, tgt, "PART"))
            left -= pf
            part_done, part_bar = True, j
        if mgmt == "TRAIL_CLOSE" and c < float(B.e20[j]):
            if j + 1 < n:
                out["legs"].append((left, float(B.o[j + 1]), "EMA_CLOSE"))
                lo_seen = min(lo_seen, float(B.o[j + 1]))
                j += 1
            else:
                out["legs"].append((left, c, "OPEN_END"))
                out["open_end"] = 1
            left, done = 0.0, True
            break
        j += 1
    if not done:
        j = min(j, n - 1, last)
        if j >= n - 1:
            out["legs"].append((left, float(B.c[n - 1]), "OPEN_END"))
            out["open_end"] = 1
        else:
            out["legs"].append((left, float(B.c[j]), "TIME"))
    out["exit_j"] = j
    slip, sslip = float(cs.get("slip_bps", 0)) / 1e4, float(cs.get("stop_slip_bps", 0)) / 1e4
    ent_eff = E * (1 + B.buy) * (1 + slip)
    val = sum(fr * px * (1 - B.sell) * (1 - (sslip if rs.endswith("GAP") or rs in STOP_REASONS else slip))
              for fr, px, rs in out["legs"])
    out["net_r"] = (val - ent_eff) / R
    out["net_pct"] = (val - ent_eff) / ent_eff
    out["exit_price"] = sum(fr * px for fr, px, _ in out["legs"])
    out["reason"] = "+".join(rs for _, _, rs in out["legs"])
    out["mfe_r"] = (hi_seen - E) / R
    out["mae_r"] = (lo_seen - E) / R
    out["bars"] = j - j0 + 1
    return out


def plan_rows(B: Bars, t: int, cfg: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Decision at the close of cross candle t: stops, eligibility, every plan's simulated outcome."""
    st, mg, en = cfg["stops"], cfg["management"], cfg["entry"]
    c = float(B.c[t])
    sw = swing_stop(B, t, int(st.get("swing_k", 3)), int(st.get("lookback", 400)), c)
    pb = pullback_low(B, t, int(st.get("lookback", 400)))
    buf = float(st.get("buffer_pct", 0.0))
    stops = {"SWING": (sw[0] * (1 - buf), sw[1]) if sw else None,
             "PBLOW": (pb[0] * (1 - buf), pb[1]) if pb and pb[0] < c else None}
    info = {"t": t, "stops": stops, "pb_start": pb[2] if pb else None}
    rows: list[dict[str, Any]] = []
    for sname in STOPS:
        s = stops[sname]
        if s is None:
            continue
        stop, sc = s
        risk_c = c - stop
        room = (float(B.e200[t]) - c) / risk_c if risk_c > 0 else float("nan")
        for ename in ENTRIES:
            if ename == "NEXT_OPEN":
                if t + 1 >= len(B):
                    continue
                fill = (t + 1, float(B.o[t + 1]), "open")
                status = "filled" if fill[1] > stop else "gap_below_stop"
            else:
                j, px, status = retest_fill(B, t, stop, int(en.get("max_wait", 12)))
                fill = (j, px, "limit")
                if status == "filled" and px <= stop:
                    status = "gap_below_stop"
            for mgmt in mg["plans"]:
                if mgmt in ("T200", "PT200") and not (room >= float(mg.get("min_room_r", 1.0))):
                    continue  # EMA200 not above, or not enough room in R: plan not eligible
                pid = f"{ename}|{sname}|{mgmt}"
                base = {"plan": pid, "entry_kind": ename, "stop_kind": sname, "mgmt": mgmt, "stop": stop,
                        "stop_candle": sc, "room200_r": room, "fill_status": status}
                if status != "filled":
                    rows.append({**base, "filled": 0, "net_r": 0.0, "net_pct": 0.0, "mfe_r": 0.0, "mae_r": 0.0,
                                 "bars": 0, "exit_j": fill[0], "fill_j": fill[0], "entry": float("nan"),
                                 "exit_price": float("nan"), "reason": "NO_FILL:" + status, "ambiguous": 0,
                                 "lower_tf": 0, "open_end": int(status == "data_end"), "fill_bar_target_skipped": 0})
                    continue
                x = simulate_plan(B, t, fill, stop, mgmt, cfg)
                x.pop("legs")
                rows.append({**base, "filled": 1, **x})
    return info, rows


def features(B: Bars, t: int, info: dict[str, Any], t_prev: int) -> dict[str, float]:
    """Price action known at the close of the cross candle t (long)."""
    c, o, h, lo = float(B.c[t]), float(B.o[t]), float(B.h[t]), float(B.l[t])
    a = float(B.atr[t]) if np.isfinite(B.atr[t]) and B.atr[t] > 0 else float("nan")
    e13, e20, e200 = float(B.e13[t]), float(B.e20[t]), float(B.e200[t])
    s = info["stops"]["SWING"] or info["stops"]["PBLOW"]
    risk = c - s[0] if s else float("nan")
    rg = max(h - lo, 1e-9)
    f = {"d13_atr": (c - e13) / a, "d13_pct": (c - e13) / e13, "d13_r": (c - e13) / risk if risk > 0 else np.nan,
         "d20_atr": (c - e20) / a, "d200_atr": (c - e200) / a, "above200": float(c > e200),
         "room200_r": (e200 - c) / risk if risk > 0 else np.nan}
    for nm in STOPS:
        x = info["stops"][nm]
        f[f"stop_{nm.lower()}_atr"] = (c - x[0]) / a if x else np.nan
        f[f"stop_{nm.lower()}_pct"] = (c - x[0]) / c if x else np.nan
    f.update(slope13=(e13 - float(B.e13[t - 5])) / a, slope20=(e20 - float(B.e20[t - 5])) / a,
             slope200=(e200 - float(B.e200[t - 10])) / a,
             range5=float(np.mean(B.h[t - 4:t + 1] - B.l[t - 4:t + 1])) / a, range_cross=(h - lo) / a,
             body=abs(c - o) / rg, upper_wick=(h - max(o, c)) / rg, lower_wick=(min(o, c) - lo) / rg,
             ret20_atr=(c - float(B.c[t - 20])) / a)
    lo50, hi50 = float(B.l[t - 49:t + 1].min()), float(B.h[t - 49:t + 1].max())
    f["pos50"] = (c - lo50) / max(hi50 - lo50, 1e-9)
    pb = info["stops"]["PBLOW"]
    f["pull_depth_atr"] = (float(B.h[max(0, t - 20):t + 1].max()) - pb[0]) / a if pb else np.nan
    f["pull_len"] = float(t - info["pb_start"]) if info["pb_start"] is not None else np.nan
    sw = info["stops"]["SWING"]
    if sw:  # swing structure: is the stop swing higher than the one before it?
        prev = swing_stop(B, sw[1] - 1, 3, 400, float("inf")) if sw[1] - 1 > 6 else None
        f["higher_low"] = float(prev is not None and sw[0] > prev[0])
    else:
        f["higher_low"] = np.nan
    vv = B.v[t - 20:t].astype(float)
    f["rvol"] = float(B.v[t]) / float(vv.mean()) if vv.mean() > 0 else np.nan
    f["above_pdh"] = float(c > B.pdh[t]) if np.isfinite(B.pdh[t]) else np.nan
    f["bars_since_cross"] = float(min(t - t_prev, 500))
    f["tod"] = float(B.tod[t])
    return {k: float(v) for k, v in f.items()}


# ====================================================================== stock loading + audit
def _owner_ns(sym: str, a: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(symbol=sym, tf=a.tf, spec=str(OWNER_SPEC), config=a.config, no_check=True)


def load_stock(sym: str, a: argparse.Namespace, cfg: dict[str, Any]):
    from .owner_strategy import OwnerStrategy
    from .strategy_engine import _ema_of
    from . import tune_eval as te
    o = OwnerStrategy(sym, _owner_ns(sym, a))
    if not o.load():
        return o, None
    m = o.m
    lo_h = lo_l = lo_end = None
    ltf = cfg.get("lower_tf")
    if ltf and ltf != o.tf:
        try:
            Lx = te._load(o.du.stocks[0], o.du.config, [ltf], bool(o.du.t["use_minute"])).get(ltf)
            if Lx is not None:
                lm = Lx["m"]
                lo_h, lo_l = np.asarray(lm.high, float), np.asarray(lm.low, float)
                lo_end = np.asarray(lm.end_ns, np.int64)
        except Exception as exc:  # noqa: BLE001 - lower TF is optional; conservative rule applies without it
            o.log(f"lower timeframe {ltf} not loaded ({type(exc).__name__}: {exc}) - conservative ordering only")
    c = np.asarray(m.close, float)
    B = Bars(np.asarray(m.open, float), np.asarray(m.high, float), np.asarray(m.low, float), c,
             np.nan_to_num(np.asarray(m.volume, float)), _ema_of(m, 13), _ema_of(m, 20), _ema_of(m, 200),
             np.asarray(m.natr, float) * c, np.asarray(o.sid), np.asarray(o.end_ns), list(o.times),
             float(o.L["buy"]), float(o.L["sell"]), TF_MIN.get(o.tf, 15) * 60 * 10 ** 9, lo_h, lo_l, lo_end)
    return o, B


def data_checks(o, B: Bars) -> list[str]:
    tfm = TF_MIN.get(o.tf, 15)
    mins = np.array([int(x[-2:]) for x in B.times])
    first = [B.times[i][11:] for i in range(len(B)) if i == 0 or B.sid[i] != B.sid[i - 1]]
    _, per = np.unique(B.sid, return_counts=True)
    mode = int(np.bincount(per).argmax()) if len(per) else 0
    odd = int((per != mode).sum())
    return ["## Data checks", "",
            f"- Candle times = TradingView START (IST) = candle end - {tfm} min, +05:30. Minutes not on a {tfm}-min grid: "
            f"{int((mins % tfm != 0).sum())} of {len(B)}.",
            f"- Session first candle: {', '.join(f'{k} x{v}' for k, v in zip(*np.unique(first, return_counts=True)))}.",
            f"- Candles per session: usual {mode}; {odd} of {len(per)} sessions differ (half days / gaps in the feed).",
            f"- Data {B.times[0]} .. {B.times[-1]}; crosses counted from candle {o.a0} ({B.times[o.a0]}) = EMA200 "
            "warm-up. Our EMA history starts with the data (2026-01), TradingView's starts years earlier, so EMA values "
            "differ slightly and a cross can land one candle apart. A one-bar mismatch with a tiny EMA13-EMA20 gap at "
            "your candle is a feed / warm-up difference, not the strategy.",
            f"- Lower timeframe for same-candle ordering: {'loaded' if B.lo_end is not None else 'NOT available'}.", ""]


def audit(o, B: Bars, cross_ts: list[int], cfg: dict[str, Any], plans_by_t: dict[int, list[dict[str, Any]]]
          ) -> tuple[list[str], dict[int, str]]:
    """Owner marks vs our crosses. Returns markdown and labels {cross t: take/skip} (explicit marks only)."""
    from .owner_strategy import all_owner_rows
    ck = json.loads(OWNER_SPEC.read_text(encoding="utf-8"))["check"]
    rows = [r for r in all_owner_rows(REPO / ck["owner_trades"], o.sym, o.tf) if (r["side"] or "long") == "long"]
    labels: dict[int, str] = {}
    if not rows:
        return [], labels
    W = int(cfg.get("match_candles", 2))
    cs = np.array(cross_ts)
    f2 = lambda x: f"{x:.2f}"  # noqa: E731
    md = ["## 1. Audit: your long marks vs our crosses", "",
          "Exact = our EMA13xEMA20 cross on your candle; one-bar = one candle apart; none = no cross within "
          f"{W} candles. Gap = EMA13-EMA20 at your candle (% of price; tiny = feed/warm-up). Fill = next open. "
          "Stops: SWING (last confirmed swing low) and PBLOW (lowest low of the pullback), ✓ = your stop candle. "
          "Exit = NEXT_OPEN|SWING|TRAIL_CLOSE plan.", "",
          "| Your signal | Dec | O / H / L / C | EMA13 / 20 / 200 | Gap % | Our cross | Match | Fill | Your stop "
          "| SWING stop | PBLOW stop | Your exit | Our exit (plan) |", "|" + "---|" * 13]
    cnt = {"exact": 0, "one-bar": 0, "none": 0, "no candle": 0}
    stop_hit = {"SWING": 0, "PBLOW": 0}
    swing_k_hit = {1: 0, 2: 0, 3: 0}
    nwhy: dict[str, int] = {}
    n_stop = 0
    for r in sorted(rows, key=lambda r: r["signal"] or ""):
        i = o.find(r["signal"])
        if i is None:
            cnt["no candle"] += 1
            md.append(f"| {r['signal']} | {r['decision']} | no candle |" + " |" * 11)
            continue
        js = o.find(r["stop"])
        near = cs[np.abs(cs - i) <= W] if len(cs) else cs
        t = int(near[np.argmin(np.abs(near - i))]) if len(near) else None
        kind = "none" if t is None else ("exact" if t == i else ("one-bar" if abs(t - i) == 1 else f"{t - i:+d}"))
        cnt[kind if kind in cnt else "none"] += 1
        if t is None:  # say why: warm-up, no fresh cross (already above), or not crossed yet in our data
            nb = int(cs[np.argmin(np.abs(cs - i))]) - i if len(cs) else None
            far_txt = f"; nearest cross {nb:+d} candles" if nb is not None else ""
            if i < o.a0:
                why = "before EMA200 warm-up"
            elif B.e13[i] > B.e20[i]:
                why = "no fresh cross: EMA13 already above EMA20 (re-entry / continuation)"
            else:
                why = "EMA13 still below EMA20 in our data"
            kind = f"none ({why}{far_txt})"
            nwhy[why] = nwhy.get(why, 0) + 1
        elif js is not None:  # which swing strength gives your stop candle
            for k_ in (1, 2, 3):
                sx = swing_stop(B, t, k_, 400, float(B.c[t]))
                swing_k_hit[k_] += bool(sx and sx[1] == js)
        if t is not None and r["decision"] in ("take", "skip"):
            labels[t] = r["decision"]
        gap = 100 * (B.e13[i] - B.e20[i]) / B.c[i]
        ys = f"{r['stop']} ({B.l[js]:.2f})" if js is not None else (r["stop"] or "-")
        n_stop += js is not None
        scells, fill, ex = [], "", ""
        if t is not None:
            st = plans_by_t.get(t, [])
            for sn in STOPS:
                x = next((p for p in st if p["stop_kind"] == sn), None)
                if x is None:
                    scells.append("none")
                    continue
                ok = js is not None and x["stop_candle"] == js
                stop_hit[sn] += ok
                scells.append(f"{B.times[x['stop_candle']][5:]} ({x['stop']:.2f})"
                              + (" ✓" if ok else (f" {x['stop_candle'] - js:+d}" if js is not None else "")))
            fill = f"{B.times[t + 1][5:]} @ {B.o[t + 1]:.2f}" if t + 1 < len(B) else ""
            p = next((p for p in st if p["plan"] == "NEXT_OPEN|SWING|TRAIL_CLOSE"), None)
            if p and p["filled"]:
                ex = f"{B.times[p['exit_j']][5:]} {p['reason']} {p['net_r']:+.2f}R"
        else:
            scells = ["", ""]
        md.append(f"| {r['signal']} | {r['decision']} | {f2(B.o[i])} / {f2(B.h[i])} / {f2(B.l[i])} / {f2(B.c[i])} | "
                  f"{f2(B.e13[i])} / {f2(B.e20[i])} / {f2(B.e200[i])} | {gap:+.3f} | "
                  f"{B.times[t] if t is not None else '-'} | {kind} | {fill} | {ys} | {scells[0]} | {scells[1]} | "
                  f"{(r['exit'] or '-')} {r['reason']} | {ex} |")
    md += ["", f"Matches: exact {cnt['exact']}, one-bar {cnt['one-bar']}, none {cnt['none']}, "
           f"no candle {cnt['no candle']} (of {len(rows)} long marks). Stop candle same as yours ({n_stop} with a stop): "
           f"SWING {stop_hit['SWING']}, PBLOW {stop_hit['PBLOW']}. Swing strength (candles each side) on matched "
           f"crosses: " + ", ".join(f"k={k} {v}" for k, v in swing_k_hit.items())
           + f" (plans use k={cfg['stops'].get('swing_k', 3)}).",
           "Why 'none': " + (", ".join(f"{k} {v}" for k, v in nwhy.items()) or "-") + ".",
           f"Explicit labels for the judgement check: {sum(v == 'take' for v in labels.values())} take, "
           f"{sum(v == 'skip' for v in labels.values())} skip; every other cross is unlabeled (still simulated).", ""]
    o.log(f"audit: exact {cnt['exact']}, one-bar {cnt['one-bar']}, none {cnt['none']}; stop SWING {stop_hit['SWING']} "
          f"PBLOW {stop_hit['PBLOW']} of {n_stop}")
    return md, labels


def v2_result(o, t: int) -> tuple[float, float, int, str] | None:
    """Old engine version V2 for this cross: (net R, net %) or None."""
    v = next((x for x in json.loads(OWNER_SPEC.read_text(encoding="utf-8"))["check"]["variants"]
              if x["name"].startswith("V2 ")), None)
    if v is None:
        return None
    tr = o.sim("long", np.array([t]), v)
    if not tr:
        return None
    x = tr[0]
    st = o.stop_of("long", t, "last_swing", 1)
    if st is None or st[0] >= x["entry_price"]:
        return None
    return float(x["net_return"] * x["entry_price"] / (x["entry_price"] - st[0])), float(x["net_return"]), \
        int(x["exit_index"]), x["exit_reason"]


def extract(sym: str, a: argparse.Namespace, out_dir: str) -> str:
    """One stock: audit + every cross x eligible plan -> <out_dir>/<SYM>/{data.json, plans.csv, audit.md}."""
    cfg = json.loads(Path(a.plans).read_text(encoding="utf-8"))
    d = Path(out_dir) / sym
    d.mkdir(parents=True, exist_ok=True)
    o, B = load_stock(sym, a, cfg)
    try:
        if B is None:
            (d / "data.json").write_text(json.dumps({"symbol": sym, "crosses": [], "plans": []}), encoding="utf-8")
            return f"{sym}: no data"
        o.calibrate()
        ts = [t for t in o.sig["long"].tolist() if "EMA13xEMA20" in o.kind["long"].get(int(t), "")]
        crosses_, plans = [], []
        by_t: dict[int, list[dict[str, Any]]] = {}
        prev = o.a0
        for t in ts:
            if t < 50 or t + 1 >= len(B):
                continue
            info, rows = plan_rows(B, t, cfg)
            by_t[t] = rows
            f = features(B, t, info, prev)
            prev = t
            v2 = v2_result(o, t)
            cr = {"symbol": sym, "t": t, "time": B.times[t], "dec_ns": int(B.end_ns[t]), "close": float(B.c[t]),
                  "e13": float(B.e13[t]), "e20": float(B.e20[t]), "e200": float(B.e200[t]), "atr": float(B.atr[t]),
                  "next_open": float(B.o[t + 1]), "feat": f, "label": "",
                  "v2_r": v2[0] if v2 else None, "v2_pct": v2[1] if v2 else None,
                  "v2_close_ns": int(B.end_ns[v2[2]]) if v2 else None, "v2_open": int(v2[3] == "OPEN_END") if v2 else 0}
            crosses_.append(cr)
            for r in rows:
                r2 = {k: v for k, v in r.items() if not isinstance(v, (tuple, list))}
                r2.update(symbol=sym, t=t, dec_ns=int(B.end_ns[t]), close_ns=int(B.end_ns[min(r["exit_j"], len(B) - 1)]),
                          exit_time=B.times[min(r["exit_j"], len(B) - 1)], stop_time=B.times[r["stop_candle"]],
                          fill_time=B.times[min(r["fill_j"], len(B) - 1)])
                plans.append(r2)
        md, labels = audit(o, B, [c["t"] for c in crosses_], cfg, by_t)
        for c in crosses_:
            c["label"] = labels.get(c["t"], "")
        if md:
            (d / "audit.md").write_text("\n".join([f"# Audit {sym} {o.tf} (long)", ""] + data_checks(o, B) + md) + "\n",
                                        encoding="utf-8")
        with (d / "plans.csv").open("w", newline="", encoding="utf-8") as fh:
            if plans:
                w = csv.DictWriter(fh, fieldnames=list(plans[0]))
                w.writeheader()
                w.writerows(plans)
        (d / "data.json").write_text(json.dumps({"symbol": sym, "tf": o.tf, "crosses": crosses_, "plans": plans},
                                                default=lambda x: x.item() if hasattr(x, "item") else str(x)),
                                     encoding="utf-8")
        return f"{sym}: {len(crosses_)} crosses, {len(plans)} plan rows" + (", audit written" if md else "")
    finally:
        o.du.close()


# ====================================================================== walk-forward learning
def _metrics(rs: list[float], min_n: int) -> dict[str, Any]:
    x = np.array(rs, float)
    taken = x[x != 0.0] if len(x) else x
    eq = np.cumsum(x) if len(x) else np.zeros(1)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], eq])) - np.concatenate([[0.0], eq])).max())
    n = len(taken)
    mean = float(taken.mean()) if n else 0.0
    tstat = mean / (taken.std(ddof=1) / math.sqrt(n)) if n > 2 and taken.std(ddof=1) > 0 else 0.0
    if n < min_n:
        verdict = "insufficient evidence"
    elif mean > 0 and tstat >= 2.0:
        verdict = "positive (supported)"
    elif mean > 0:
        verdict = "positive but not significant"
    else:
        verdict = "negative"
    return {"decisions": len(x), "trades": n, "win": float((taken > 0).mean()) if n else 0.0, "avg_r": mean,
            "total_r": float(x.sum()), "per_decision": float(x.mean()) if len(x) else 0.0, "max_dd_r": dd,
            "t": tstat, "verdict": verdict}


class Learner:
    def __init__(self, crosses_: list[dict[str, Any]], plans: list[dict[str, Any]], cfg: dict[str, Any]):
        self.cfg, self.L = cfg, cfg["learn"]
        self.C = sorted(crosses_, key=lambda c: (c["dec_ns"], c["symbol"]))
        self.fn = sorted({k for c in self.C for k in c["feat"]})
        self.P: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
        for p in plans:
            self.P.setdefault((p["symbol"], p["t"]), {})[p["plan"]] = p
        self.plan_ids = sorted({p["plan"] for p in plans})
        xi = {(c["symbol"], c["t"]): k for k, c in enumerate(self.C)}
        self.X = np.array([[c["feat"].get(k, np.nan) for k in self.fn] for c in self.C], float)
        # one flat table of plan rows: cross index, plan, close time, net R
        self.rows = [(xi[(p["symbol"], p["t"])], p["plan"], p["close_ns"] if not p["open_end"] else 2 ** 62,
                      p["net_r"], p["dec_ns"]) for p in plans if (p["symbol"], p["t"]) in xi]

    def weight(self, dec_ns: np.ndarray, now_ns: int) -> np.ndarray:
        age = np.maximum(0, (now_ns - dec_ns) / 86400e9) * 5 / 7  # ~sessions
        return (1 + age / float(self.L.get("recency_decay_sessions", 100))) ** -float(self.L.get("recency_power", 2))

    def cell(self, c: dict[str, Any]) -> str:
        f = c["feat"]
        near = abs(f.get("d13_atr", np.nan)) <= float(self.L.get("rule_near_atr", 0.5))
        return ("near" if near else "far") + ("/above200" if f.get("above200") == 1.0 else "/below200")

    def fit(self, now_ns: int) -> dict[str, Any]:
        from sklearn.ensemble import HistGradientBoostingRegressor
        R = self.rows
        models: dict[str, Any] = {}
        rule: dict[tuple[str, str], tuple[float, int]] = {}
        for pid in self.plan_ids:
            sel = [(ci, r, dn) for ci, p, cl, r, dn in R if p == pid and cl < now_ns]
            if not sel:
                continue
            ci = np.array([s[0] for s in sel])
            y = np.array([s[1] for s in sel])
            w = self.weight(np.array([s[2] for s in sel], float), now_ns)
            n = len(y)
            mean = float((w * y).sum() / w.sum())
            mdl = None
            if n >= int(self.L.get("min_rows", 30)):
                mdl = HistGradientBoostingRegressor(max_depth=3, max_iter=120, learning_rate=0.05,
                                                    min_samples_leaf=max(10, n // 20), random_state=0)
                mdl.fit(self.X[ci], np.clip(y, -3, 10), sample_weight=w)
            models[pid] = (mdl, mean, n)
            cells: dict[str, list[int]] = {}
            for k, c_ in enumerate(ci.tolist()):
                cells.setdefault(self.cell(self.C[c_]), []).append(k)
            for cl, ks in cells.items():
                ks_ = np.array(ks)
                rule[(pid, cl)] = (float((w[ks_] * y[ks_]).sum() / w[ks_].sum()), len(ks))
        return {"models": models, "rule": rule}

    def run(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        fitted, k_fit = None, -10 ** 9
        every = int(self.L.get("refit_every", 25))
        min_rows, edge = int(self.L.get("min_rows", 30)), float(self.L.get("min_edge_r", 0.05))
        block: dict[str, np.ndarray] = {}
        for k, c in enumerate(self.C):
            if fitted is None or k - k_fit >= every:
                fitted, k_fit = self.fit(c["dec_ns"]), k
                if k % (every * 20) == 0:
                    print(f"  walk-forward {k:,}/{len(self.C):,} crosses ({c['time'][:10]})", flush=True)
                # one batched prediction for the crosses this fit serves (all decided after the fit time)
                block = {pid: mdl.predict(self.X[k:k + every]) for pid, (mdl, _, _) in fitted["models"].items()
                         if mdl is not None}
            elig = self.P.get((c["symbol"], c["t"]), {})
            preds: dict[str, float] = {}
            for pid, (mdl, mean, n) in fitted["models"].items():
                if pid not in elig or mdl is None:
                    continue
                p = float(block[pid][k - k_fit])
                s = n / (n + 100.0)  # thin evidence leans on the plan's own recent average
                preds[pid] = s * p + (1 - s) * mean
            rules = {pid: fitted["rule"][(pid, self.cell(c))] for pid in elig if (pid, self.cell(c)) in fitted["rule"]}
            rules = {p: v for p, v in rules.items() if v[1] >= max(10, min_rows // 2)}
            d = {"k": k, "cell": self.cell(c)}
            if not preds:
                d.update(model="SKIP", model_er=None, model_why="insufficient evidence (no plan has "
                         f"{min_rows} closed trades yet)")
            else:
                best = max(preds, key=preds.get)
                order = sorted(preds.values(), reverse=True)
                if preds[best] < edge:
                    d.update(model="SKIP", model_er=preds[best], model_why=f"best plan {best} expects "
                             f"{preds[best]:+.2f}R < {edge:.2f}R")
                else:
                    d.update(model=best, model_er=preds[best], model_why=f"expects {preds[best]:+.2f}R"
                             + (f" (next best {order[1]:+.2f}R)" if len(order) > 1 else ""))
            if not rules:
                d.update(rule="SKIP", rule_er=None, rule_why=f"insufficient evidence in cell {self.cell(c)}")
            else:
                best = max(rules, key=lambda p: rules[p][0])
                m_, n_ = rules[best]
                d.update(rule=best if m_ >= edge else "SKIP", rule_er=m_,
                         rule_why=f"cell {self.cell(c)}: {best} averaged {m_:+.2f}R over {n_} closed")
            out.append(d)
        return out


# ====================================================================== orchestration + report
class Research:
    def __init__(self, a: argparse.Namespace):
        from .discover import Discoverer
        from .walk_forward import read_stocks
        self.a = a
        self.cfg = json.loads(Path(a.plans).read_text(encoding="utf-8"))
        a.tf = a.tf or self.cfg.get("timeframe", "15m")
        cfgj = json.loads(Path(a.config).read_text(encoding="utf-8"))
        if a.stocks:
            uni = read_stocks(a.stocks if a.stocks.startswith("@") else "@" + str(REPO / "deploy" / "oracle" / "live" /
                                                                                  "universe.txt"))
            self.stocks = uni if a.stocks.startswith("@") else uni[:int(a.stocks)]
        else:
            self.stocks = []
        sym = a.symbol.upper().removesuffix("-EQ")
        if sym not in self.stocks:
            self.stocks.insert(0, sym)
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "owner_plans_stocks.txt"
        tmp.write_text(self.stocks[0] + "\n", encoding="utf-8")
        # one-stock and pooled runs get their own folders (a pooled run must not overwrite the one-stock report)
        name = f"owner-plans-{a.tf}" + (f"-{sym}" if len(self.stocks) == 1 else f"-pool{len(self.stocks)}")
        self.du = Discoverer(a.tf, name, cfgj, "@" + str(tmp), 1)
        self.log, self.root = self.du.log, self.du.root
        self.sdir = self.root / "stocks"
        self.sdir.mkdir(parents=True, exist_ok=True)

    def collect(self) -> None:
        todo = [s for s in self.stocks if self.a.fresh or not (self.sdir / s / "data.json").is_file()]
        self.log(f"{len(self.stocks)} stocks: {len(self.stocks) - len(todo)} already done (checkpoint), {len(todo)} to "
                 f"simulate with {self.a.workers} worker(s)")
        t0 = time.monotonic()
        if self.a.workers <= 1 or len(todo) <= 1:
            for k, s in enumerate(todo, 1):
                try:
                    msg = extract(s, self.a, str(self.sdir))
                except Exception as exc:  # noqa: BLE001
                    msg = f"{s}: FAILED {type(exc).__name__}: {exc}"
                self.log(f"[{k}/{len(todo)}] {msg} ({time.monotonic() - t0:.0f}s)")
            return
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=self.a.workers) as ex:
            fut = {ex.submit(extract, s, self.a, str(self.sdir)): s for s in todo}
            for k, f in enumerate(as_completed(fut), 1):
                s = fut[f]
                try:
                    msg = f.result()
                except Exception as exc:  # noqa: BLE001 - one bad stock must not stop the run
                    msg = f"{s}: FAILED {type(exc).__name__}: {exc}"
                el = time.monotonic() - t0
                self.log(f"[{k}/{len(todo)}] {msg} ({el:.0f}s, ~{el / k * (len(todo) - k):.0f}s left)")

    def run(self) -> int:
        self.collect()
        C, P = [], []
        for s in self.stocks:
            p = self.sdir / s / "data.json"
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                C += d["crosses"]
                P += d["plans"]
        if not C:
            self.log("no crosses - nothing to learn")
            return 1
        self.log(f"walk-forward over {len(C):,} crosses / {len(P):,} plan rows "
                 f"({'pooled over ' + str(len(self.stocks)) + ' stocks' if len(self.stocks) > 1 else 'one stock'})")
        L = Learner(C, P, self.cfg)
        dec = L.run()
        self.report(L, dec)
        return 0

    def report(self, L: Learner, dec: list[dict[str, Any]]) -> None:
        cfg, lc = self.cfg, self.cfg["learn"]
        mn = int(lc.get("min_evidence_trades", 20))
        bl = cfg["baselines"]

        def got(c: dict[str, Any], pid: str) -> tuple[float | None, bool]:
            """Realised net R of plan pid on cross c, and whether it is still open at the data end."""
            if pid == "SKIP":
                return 0.0, False
            if pid == "V2":
                return (c["v2_r"] if c["v2_r"] is not None else 0.0), bool(c.get("v2_open"))
            p = L.P.get((c["symbol"], c["t"]), {}).get(pid)
            if p is None:
                return 0.0, False  # not eligible = no trade
            return float(p["net_r"]), bool(p["open_end"])

        sel = {"MODEL": [d["model"] for d in dec], "RULE": [d["rule"] for d in dec]}
        for nm, pid in bl.items():
            if not nm.startswith("_"):  # "_doc" is a comment, not a baseline
                sel[nm] = [pid] * len(dec)
        res: dict[str, dict[str, Any]] = {}
        per_stock: dict[str, dict[str, list[float]]] = {}
        n_open: dict[str, int] = {}
        for nm, picks in sel.items():
            rs = []
            n_open[nm] = 0
            for c, pid in zip(L.C, picks):
                r, op = got(c, pid)
                if op:
                    n_open[nm] += 1
                    continue  # still open at the data end: not scored
                rs.append(r)
                per_stock.setdefault(c["symbol"], {}).setdefault(nm, []).append(r)
            res[nm] = _metrics(rs, mn)
        # hindsight diagnostic
        hind = []
        for c in L.C:
            ps = [p for p in L.P.get((c["symbol"], c["t"]), {}).values() if not p["open_end"]]
            hind.append(max([0.0] + [float(p["net_r"]) for p in ps]))
        md = [f"# Owner EMA13xEMA20 trade plans - {self.a.tf} long", "",
              f"{len(L.C):,} crosses on {len(per_stock)} stock(s), {L.C[0]['time']} .. {L.C[-1]['time']} (candle START, "
              "IST). Every cross is scored on its own (overlapping trades are possible). Net R after lane costs and "
              f"slippage ({cfg['costs']['slip_bps']} bps, stops {cfg['costs']['stop_slip_bps']} bps). Walk-forward: "
              "the model / rule at each cross only learned from plan rows that had CLOSED before that cross candle "
              f"closed (refit every {lc['refit_every']} crosses, recent rows weigh more). Fewer than {mn} trades = "
              "insufficient evidence. **Profitability is claimed only where the verdict says 'supported'.**", "",
              "## 2. Learned selection vs baselines (walk-forward, only the chosen plan is scored)", "",
              "| Selector | Decisions | Trades | Win | Avg R / trade | Total R | Avg R / decision | Max DD (R) | t | "
              "Verdict | Open at end (not scored) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for nm, m in res.items():
            lab = f"{nm} ({bl[nm]})" if nm in bl else nm
            md.append(f"| {lab} | {m['decisions']} | {m['trades']} | {m['win']:.0%} | {m['avg_r']:+.3f} | "
                      f"{m['total_r']:+.1f} | {m['per_decision']:+.3f} | {m['max_dd_r']:.1f} | {m['t']:.2f} | "
                      f"{m['verdict']} | {n_open[nm]} |")
        md += ["", f"Hindsight best plan per cross (DIAGNOSTIC ONLY, not achievable): avg {np.mean(hind):+.2f}R per "
               "cross.", ""]
        mb = res["MODEL"]
        rbk = max((k for k in res if k not in ("MODEL", "RULE")), key=lambda k: res[k]["total_r"])
        md += [f"**Did the learned selection beat the baselines?** MODEL total {mb['total_r']:+.1f}R vs best baseline "
               f"{rbk} {res[rbk]['total_r']:+.1f}R; MODEL verdict: {mb['verdict']}. Note: SKIP scores 0R, so a selector "
               "that mostly skips beats losing baselines without having an edge - judge it by its own verdict.", ""]
        md += self.cost_view(L)
        self.log(" | ".join(f"{k} {v['total_r']:+.1f}R/{v['trades']} ({v['verdict']})" for k, v in res.items()))
        # where each plan worked / failed (all closed rows, in-sample description)
        md += ["## 3. Where each plan worked and failed (all closed rows; descriptive, in-sample)", "",
               "| Plan | Rows | Filled | Win | Avg R | " + " | ".join(
                   f"{x}" for x in ("near/above200", "near/below200", "far/above200", "far/below200")) + " |",
               "|---|---|---|---|---|---|---|---|---|"]
        cells_of = {(c["symbol"], c["t"]): L.cell(c) for c in L.C}
        for pid in L.plan_ids:
            rows = [p for k, v in L.P.items() for q, p in v.items() if q == pid and not p["open_end"]]
            if not rows:
                continue
            r = np.array([p["net_r"] for p in rows])
            fl = np.array([p["filled"] for p in rows]).astype(bool)
            cl = []
            for cn in ("near/above200", "near/below200", "far/above200", "far/below200"):
                x = np.array([p["net_r"] for p in rows if cells_of.get((p["symbol"], p["t"])) == cn and p["filled"]])
                cl.append(f"{x.mean():+.2f} ({len(x)})" if len(x) else "-")
            md.append(f"| {pid} | {len(r)} | {fl.sum()} | {(r[fl] > 0).mean() if fl.any() else 0:.0%} | "
                      f"{r[fl].mean() if fl.any() else 0:+.2f} | " + " | ".join(cl) + " |")
        md += ["", "Cell = near/far EMA13 at the cross (|close-EMA13| <= "
               f"{lc.get('rule_near_atr', 0.5)} ATR) x close above/below EMA200. Cell values: avg R (filled trades).", ""]
        md += self.near_far(L)
        # owner judgement agreement
        lab = [(c["label"], d["model"]) for c, d in zip(L.C, dec) if c["label"]]
        if lab:
            tt = sum(1 for a_, m in lab if a_ == "take" and m != "SKIP")
            ts = sum(1 for a_, m in lab if a_ == "take" and m == "SKIP")
            st = sum(1 for a_, m in lab if a_ == "skip" and m != "SKIP")
            ss = sum(1 for a_, m in lab if a_ == "skip" and m == "SKIP")
            md += ["## 5. Your explicit take / skip marks vs the model's walk-forward choice", "",
                   "| | Model trades | Model skips |", "|---|---|---|", f"| You took | {tt} | {ts} |",
                   f"| You skipped | {st} | {ss} |", "", "Unmarked crosses are not counted here.", ""]
        # per stock
        md += ["## 6. Per stock (walk-forward picks; total R / trades / verdict)", "",
               "| Stock | " + " | ".join(sel) + " |", "|---|" + "---|" * len(sel)]
        for s, d in sorted(per_stock.items(), key=lambda kv: -sum(kv[1].get("MODEL", []))):
            cells = []
            for nm in sel:
                m = _metrics(d.get(nm, []), mn)
                cells.append(f"{m['total_r']:+.1f} / {m['trades']}" + (" (thin)" if m["trades"] < mn else ""))
            md.append(f"| {s} | " + " | ".join(cells) + " |")
        md += self.decisions(L, dec)
        (self.root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        audits = [str(self.sdir / s / "audit.md") for s in self.stocks if (self.sdir / s / "audit.md").is_file()]
        self.log(f"report {self.root / 'REPORT.md'}" + (f"; audit {', '.join(audits)}" if audits else ""))

    def cost_view(self, L: Learner) -> list[str]:
        """Gross vs net R by stop width (descriptive): narrow stops pay the same costs in rupees, far more in R."""
        md = ["## 2b. Costs in R by stop width (filled trades, descriptive)", "",
              "| Plan | Stop width | Trades | Gross R | Net R | Cost R |", "|---|---|---|---|---|---|"]
        for pid in [v for k, v in self.cfg["baselines"].items() if not k.startswith("_") and v != "V2"]:
            ps = [p for v in L.P.values() for q, p in v.items() if q == pid and p["filled"] and not p["open_end"]]
            if not ps:
                continue
            rp = np.array([p["risk"] / p["entry"] for p in ps])
            g = np.array([(p["exit_price"] - p["entry"]) / p["risk"] for p in ps])
            nr = np.array([p["net_r"] for p in ps])
            for lo, hi in ((0, .003), (.003, .006), (.006, .01), (.01, 1.0)):
                m = (rp >= lo) & (rp < hi)
                if m.any():
                    md.append(f"| {pid} | {100 * lo:.1f}-{100 * hi:.1f}% | {int(m.sum())} | {g[m].mean():+.2f} | "
                              f"{nr[m].mean():+.2f} | {(g[m] - nr[m]).mean():.2f} |")
        return md + [""]

    def near_far(self, L: Learner) -> list[str]:
        lc = self.cfg["learn"]
        a, b = "NEXT_OPEN|SWING|TRAIL_CLOSE", "RETEST13|SWING|TRAIL_CLOSE"
        md = ["## 4. 'Near' vs 'far' from EMA13 - which definition separates next-open from retest (descriptive)", "",
              f"Avg R of {a} vs {b} (no-fill retests count 0R) in each group.", "",
              "| Definition | Near: n | Near: next open | Near: retest | Far: n | Far: next open | Far: retest |",
              "|---|---|---|---|---|---|---|"]
        defs = [(f"|dist| <= {x} ATR", "d13_atr", x) for x in lc.get("near_far_atr", [])] + \
               [(f"|dist| <= {100 * x:.1f}%", "d13_pct", x) for x in lc.get("near_far_pct", [])]
        for nm, key, thr in defs:
            g = {True: ([], []), False: ([], [])}
            for c in L.C:
                v = c["feat"].get(key)
                pa, pb = L.P.get((c["symbol"], c["t"]), {}).get(a), L.P.get((c["symbol"], c["t"]), {}).get(b)
                if v is None or not np.isfinite(v) or pa is None or pb is None or pa["open_end"] or pb["open_end"]:
                    continue
                g[abs(v) <= thr][0].append(pa["net_r"])
                g[abs(v) <= thr][1].append(pb["net_r"])
            f = lambda x: f"{np.mean(x):+.2f}" if x else "-"  # noqa: E731
            md.append(f"| {nm} | {len(g[True][0])} | {f(g[True][0])} | {f(g[True][1])} | {len(g[False][0])} | "
                      f"{f(g[False][0])} | {f(g[False][1])} |")
        return md + [""]

    def decisions(self, L: Learner, dec: list[dict[str, Any]]) -> list[str]:
        mg = self.cfg["management"]
        trail_txt = {"T2R": "none (flat at 2R)", "T200": "none (flat at EMA200)",
                     "PT2R": f"{mg.get('partial_frac', 0.5):.0%} at 2R, rest EMA20 touch",
                     "PT200": f"{mg.get('partial_frac', 0.5):.0%} at EMA200, rest EMA20 touch",
                     "TRAIL_TOUCH": "EMA20 touch (prev candle EMA20, never loosens)",
                     "TRAIL_CLOSE": "close below EMA20 -> next open"}
        rows = []
        for c, d in zip(L.C, dec):
            pid = d["model"]
            p = L.P.get((c["symbol"], c["t"]), {}).get(pid) if pid != "SKIP" else None
            if p is not None:
                act = "take" if p["entry_kind"] == "NEXT_OPEN" else "wait"
                ent = (f"next open {c['next_open']:.2f}" if act == "take" else f"limit @ EMA13 (~{c['e13']:.2f})")
                tgt = ("2R " + f"{p['entry'] + mg.get('target_r', 2) * p['risk']:.2f}" if p["mgmt"] in ("T2R", "PT2R")
                       and p["filled"] else ("EMA200 ~" + f"{c['e200']:.2f}" if p["mgmt"] in ("T200", "PT200")
                                             else "-"))
                actual = (f"{p['fill_time']} @ {p['entry']:.2f} -> {p['exit_time']} {p['reason']} {p['net_r']:+.2f}R"
                          if p["filled"] else f"{p['reason']} (0R)")
                stop = f"{p['stop']:.2f} ({p['stop_time']})"
                flag = " [ambiguous: stop assumed first]" if p.get("ambiguous") else (
                    " [5m resolved]" if p.get("lower_tf") else "")
            else:
                act, ent, tgt, actual, stop, flag = "skip", "-", "-", "0R", "-", ""
            best = max((q for q in L.P.get((c["symbol"], c["t"]), {}).values() if not q["open_end"]),
                       key=lambda q: q["net_r"], default=None)
            rows.append({"symbol": c["symbol"], "cross": c["time"], "action": act, "plan": pid, "why": d["model_why"],
                         "planned_entry": ent, "stop": stop, "first_target": tgt,
                         "trail": trail_txt.get(p["mgmt"], "-") if p else "-",
                         "expected_r": "" if d["model_er"] is None else round(d["model_er"], 3),
                         "actual": actual + flag, "rule_pick": d["rule"], "rule_why": d["rule_why"],
                         "your_mark": c["label"], "v2_r": "" if c["v2_r"] is None else round(c["v2_r"], 2),
                         "hindsight_best": f"{best['plan']} {best['net_r']:+.2f}R" if best else "",
                         "close": round(c["close"], 2), "ema13": round(c["e13"], 2), "ema20": round(c["e20"], 2),
                         "ema200": round(c["e200"], 2), "d13_atr": round(c["feat"].get("d13_atr", np.nan), 2),
                         "room200_r": round(c["feat"].get("room200_r", np.nan), 2)})
        with (self.root / "decisions.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        md = ["", "## 7. Every cross (latest first; full list in decisions.csv, every plan in stocks/<SYM>/plans.csv)", "",
              "| Stock | Cross candle | Action | Why | Planned entry | Initial stop (candle) | First target | Trail | "
              "Exp. R | What happened | Your mark | V2 R | Hindsight best (diagnostic) |", "|" + "---|" * 13]
        for r in sorted(rows, key=lambda r: r["cross"], reverse=True)[: int(self.a.max_rows)]:
            md.append(f"| {r['symbol']} | {r['cross']} | {r['action']} {r['plan'] if r['action'] != 'skip' else ''} | "
                      f"{r['why']} | {r['planned_entry']} | {r['stop']} | {r['first_target']} | {r['trail']} | "
                      f"{r['expected_r']} | {r['actual']} | {r['your_mark']} | {r['v2_r']} | {r['hindsight_best']} |")
        return md


def main(argv: list[str] | None = None) -> int:
    from .tune import CONFIG
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default="RELIANCE")
    ap.add_argument("--tf", default=None)
    ap.add_argument("--stocks", default=None, help="N = first N of universe.txt (+ --symbol), or @file")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--plans", default=str(SPEC))
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--fresh", action="store_true", help="recompute every stock (ignore checkpoints)")
    ap.add_argument("--max-rows", type=int, default=400, help="crosses listed in REPORT.md (all in decisions.csv)")
    a = ap.parse_args(argv)
    r = Research(a)
    try:
        return r.run()
    finally:
        r.du.close()


if __name__ == "__main__":
    sys.exit(main())
