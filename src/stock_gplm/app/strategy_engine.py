"""Generic strategy execution engine: one code path for catalogue, user, AI and ML strategies.

For one stock x timeframe lane and a list of :class:`~stock_gplm.app.strategy_library.StrategyDef`:

1. **Applicability / data gate.** Wrong timeframe, missing benchmark or lane feature, or a declared
   ``blocked`` dependency produce an explicit ``BLOCKED_*`` row; nothing is approximated silently.
2. **Parameter optimisation (discovery segment only).** Every grid combination is simulated on the
   first 60% of the development window. The combination with the best cost-adjusted t-statistic of
   excess return (minimum trade count enforced) is chosen; ties keep the declared defaults.
3. **Validation (one read).** The chosen configuration is simulated once on the purged validation
   segment: trade metrics, walk-forward folds, regime breakdown and a one-sided p-value of excess
   return over the same-exit unconditional baseline.
4. **Final development segment.** Only after the lane's Benjamini-Hochberg family (every validated
   configuration on the lane, catalogue + library + discovered) passes the gates — done by the caller.

Fills follow :mod:`stock_gplm.m54.backtest`: signal at the close of bar t, entry at the open of bar
t+1, stop checked before target on the same bar, gaps fill at the worse open, trades must finish
inside their segment, no overlapping trades, paths touching discontinuity bars are skipped.
``direction = short`` mirrors this; on daily/weekly lanes it is labelled ``SHORT_SYNTHETIC``
(research only — an overnight cash-equity short is not available in India; read it as a
futures-like short with the same cost model).
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.backtest import ExitPolicy, LaneMarket, fold_metrics, metrics
from . import strategy_dsl as dsl
from .strategy_library import StrategyDef

# v2 (2026-11-15): the unconditional baseline applies the strategy's signal exit (exit.when) too, so the
# excess-return test compares like with like; trade ledgers are returned for the audit trail.
# v3 (2026-11-20): intraday square-off at 15:15 IST and no new entries after 14:45 IST (owner session rules).
# v4 (2026-11-20): owner swing exit (exit.swing_stop: stop 0.2% beyond the last swing low/high, target 2x risk).
ENGINE_CONTRACT = "stock-gplm.strategy-engine.v4"


@dataclass(frozen=True)
class Exit:
    max_hold: int
    stop_atr: float | None
    target_atr: float | None
    trail_atr: float | None
    # owner swing exit (2026-11-20): stop `swing_buffer` beyond the last swing low (long) / high (short),
    # target = `swing_rr` x risk. Swing = latest confirmed 2-2 pivot within `swing_lookback` bars on the
    # right side of entry; fallback: lowest low / highest high of the last 5 bars.
    swing_buffer: float | None = None
    swing_rr: float = 2.0
    swing_lookback: int = 20

    @staticmethod
    def of(d: Mapping[str, Any]) -> "Exit":
        sw = d.get("swing_stop") or {}
        return Exit(int(d["max_hold_bars"]), d.get("stop_atr"), d.get("target_atr"), d.get("trail_atr"),
                    float(sw.get("buffer", 0.002)) if sw else None, float(sw.get("rr", 2.0)),
                    int(sw.get("lookback", 20)))

    def key(self) -> str:
        k = ExitPolicy(self.max_hold, self.stop_atr, self.target_atr, self.trail_atr).key()
        if self.swing_buffer is not None:
            k += f"|swing:{self.swing_buffer:g}:{self.swing_rr:g}:{self.swing_lookback}"
        return k

    def text(self, when: str | None, short: bool) -> str:
        side = "above" if short else "below"
        other = "below" if short else "above"
        parts = [f"time exit after {self.max_hold} bars at the next open"]
        if self.swing_buffer is not None:
            parts.append(f"stop {self.swing_buffer:.1%} {side} the last swing {'high' if short else 'low'}, "
                         f"target {self.swing_rr:g}x risk")
        if self.stop_atr:
            parts.append(f"stop {self.stop_atr:g}xATR(14) {side} entry")
        if self.target_atr:
            parts.append(f"target {self.target_atr:g}xATR(14) {other} entry")
        if self.trail_atr:
            parts.append(f"trailing stop {self.trail_atr:g}xATR(14)")
        if when:
            parts.append(f"exit at next open when: {when}")
        return "; ".join(parts)


def swing_level(low: np.ndarray, high: np.ndarray, t: int, entry: float, short: bool, lookback: int) -> float | None:
    """Last swing low (long) / high (short) known at the close of bar t, on the protective side of entry.

    Pivot k: its low is the lowest of k-2..k+2 (high: highest), so it is confirmed by bar k+2 <= t.
    Fallback: lowest low / highest high of bars t-4..t. None when no level is on the right side."""
    lo_k = max(2, t - lookback)
    for k in range(t - 2, lo_k - 1, -1):
        if not short:
            v = low[k]
            if v < entry and v <= low[k - 2:k + 3].min():
                return float(v)
        else:
            v = high[k]
            if v > entry and v >= high[k - 2:k + 3].max():
                return float(v)
    w = slice(max(0, t - 4), t + 1)
    v = float(high[w].max()) if short else float(low[w].min())
    return v if (v > entry if short else v < entry) else None


def _path(m: LaneMarket, t: int, p: Exit, last: int, short: bool, xmask: np.ndarray | None
          ) -> tuple[int, float, str] | None:
    entry = m.open[t + 1]
    atr = m.natr[t] * entry if math.isfinite(m.natr[t]) else float("nan")
    if (p.stop_atr or p.target_atr or p.trail_atr) and not math.isfinite(atr):
        return None
    sgn = -1.0 if short else 1.0
    stop = entry - sgn * p.stop_atr * atr if p.stop_atr else (math.inf if short else -math.inf)
    target = entry + sgn * p.target_atr * atr if p.target_atr else (-math.inf if short else math.inf)
    if p.swing_buffer is not None:
        lvl = swing_level(m.low, m.high, t, entry, short, p.swing_lookback)
        if lvl is None:
            return None  # entry opened beyond the swing level: no valid stop, no trade
        stop = lvl * (1 + p.swing_buffer) if short else lvl * (1 - p.swing_buffer)
        target = entry - p.swing_rr * (stop - entry) if short else entry + p.swing_rr * (entry - stop)
    time_bar = t + 1 + p.max_hold
    if m.intraday:
        sess_end = int(m.exit_last[t + 1])  # square-off bar (15:15 IST), owner rule 2026-11-20
        if sess_end < t + 1:
            return None
        hold_end = min(time_bar - 1, sess_end)
    else:
        sess_end = -1
        hold_end = time_bar - 1
    if hold_end > last or (not m.intraday and time_bar > last):
        return None
    if m._blocked_cum[min(time_bar, len(m) - 1) + 1] - m._blocked_cum[t + 1] > 0:
        return None
    simple = not (p.stop_atr or p.target_atr or p.trail_atr or p.swing_buffer is not None) and xmask is None
    if not simple:
        water = entry
        o_, h_, l_ = m.open, m.high, m.low
        for j in range(t + 1, hold_end + 1):
            o, h, lo = o_[j], h_[j], l_[j]
            if not short:
                if j > t + 1 and o <= stop:
                    return j, float(o), "STOP_GAP"
                if lo <= stop:
                    return j, float(stop), "STOP" if not p.trail_atr or stop < entry else "TRAIL"
                if j > t + 1 and o >= target:
                    return j, float(o), "TARGET_GAP"
                if h >= target:
                    return j, float(target), "TARGET"
                if p.trail_atr:
                    water = max(water, h)
                    stop = max(stop, water - p.trail_atr * atr)
            else:
                if j > t + 1 and o >= stop:
                    return j, float(o), "STOP_GAP"
                if h >= stop:
                    return j, float(stop), "STOP" if not p.trail_atr or stop > entry else "TRAIL"
                if j > t + 1 and o <= target:
                    return j, float(o), "TARGET_GAP"
                if lo <= target:
                    return j, float(target), "TARGET"
                if p.trail_atr:
                    water = min(water, lo)
                    stop = min(stop, water + p.trail_atr * atr)
            if xmask is not None and xmask[j]:
                if m.intraday and j >= sess_end:
                    return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
                if j + 1 <= time_bar:
                    return j + 1, float(m.open[j + 1]), "SIGNAL_EXIT"
    if m.intraday and time_bar > sess_end:
        return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
    return time_bar, float(m.open[time_bar]), "TIME"


def simulate(m: LaneMarket, mask: np.ndarray, seg: tuple[int, int], p: Exit, buy: float, sell: float,
             short: bool = False, xmask: np.ndarray | None = None, independent: bool = True,
             fired: np.ndarray | None = None) -> list[dict[str, Any]]:
    a, b = seg
    last = b - 1
    if fired is None:
        fired = np.nonzero(mask[a:b] & ~m.no_signal[a:b])[0] + a
    trades: list[dict[str, Any]] = []
    nxt = -1
    sid = m.session_id
    for t in fired.tolist():
        if t < nxt or t + 1 > last:
            continue
        if m.intraday and (sid[t + 1] != sid[t] or not m.entry_ok[t]):
            continue
        fill = _path(m, t, p, last, short, xmask)
        if fill is None:
            continue
        e, px, reason = fill
        ent = float(m.open[t + 1])
        if short:
            net = (ent * (1 - sell) - px * (1 + buy)) / ent
            gross = 1 - px / ent
            hi = float(m.high[t + 1:e + 1].max()) if e >= t + 1 else ent
            lo = float(m.low[t + 1:e + 1].min()) if e >= t + 1 else ent
            mfe, mae = 1 - lo / ent, 1 - hi / ent
        else:
            net = px * (1 - sell) / (ent * (1 + buy)) - 1
            gross = px / ent - 1
            hi = float(m.high[t + 1:e + 1].max()) if e >= t + 1 else ent
            lo = float(m.low[t + 1:e + 1].min()) if e >= t + 1 else ent
            mfe, mae = hi / ent - 1, lo / ent - 1
        trades.append({"t": t, "exit_index": e, "session_id": int(sid[t]), "entry_price": ent, "exit_price": px,
                       "gross_return": gross, "net_return": net, "bars_held": e - t, "exit_reason": reason,
                       "mfe": mfe, "mae": mae})
        if independent:
            nxt = e
    return trades


def _mask_key(x: np.ndarray | None) -> str | None:
    if x is None:
        return None
    return hashlib.sha256(np.packbits(np.asarray(x, bool)).tobytes()).hexdigest()[:20]


def baseline(m: LaneMarket, seg: tuple[int, int], p: Exit, buy: float, sell: float, short: bool,
             cache: dict[Any, Any], max_starts: int = 1500, xmask: np.ndarray | None = None) -> float | None:
    """Mean net return of the *same* exit policy entered on every eligible bar of the segment.

    The exit policy includes the signal exit ``xmask`` (``exit.when``): a rule that leaves early on a
    signal is compared with a baseline that leaves on the same signal, never with a baseline held to
    the time/stop exit (that mismatch inflated or deflated excess returns before engine v2).
    """
    key = (seg, p.key(), short, _mask_key(xmask))
    if key in cache:
        return cache[key]
    a, b = seg
    stride = max(1, (b - a) // max_starts)
    fired = np.arange(a, b, stride)
    fired = fired[~m.no_signal[fired]]
    tr = simulate(m, np.ones(len(m), bool), seg, p, buy, sell, short, xmask, independent=False, fired=fired)
    cache[key] = float(np.mean([x["net_return"] for x in tr])) if tr else None
    return cache[key]


def _score(trades: Sequence[Mapping[str, Any]], base: float | None, min_trades: int) -> float:
    if len(trades) < min_trades:
        return -math.inf
    x = np.array([t["net_return"] for t in trades]) - (base or 0.0)
    sd = float(x.std(ddof=1)) if x.size > 1 else 0.0
    return float(x.mean() / (sd / math.sqrt(x.size))) if sd > 0 else -math.inf


TRADE_FIELDS = ("t", "exit_index", "entry_price", "exit_price", "exit_reason", "gross_return", "net_return",
                "bars_held", "mfe", "mae")


def _compact(t: Mapping[str, Any]) -> list[Any]:
    """One trade as a short list in :data:`TRADE_FIELDS` order (cached as JSON, written to the audit ledger)."""
    return [int(t["t"]), int(t["exit_index"]), round(float(t["entry_price"]), 6), round(float(t["exit_price"]), 6),
            t["exit_reason"], round(float(t["gross_return"]), 8), round(float(t["net_return"]), 8),
            int(t["bars_held"]), round(float(t["mfe"]), 8), round(float(t["mae"]), 8)]


def regime_breakdown(trades: Sequence[Mapping[str, Any]], regimes: Mapping[str, np.ndarray]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, mask in regimes.items():
        sel = [t["net_return"] for t in trades if mask[t["t"]]]
        if sel:
            a = np.array(sel)
            out[name] = {"n": int(a.size), "mean_net": round(float(a.mean()), 6), "win_rate": round(float((a > 0).mean()), 4)}
        else:
            out[name] = {"n": 0}
    return out


def lane_regimes(ctx: dsl.Context, m: LaneMarket, train_end: int) -> dict[str, np.ndarray]:
    """BULL/BEAR (close vs SMA200 at the signal bar), HIGH/LOW volatility (ATR% vs the discovery-segment median)."""
    trend = dsl.sma(ctx.close, 200)
    with np.errstate(invalid="ignore"):
        bull = ctx.close > trend
        bear = ctx.close <= trend
    v = m.natr
    med = float(np.nanmedian(v[:max(1, train_end)])) if np.isfinite(v[:max(1, train_end)]).any() else float("nan")
    with np.errstate(invalid="ignore"):
        return {"BULL": bull, "BEAR": bear, "HIGH_VOL": v > med, "LOW_VOL": v <= med}


def segments(n: int, purge: int) -> dict[str, tuple[int, int]] | None:
    if n < max(90, purge * 6):
        return None
    a, b = int(n * 0.60), int(n * 0.80)
    return {"discovery": (0, a), "validation": (min(n, a + purge), b), "test": (min(n, b + purge), n)}


@dataclass
class LaneInputs:
    ctx: dsl.Context
    market: LaneMarket
    buy: float
    sell: float
    intraday: bool
    timeframe: str
    folds: int
    min_trades_opt: int = 8
    audit_discovery: bool = False  # also keep discovery-segment trades of the chosen configuration


def evaluate_strategy(s: StrategyDef, li: LaneInputs, base_cache: dict[Any, Any],
                      regimes: Mapping[str, np.ndarray] | None, max_hold_cap: int | None = None) -> dict[str, Any]:
    """Optimise on discovery, read validation once. Returns a result dict (no gates applied)."""
    t0 = time.perf_counter()
    m = li.market
    short = s.direction == "short"
    res: dict[str, Any] = {"strategy_id": s.id, "direction": s.direction,
                           "position_basis": ("SHORT_SYNTHETIC" if short and not li.intraday else
                                              "SHORT_INTRADAY" if short else "LONG")}
    if s.blocked:
        res.update(status=s.blocked["status"], block_reason=s.blocked["reason"])
        return res
    ex = Exit.of(s.doc["exit"])
    segs = segments(len(m), ex.max_hold)
    if segs is None:
        res.update(status="BLOCKED_INSUFFICIENT_HISTORY",
                   block_reason=f"{len(m)} bars; purged 60/20/20 split needs >= {max(90, ex.max_hold * 6)}")
        return res
    combos = s.grid()
    best: tuple[float, dict[str, Any], int, float | None] | None = None
    masks: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
    for params in combos:
        try:
            mask = dsl.signal(s.entry_node, li.ctx, params)
            xm = dsl.signal(s.exit_node, li.ctx, params) if s.exit_node is not None else None
        except dsl.MissingData as exc:
            res.update(status="BLOCKED_MISSING_DATA", block_reason=str(exc), requires=exc.requirement)
            return res
        except dsl.DSLError as exc:
            res.update(status="BLOCKED_TIMEFRAME_SCOPE" if "intraday" in str(exc) else "INVALID_CONFIG",
                       block_reason=str(exc))
            return res
        key = repr(sorted(params.items()))
        masks[key] = (mask, xm)
        if len(combos) == 1:
            best = (0.0, params, -1, None)
            break
        tr = simulate(m, mask, segs["discovery"], ex, li.buy, li.sell, short, xm)
        bmean = baseline(m, segs["discovery"], ex, li.buy, li.sell, short, base_cache, xmask=xm)
        sc = _score(tr, bmean, li.min_trades_opt)
        is_default = params == s.defaults()
        if best is None or sc > best[0] or (sc == best[0] and is_default):
            best = (sc, params, len(tr), float(np.mean([x["net_return"] for x in tr])) if tr else None)
    assert best is not None
    params = best[1]
    mask, xm = masks[repr(sorted(params.items()))]
    if len(combos) == 1:  # discovery-segment diagnostics for single-configuration strategies
        trd = simulate(m, mask, segs["discovery"], ex, li.buy, li.sell, short, xm)
        best = (0.0, params, len(trd), float(np.mean([x["net_return"] for x in trd])) if trd else None)
    vseg = segs["validation"]
    tr = simulate(m, mask, vseg, ex, li.buy, li.sell, short, xm)
    bmean = baseline(m, vseg, ex, li.buy, li.sell, short, base_cache, xmask=xm)
    met = metrics(tr, m, vseg, bmean)
    met["folds"] = fold_metrics(tr, vseg, li.folds, bmean)
    ledger = {"validation": [_compact(x) for x in tr]}
    if li.audit_discovery:
        ledger["discovery"] = [_compact(x) for x in simulate(m, mask, segs["discovery"], ex, li.buy, li.sell, short, xm)]
    if len(tr) > 1:
        met["excess_sd"] = float(np.std(np.array([x["net_return"] for x in tr]) - (bmean or 0.0), ddof=1))
    signals = int((mask & ~m.no_signal).sum())
    res.update(status="TESTED", params=params, grid_size=len(combos), discovery_trades=best[2],
               discovery_mean_net=best[3], met=met, segs=segs, exit=ex, signals=signals,
               regimes=regime_breakdown(tr, regimes) if regimes else {},
               exit_text=ex.text(s.doc["exit"].get("when"), short), seconds=round(time.perf_counter() - t0, 3),
               baseline_mean_net=bmean, trades=ledger, _mask=mask, _xmask=xm)
    return res


def final_read(res: Mapping[str, Any], li: LaneInputs, base_cache: dict[Any, Any]) -> dict[str, Any]:
    m = li.market
    seg = res["segs"]["test"]
    short = res["direction"] == "short"
    tr = simulate(m, res["_mask"], seg, res["exit"], li.buy, li.sell, short, res["_xmask"])
    bmean = baseline(m, seg, res["exit"], li.buy, li.sell, short, base_cache, xmask=res["_xmask"])
    met = metrics(tr, m, seg, bmean)
    met["_trades"] = tr
    return met


__all__ = ["ENGINE_CONTRACT", "TRADE_FIELDS", "Exit", "LaneInputs", "baseline", "evaluate_strategy", "final_read", "lane_regimes",
           "regime_breakdown", "segments", "simulate"]
