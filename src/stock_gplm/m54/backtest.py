"""Trade-level backtest of one entry rule plus one exit policy on one timeframe lane.

Timing and fills (long only; research diagnostics, never orders):

* the entry signal uses information up to the **close of bar t**;
* entry fills at the **open of bar t+1** (next executable bar) plus buy-side cost and slippage;
  an intraday lane never enters across a session boundary;
* every bar from t+1 is checked in this order: stop (a gap through the stop fills at the
  worse open), then target (a gap through the target fills at the open), so when one bar
  touches both the stop is assumed first (pessimistic); a trailing stop ratchets on closed
  highs and applies from the next bar;
* a time exit fills at the open of bar t+1+max_hold; an intraday lane is flat at the close
  of the session's last bar;
* a trade must finish inside its segment (purging); a path that touches a bar flagged as a
  price discontinuity (suspected corporate action) is skipped;
* trades never overlap: the next signal may come at the exit bar or later.

Statistics test the mean *excess* net return over the unconditional baseline (the same exit
policy entered on every eligible bar of the segment). On intraday lanes trades are first
averaged per session, so thousands of minute bars cannot pose as thousands of independent
observations; ``effective_n`` is the number of distinct sessions with a trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as sps


@dataclass(frozen=True)
class ExitPolicy:
    max_hold: int  # bars
    stop_atr: float | None = None
    target_atr: float | None = None
    trail_atr: float | None = None

    def key(self) -> str:
        f = lambda v: "-" if v is None else f"{v:g}"  # noqa: E731
        return f"h{self.max_hold}_s{f(self.stop_atr)}_t{f(self.target_atr)}_tr{f(self.trail_atr)}"

    def to_dict(self) -> dict[str, Any]:
        return {"max_hold_bars": self.max_hold, "stop_atr": self.stop_atr, "target_atr": self.target_atr,
                "trail_atr": self.trail_atr}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "ExitPolicy":
        return ExitPolicy(int(d["max_hold_bars"]), d.get("stop_atr"), d.get("target_atr"), d.get("trail_atr"))

    def text(self) -> str:
        parts = [f"time exit after {self.max_hold} bars (at the next bar's open)"]
        if self.stop_atr is not None:
            parts.append(f"stop {self.stop_atr:g} x ATR(14) below entry")
        if self.target_atr is not None:
            parts.append(f"target {self.target_atr:g} x ATR(14) above entry")
        if self.trail_atr is not None:
            parts.append(f"trailing stop {self.trail_atr:g} x ATR(14) below the highest closed high")
        return "; ".join(parts)


@dataclass(frozen=True)
class Costs:
    buy: float  # per-side fraction including slippage
    sell: float

    @staticmethod
    def from_bps(c: Mapping[str, Any]) -> "Costs":
        return Costs((c["commission_bps"] + c["slippage_bps"] + c["buy_statutory_bps"]) / 10_000,
                     (c["commission_bps"] + c["slippage_bps"] + c["sell_statutory_bps"]) / 10_000)


@dataclass
class LaneMarket:
    timeframe: str
    sessions: np.ndarray  # session date strings
    session_id: np.ndarray  # int per bar
    end_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    natr: np.ndarray  # ATR(14)/close at t
    blocked: np.ndarray  # no trade path may touch these bars
    no_signal: np.ndarray  # signals suppressed (incomplete bars, discontinuity warm-up)
    intraday: bool
    bars_per_year: float
    session_last: np.ndarray = field(init=False)  # index of the last bar of each bar's session

    def __post_init__(self) -> None:
        n = len(self.close)
        last = np.empty(n, dtype=np.int64)
        if n:
            change = np.nonzero(np.diff(self.session_id) != 0)[0]
            ends = np.concatenate((change, [n - 1]))
            starts = np.concatenate(([0], change + 1))
            for s, e in zip(starts, ends):
                last[s:e + 1] = e
        self.session_last = last
        self._blocked_cum = np.concatenate(([0], np.cumsum(self.blocked.astype(np.int64))))

    def __len__(self) -> int:
        return len(self.close)

    def label(self, i: int) -> str:
        if not self.intraday:
            return str(self.sessions[i])
        mins = int((self.end_ns[i] // 60_000_000_000 + 330) % 1440)  # IST minutes of day at bar end
        return f"{self.sessions[i]} {mins // 60:02d}:{mins % 60:02d}"


def _fill_path(m: LaneMarket, t: int, p: ExitPolicy, last_allowed: int) -> tuple[int, float, str] | None:
    """Exit bar index, exit reference price and reason for a trade signalled at t (entry open t+1)."""
    entry = m.open[t + 1]
    atr = m.natr[t] * entry if math.isfinite(m.natr[t]) else float("nan")
    needs_atr = p.stop_atr is not None or p.target_atr is not None or p.trail_atr is not None
    if needs_atr and not math.isfinite(atr):
        return None
    stop = entry - p.stop_atr * atr if p.stop_atr is not None else -math.inf
    target = entry + p.target_atr * atr if p.target_atr is not None else math.inf
    time_bar = t + 1 + p.max_hold
    if m.intraday:
        sess_end = int(m.session_last[t + 1])
        hold_end = min(time_bar - 1, sess_end)
    else:
        sess_end = -1
        hold_end = time_bar - 1
    if hold_end > last_allowed or (not m.intraday and time_bar > last_allowed):
        return None
    if m._blocked_cum[min(time_bar, len(m) - 1) + 1] - m._blocked_cum[t + 1] > 0:
        return None
    if p.trail_atr is None and stop == -math.inf and target == math.inf:
        if m.intraday and time_bar > sess_end:
            return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
        return time_bar, float(m.open[time_bar]), "TIME"
    high_water = entry
    for j in range(t + 1, hold_end + 1):
        o, h, lo = m.open[j], m.high[j], m.low[j]
        if j > t + 1 and o <= stop:
            return j, float(o), "STOP_GAP"
        if lo <= stop:
            return j, float(stop), "STOP" if p.trail_atr is None or stop < entry else "TRAIL"
        if j > t + 1 and o >= target:
            return j, float(o), "TARGET_GAP"
        if h >= target:
            return j, float(target), "TARGET"
        if p.trail_atr is not None:
            high_water = max(high_water, h)
            stop = max(stop, high_water - p.trail_atr * atr)
    if m.intraday and time_bar > sess_end:
        return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
    return time_bar, float(m.open[time_bar]), "TIME"


def simulate(m: LaneMarket, mask: np.ndarray, seg: tuple[int, int], p: ExitPolicy, costs: Costs,
             independent: bool = True) -> list[dict[str, Any]]:
    a, b = seg
    last_allowed = b - 1
    fired = np.nonzero(mask[a:b] & ~m.no_signal[a:b])[0] + a
    trades: list[dict[str, Any]] = []
    nxt = -1
    for t in fired.tolist():
        if t < nxt or t + 1 > last_allowed:
            continue
        if m.intraday and m.session_id[t + 1] != m.session_id[t]:
            continue
        fill = _fill_path(m, t, p, last_allowed)
        if fill is None:
            continue
        e, px, reason = fill
        entry_ref = float(m.open[t + 1])
        net = px * (1 - costs.sell) / (entry_ref * (1 + costs.buy)) - 1
        hi = float(m.high[t + 1:e + 1].max()) if e >= t + 1 else entry_ref
        lo = float(m.low[t + 1:e + 1].min()) if e >= t + 1 else entry_ref
        trades.append({"t": t, "exit_index": e, "signal": m.label(t), "entry": m.label(t + 1), "exit": m.label(e),
                       "session_id": int(m.session_id[t]), "entry_price": entry_ref, "exit_price": px,
                       "gross_return": px / entry_ref - 1, "net_return": net, "bars_held": e - t,
                       "exit_reason": reason, "mfe": hi / entry_ref - 1, "mae": lo / entry_ref - 1})
        if independent:
            nxt = e
    return trades


def baseline(m: LaneMarket, seg: tuple[int, int], p: ExitPolicy, costs: Costs, cache: dict[Any, Any] | None = None,
             max_starts: int = 4000) -> float | None:
    """Mean net return of the same exit policy entered on every eligible bar (a deterministic stride if large)."""
    key = (seg, p.key())
    if cache is not None and key in cache:
        return cache[key]
    a, b = seg
    mask = np.zeros(len(m), dtype=bool)
    stride = max(1, (b - a) // max_starts)
    mask[a:b:stride] = True
    tr = simulate(m, mask, seg, p, costs, independent=False)
    value = float(np.mean([x["net_return"] for x in tr])) if tr else None
    if cache is not None:
        cache[key] = value
    return value


def _weighted_mean(x: np.ndarray, w: np.ndarray | None) -> float:
    return float(np.average(x, weights=w)) if w is not None and w.sum() > 0 else float(x.mean())


def metrics(trades: Sequence[Mapping[str, Any]], m: LaneMarket, seg: tuple[int, int], base_mean: float | None,
            weights: np.ndarray | None = None) -> dict[str, Any]:
    """Economic and statistical summary of a trade list (uniform statistics; weights affect only the
    ``weighted_*`` fields, never the p-value)."""
    a, b = seg
    out: dict[str, Any] = {"segment": [m.label(a), m.label(b - 1)] if b > a else None, "trades": len(trades),
                           "baseline_mean_net": base_mean,
                           "buy_hold_return": float(m.close[b - 1] / m.open[a] - 1) if b > a else None}
    if not trades:
        out.update({"effective_n": 0, "mean_net": None, "excess_mean": None, "t_stat": None, "p_value": None})
        return out
    net = np.array([t["net_return"] for t in trades])
    gross = np.array([t["gross_return"] for t in trades])
    excess = net - (base_mean or 0.0)
    if m.intraday:
        sid = np.array([t["session_id"] for t in trades])
        uniq, inv = np.unique(sid, return_inverse=True)
        clusters = np.bincount(inv, weights=excess) / np.bincount(inv)
    else:
        clusters = excess
    n_eff = int(clusters.size)
    if n_eff >= 3 and float(np.std(clusters, ddof=1)) > 0:
        tstat = float(clusters.mean() / (np.std(clusters, ddof=1) / math.sqrt(n_eff)))
        p = float(sps.t.sf(tstat, df=n_eff - 1))
    else:
        tstat, p = None, None
    pos, neg = net[net > 0].sum(), net[net < 0].sum()
    eq = np.cumprod(1 + net)
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    span_years = max((b - a) / m.bars_per_year, 1e-9)
    per_year = len(trades) / span_years
    sd = float(net.std(ddof=1)) if net.size > 1 else 0.0
    down = net[net < 0]
    dsd = float(np.sqrt(np.mean(down ** 2))) if down.size else 0.0
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    out.update({
        "effective_n": n_eff, "independence_unit": "session" if m.intraday else "trade",
        "mean_net": float(net.mean()), "median_net": float(np.median(net)), "mean_gross": float(gross.mean()),
        "total_net_compounded": float(eq[-1] - 1), "total_gross_compounded": float(np.prod(1 + gross) - 1),
        "win_rate": float((net > 0).mean()),
        "profit_factor": float(pos / -neg) if neg < 0 else (None if pos == 0 else float("inf")),
        "max_drawdown": float((eq / peak - 1).min()), "excess_mean": float(excess.mean()),
        "t_stat": tstat, "p_value": p,
        "sharpe_annualized": float(net.mean() / sd * math.sqrt(per_year)) if sd > 0 else None,
        "sortino_annualized": float(net.mean() / dsd * math.sqrt(per_year)) if dsd > 0 else None,
        "trades_per_year": per_year, "avg_bars_held": float(np.mean([t["bars_held"] for t in trades])),
        "exposure_fraction": float(sum(t["bars_held"] for t in trades) / max(1, b - a)),
        "mean_mfe": float(np.mean([t["mfe"] for t in trades])), "mean_mae": float(np.mean([t["mae"] for t in trades])),
        "exit_reasons": dict(sorted(reasons.items())),
    })
    if weights is not None and weights.size == net.size and weights.sum() > 0:
        kish = float(weights.sum() ** 2 / (weights ** 2).sum())
        out.update({"weighted_mean_net": _weighted_mean(net, weights), "weighted_excess_mean": _weighted_mean(excess, weights),
                    "weighted_effective_n": kish})
    return out


def fold_metrics(trades: Sequence[Mapping[str, Any]], seg: tuple[int, int], folds: int, base_mean: float | None
                 ) -> list[dict[str, Any]]:
    a, b = seg
    edges = np.linspace(a, b, folds + 1).astype(int)
    rows = []
    for i in range(folds):
        vals = [t["net_return"] - (base_mean or 0.0) for t in trades if edges[i] <= t["t"] < edges[i + 1]]
        rows.append({"fold": i + 1, "n": len(vals), "mean_excess": float(np.mean(vals)) if vals else None})
    return rows


def bh_adjust(pvalues: Sequence[float | None], total_tests: int) -> list[float | None]:
    """Benjamini-Hochberg q-values; ``None`` p-values count as tests (p = 1) but stay ``None``."""
    mtot = max(total_tests, len(pvalues))
    filled = np.array([1.0 if p is None else p for p in pvalues])
    if filled.size == 0:
        return []
    order = np.argsort(filled, kind="stable")
    ranked = filled[order] * mtot / np.arange(1, filled.size + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    out = np.empty_like(q)
    out[order] = q
    return [None if p is None else float(v) for p, v in zip(pvalues, out)]
