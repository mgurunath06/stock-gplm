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
import json
import math
import os
from pathlib import Path
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
# v5: with the swing exit, intraday trades have no bar limit: stop, target or the 15:15 square-off only.
ENGINE_CONTRACT = "stock-gplm.strategy-engine.v5"


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
    # owner 2026-11-20: when the swing level is further than this from entry, the stop goes beyond the
    # signal candle's own low (high for shorts) instead. None = always the swing level.
    swing_max: float | None = None
    # owner 2026-11-22: fixed price stop / target as a fraction of entry (0.01 = 1%), and intraday positions
    # held overnight (no 15:15 square-off, entries allowed on any candle) - 15m legs run over several days.
    stop_pct: float | None = None
    target_pct: float | None = None
    overnight: bool = False
    # owner 2026-11-22 structure exits: swing pivot strength (2 = minor, the default rule; 5 = major swing),
    # target at EMA(n) of the signal candle (no trade when price is already beyond it), minimum reward/risk.
    swing_strength: int = 2
    target_ema: int | None = None
    min_rr: float | None = None
    # owner strategy 2026-11-22 (EMA13 x EMA20): stop `pullback_buffer` beyond the lowest low (long) / highest high
    # (short) of the pullback = the last run of candles with EMA(fast) below (short: above) EMA(slow), up to the signal
    # candle. Can be days back.
    # pullback_mode "last_swing": the most recent pivot low (pullback_strength candles each side, confirmed by the signal
    # candle) inside that run instead of its lowest low (falls back to the lowest low when there is none).
    pullback_fast: int | None = None
    pullback_slow: int = 20
    pullback_buffer: float = 0.0
    pullback_lookback: int = 400
    pullback_mode: str = "lowest"
    pullback_strength: int = 1
    # owner conditional exit: signal close below (short: above) EMA(anchor) -> target = EMA(anchor) (live: the EMA of the
    # previous candle, i.e. known at each candle's open; fixed: the signal candle's EMA), reached within regime_margin
    # (0.001 = 0.1%); otherwise hold until EMA(trail) is breached: "close" = a close beyond it (exit at that close),
    # "next_open" = same, exit next open, "touch" = the low (short: high) trades through the previous candle's EMA
    # (exit there), "none" = no EMA exit.
    # regime_on_target "trail" (owner trailing A): reaching the target switches to the EMA(trail) exit instead of exiting.
    # swing_trail k > 0 (trailing B): raise the stop to each new pivot low (k candles each side) formed after entry.
    # breakeven_r (trailing C): at +R x risk move the stop to entry. stop_close: the stop fires on a CLOSE beyond it.
    regime_anchor: int | None = None
    regime_trail: int = 20
    regime_fixed: bool = False
    regime_breach: str = "close"
    regime_margin: float = 0.0
    regime_on_target: str = "exit"
    swing_trail: int = 0
    breakeven_r: float | None = None
    stop_close: bool = False
    # regime_after "prev_close_or_touch" (owner 2026-11-22): after the EMA anchor was hit (on_target "trail"), exit on the
    # first close below the previous close (short: above) or a touch of EMA(trail).
    regime_after: str = ""

    @property
    def structural(self) -> bool:
        """Held to stop / target / structure exit only - no bar limit (intraday: until the 15:15 square-off)."""
        return self.swing_buffer is not None or self.pullback_fast is not None or self.regime_anchor is not None

    @staticmethod
    def of(d: Mapping[str, Any]) -> "Exit":
        sw = d.get("swing_stop") or {}
        mx = sw.get("max_swing_pct") if sw else None
        sp, tp = d.get("stop_pct"), d.get("target_pct")
        te_, mr = d.get("target_ema"), d.get("min_rr")
        pb, rg = d.get("pullback_stop") or {}, d.get("ema_regime") or {}
        return Exit(int(d["max_hold_bars"]), d.get("stop_atr"), d.get("target_atr"), d.get("trail_atr"),
                    float(sw.get("buffer", 0.002)) if sw else None, float(sw.get("rr", 2.0)),
                    int(sw.get("lookback", 20)), float(mx) if mx is not None else None,
                    float(sp) if sp else None, float(tp) if tp else None, bool(d.get("overnight", False)),
                    int(sw.get("strength", 2)) if sw else 2, int(te_) if te_ else None,
                    float(mr) if mr is not None else None,
                    int(pb.get("fast", 13)) if pb else None, int(pb.get("slow", 20)), float(pb.get("buffer", 0.0)),
                    int(pb.get("lookback", 400)), str(pb.get("mode", "lowest")), int(pb.get("strength", 1)),
                    int(rg.get("anchor", 200)) if rg else None, int(rg.get("trail", 20)),
                    rg.get("target", "live") == "fixed", str(rg.get("breach_fill", "close")),
                    float(rg.get("margin", 0.0)), str(rg.get("on_target", "exit")), int(rg.get("swing_trail", 0)),
                    float(rg["breakeven_r"]) if rg.get("breakeven_r") is not None else None,
                    bool(rg.get("stop_close", False)), str(rg.get("after_target", "")))

    def key(self) -> str:
        k = ExitPolicy(self.max_hold, self.stop_atr, self.target_atr, self.trail_atr).key()
        if self.swing_buffer is not None:
            k += f"|swing:{self.swing_buffer:g}:{self.swing_rr:g}:{self.swing_lookback}"
            if self.swing_max is not None:
                k += f":max{self.swing_max:g}"
        if self.stop_pct or self.target_pct:
            k += f"|pct:{self.stop_pct or 0:g}:{self.target_pct or 0:g}"
        if self.overnight:
            k += "|overnight"
        if self.swing_strength != 2:
            k += f"|strength{self.swing_strength}"
        if self.target_ema:
            k += f"|tema{self.target_ema}"
        if self.min_rr is not None:
            k += f"|minrr{self.min_rr:g}"
        if self.pullback_fast is not None:
            k += (f"|pullback:{self.pullback_fast}:{self.pullback_slow}:{self.pullback_buffer:g}:{self.pullback_lookback}"
                  f":{self.pullback_mode}:{self.pullback_strength}")
        if self.regime_anchor is not None:
            k += (f"|regime:{self.regime_anchor}:{self.regime_trail}:{'fixed' if self.regime_fixed else 'live'}:"
                  f"{self.regime_breach}:{self.regime_margin:g}:{self.regime_on_target}:{self.swing_trail}:"
                  f"{self.breakeven_r}:{int(self.stop_close)}:{self.regime_after}")
        return k

    def text(self, when: str | None, short: bool) -> str:
        side = "above" if short else "below"
        other = "below" if short else "above"
        parts = [f"time exit after {self.max_hold} bars at the next open (intraday: none, square-off 15:15)"
                 if self.swing_buffer is not None else f"time exit after {self.max_hold} bars at the next open"]
        if self.swing_buffer is not None:
            parts.append(f"stop {self.swing_buffer:.1%} {side} the last swing {'high' if short else 'low'}"
                         + (f" (signal candle {'high' if short else 'low'} when the swing is more than "
                            f"{self.swing_max:.1%} away)" if self.swing_max is not None else "")
                         + f", target {self.swing_rr:g}x risk")
        if self.stop_atr:
            parts.append(f"stop {self.stop_atr:g}xATR(14) {side} entry")
        if self.target_atr:
            parts.append(f"target {self.target_atr:g}xATR(14) {other} entry")
        if self.trail_atr:
            parts.append(f"trailing stop {self.trail_atr:g}xATR(14)")
        if self.stop_pct:
            parts.append(f"stop {self.stop_pct:.1%} {side} entry")
        if self.target_pct:
            parts.append(f"target {self.target_pct:.1%} {other} entry")
        if self.overnight:
            parts.append("held overnight (no 15:15 square-off)")
        if self.swing_buffer is not None and self.swing_strength != 2:
            parts.append(f"swing = lowest of {self.swing_strength} candles each side (major swing)")
        if self.target_ema:
            parts.append(f"target = EMA {self.target_ema} at the signal candle")
        if self.min_rr is not None:
            parts.append(f"no trade when reward/risk < {self.min_rr:g}")
        if self.pullback_fast is not None:
            lvl = ("last swing " + ("high" if short else "low") if self.pullback_mode == "last_swing" else
                   ("highest high" if short else "lowest low"))
            parts.append(f"stop {self.pullback_buffer:.1%} {side} the {lvl} of the pullback (last run of EMA "
                         f"{self.pullback_fast} {'above' if short else 'below'} EMA {self.pullback_slow})"
                         + (" - fires on a close beyond it" if self.stop_close else ""))
        if self.regime_anchor is not None:
            br = {"close": f"a close {'above' if short else 'below'} EMA {self.regime_trail} (exit at that close)",
                  "next_open": f"a close {'above' if short else 'below'} EMA {self.regime_trail} (exit next open)",
                  "touch": f"price touches EMA {self.regime_trail} (exit there)",
                  "none": "the stop"}[self.regime_breach]
            parts.append(f"signal close {side} EMA {self.regime_anchor}: target EMA {self.regime_anchor} "
                         f"({'fixed at the signal candle' if self.regime_fixed else 'live'}"
                         + (f", reached within {self.regime_margin:.2%}" if self.regime_margin else "") + ")"
                         + ("; on reaching it keep holding (trail)" if self.regime_on_target == "trail" else "")
                         + f"; otherwise hold until {br}")
            if self.swing_trail:
                parts.append(f"trailing stop under each new swing {'high' if short else 'low'} ({self.swing_trail} "
                             "candles each side)")
            if self.breakeven_r is not None:
                parts.append(f"stop to entry at +{self.breakeven_r:g}R")
            if self.regime_after == "prev_close_or_touch":
                parts.append(f"after EMA {self.regime_anchor} is hit: exit on a close {'above' if short else 'below'} the "
                             f"previous close or a touch of EMA {self.regime_trail}")
        if when:
            parts.append(f"exit at next open when: {when}")
        return "; ".join(parts)


# GPLM_EXITS_FILE (2026-11-21): use / learn into another exits file, e.g. full-history exits side by side with 2026's.
EXITS_FILE = Path(os.environ.get("GPLM_EXITS_FILE")
                  or Path(__file__).resolve().parents[3] / "config" / "strategies" / "exits.json")
_EXITS: dict[str, Any] | None = None


SWING_KEYS = ("buffer", "rr", "lookback", "max_swing_pct")


def _exits_doc(path: Path | None = None) -> dict[str, Any]:
    global _EXITS
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if _EXITS is None:
        _EXITS = json.loads(EXITS_FILE.read_text(encoding="utf-8")) if EXITS_FILE.is_file() else {}
    return _EXITS


def stock_exits(symbol: str, path: Path | None = None) -> dict[str, Any]:
    """Everything exits.json says about one stock (default + its entry): part of research's lane cache key."""
    doc = _exits_doc(path)
    return {"default": doc.get("default") or {},
            "stock": (doc.get("stocks") or {}).get(symbol.upper().removesuffix("-EQ")) or {}}


def swing_settings(symbol: str, timeframe: str, path: Path | None = None, strategy_id: str | None = None
                   ) -> dict[str, Any]:
    """Swing-exit settings from exits.json, most specific wins: default < stock "*" < stock timeframe <
    stock timeframe strategies.<id>. Learned by ``forward_test --learn-exits`` (owner 2026-11-20: per stock,
    per strategy - granular learnings)."""
    se = stock_exits(symbol, path)
    st = se["stock"]
    tfp = st.get(timeframe) or {}
    out = dict(se["default"])
    for part in (st.get("*"), tfp, (tfp.get("strategies") or {}).get(strategy_id) if strategy_id else None):
        if part:
            out.update({k: v for k, v in part.items() if k in SWING_KEYS})
    return out


def exit_for(doc_exit: Mapping[str, Any], symbol: str = "", timeframe: str = "", strategy_id: str | None = None) -> Exit:
    """Strategy exit with the stock's (and strategy's) swing settings applied - only to swing-exit strategies."""
    d = dict(doc_exit)
    if d.get("swing_stop") is not None:
        d["swing_stop"] = {**d["swing_stop"], **swing_settings(symbol, timeframe, strategy_id=strategy_id)}
    return Exit.of(d)


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


def swing_level_k(low: np.ndarray, high: np.ndarray, t: int, entry: float, short: bool, lookback: int, k: int
                  ) -> float | None:
    """Last swing low (long) / high (short) with `k` candles on each side, confirmed by candle t, within
    `lookback` candles, on the protective side of entry. No fallback (a major swing must exist)."""
    for i in range(t - k, max(k, t - lookback) - 1, -1):
        if not short:
            v = low[i]
            if v < entry and v <= low[i - k:i + k + 1].min():
                return float(v)
        else:
            v = high[i]
            if v > entry and v >= high[i - k:i + k + 1].max():
                return float(v)
    return None


def pullback_level(low: np.ndarray, high: np.ndarray, fast: np.ndarray, slow: np.ndarray, t: int, short: bool,
                   lookback: int, mode: str = "lowest", strength: int = 1, entry: float | None = None
                   ) -> tuple[float, int] | None:
    """Lowest low (long) / highest high (short) of the pullback before signal candle t, and its candle.

    Pullback = the most recent run of candles with fast EMA below slow EMA (short: above) ending at or before t,
    searched within `lookback` candles; the level covers that run's first candle up to t (the signal candle).
    None when no such run is found."""
    lo_k = max(0, t - lookback)
    k = t
    pull = (lambda i: fast[i] > slow[i]) if short else (lambda i: fast[i] < slow[i])
    while k >= lo_k and not pull(k):
        k -= 1
    if k < lo_k:
        return None
    s = k
    while s - 1 >= lo_k and pull(s - 1):
        s -= 1
    x = high if short else low
    if mode == "last_swing":  # most recent pivot in [s, t], confirmed by candle t, on the protective side of entry
        for i in range(t - strength, s - 1, -1):
            if i - strength < 0:
                break
            w = x[i - strength:i + strength + 1]
            v = float(x[i])
            if (v >= w.max() if short else v <= w.min()) and (entry is None or (v > entry if short else v < entry)):
                return v, i
    seg = x[s:t + 1]
    i = int(np.argmax(seg) if short else np.argmin(seg))
    return float(seg[i]), s + i


_EMA_CACHE: dict[tuple[int, int], tuple[Any, np.ndarray]] = {}


def _ema_of(m: LaneMarket, n: int) -> np.ndarray:
    """EMA(n) of close - the same function the strategy DSL uses - cached per market lane."""
    hit = _EMA_CACHE.get((id(m), n))
    if hit is not None and hit[0] is m:
        return hit[1]
    from .strategy_dsl import ema
    arr = np.asarray(ema(np.asarray(m.close, dtype=float), int(n)), dtype=float)
    if len(_EMA_CACHE) > 64:
        _EMA_CACHE.clear()
    _EMA_CACHE[(id(m), n)] = (m, arr)
    return arr


def _path(m: LaneMarket, t: int, p: Exit, last: int, short: bool, xmask: np.ndarray | None,
          entry_px: float | None = None) -> tuple[int, float, str] | None:
    # entry_px: a limit fill inside candle t+1 (owner pullback entry) instead of its open
    entry = m.open[t + 1] if entry_px is None else entry_px
    atr = m.natr[t] * entry if math.isfinite(m.natr[t]) else float("nan")
    if (p.stop_atr or p.target_atr or p.trail_atr) and not math.isfinite(atr):
        return None
    sgn = -1.0 if short else 1.0
    stop = entry - sgn * p.stop_atr * atr if p.stop_atr else (math.inf if short else -math.inf)
    target = entry + sgn * p.target_atr * atr if p.target_atr else (-math.inf if short else math.inf)
    if p.swing_buffer is not None:
        lvl = (swing_level(m.low, m.high, t, entry, short, p.swing_lookback) if p.swing_strength == 2 else
               swing_level_k(m.low, m.high, t, entry, short, p.swing_lookback, p.swing_strength))
        if lvl is not None and p.swing_max is not None and abs(entry - lvl) / entry > p.swing_max:
            c = float(m.high[t]) if short else float(m.low[t])  # signal candle low / high
            lvl = c if (c > entry if short else c < entry) else None
        if lvl is None:
            return None  # entry opened beyond the swing level: no valid stop, no trade
        stop = lvl * (1 + p.swing_buffer) if short else lvl * (1 - p.swing_buffer)
        target = entry - p.swing_rr * (stop - entry) if short else entry + p.swing_rr * (entry - stop)
    if p.stop_pct:
        stop = entry * (1 + p.stop_pct) if short else entry * (1 - p.stop_pct)
    if p.target_pct:
        target = entry * (1 - p.target_pct) if short else entry * (1 + p.target_pct)
    if p.target_ema:
        lv = float(_ema_of(m, p.target_ema)[t])
        if not math.isfinite(lv) or (lv >= entry if short else lv <= entry):
            return None  # price already beyond the EMA target: no room, no trade
        target = lv
    if p.pullback_fast is not None:
        pl = pullback_level(m.low, m.high, _ema_of(m, p.pullback_fast), _ema_of(m, p.pullback_slow), t, short,
                            p.pullback_lookback, p.pullback_mode, p.pullback_strength, entry)
        if pl is None or (pl[0] <= entry if short else pl[0] >= entry):
            return None  # no pullback found, or entry opened beyond it: no valid stop, no trade
        stop = pl[0] * (1 + p.pullback_buffer) if short else pl[0] * (1 - p.pullback_buffer)
    if p.min_rr is not None and math.isfinite(stop) and math.isfinite(target) and stop != entry:
        if abs(target - entry) / abs(entry - stop) < p.min_rr:
            return None
    sess = m.intraday and not p.overnight  # session square-off applies
    time_bar = t + 1 + p.max_hold
    if sess:
        sess_end = int(m.exit_last[t + 1])  # square-off bar (15:15 IST), owner rule 2026-11-20
        if sess_end < t + 1:
            return None
        if p.structural:  # owner rule: intraday holds to stop / target / 15:15, no bar limit
            time_bar = sess_end + 1
        hold_end = min(time_bar - 1, sess_end)
    else:
        sess_end = -1
        hold_end = time_bar - 1
    # owner 2026-11-20: daily/weekly swing trades hold until stop or target - no bar limit. A trade still
    # running at the end of the range is closed there at the close and labelled OPEN_END.
    hold_open = p.structural and not sess
    if hold_open:
        time_bar = last + 1
        hold_end = last
    if not hold_open and (hold_end > last or (not sess and time_bar > last)):
        return None
    if hold_end < t + 1:
        return None
    # hold-to-target: the path ends at the exit, not at the range end - checked on the real path in simulate()
    # (2026-11-21: one blocked bar used to drop every earlier daily trade, e.g. all RELIANCE trades before 2009-05-18)
    if not hold_open and m._blocked_cum[min(time_bar, len(m) - 1) + 1] - m._blocked_cum[t + 1] > 0:
        return None
    if p.regime_anchor is not None:
        return _regime_loop(m, t, p, last, short, entry, stop, hold_end, sess, sess_end, hold_open, time_bar)
    simple = not (p.stop_atr or p.target_atr or p.trail_atr or p.swing_buffer is not None or p.stop_pct
                  or p.target_pct or p.target_ema or p.structural) and xmask is None
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
                if sess and j >= sess_end:
                    return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
                if j + 1 > last:  # exit signal on the latest candle: the next open is not known yet
                    if hold_open:
                        return last, float(m.close[last]), "OPEN_END"
                elif j + 1 <= time_bar:
                    return j + 1, float(m.open[j + 1]), "SIGNAL_EXIT"
    if sess and time_bar > sess_end:
        return sess_end, float(m.close[sess_end]), "SESSION_CLOSE"
    if hold_open:
        return last, float(m.close[last]), "OPEN_END"
    return time_bar, float(m.open[time_bar]), "TIME"


def _regime_loop(m: LaneMarket, t: int, p: Exit, last: int, short: bool, entry: float, stop: float, hold_end: int,
                 sess: bool, sess_end: int, hold_open: bool, time_bar: int) -> tuple[int, float, str] | None:
    """Owner conditional exit (see Exit.regime_*): written for longs, shorts mirrored by the sign sg."""
    ea, et = _ema_of(m, p.regime_anchor), _ema_of(m, p.regime_trail)
    a0, c0 = float(ea[t]), float(m.close[t])
    if not math.isfinite(a0) or not math.isfinite(float(et[t])):
        return None
    sg = -1.0 if short else 1.0  # x * sg rises when the trade gains
    o_, h_, l_, c_ = m.open, m.high, m.low, m.close
    risk = (entry - stop) * sg
    # signal candle on the far side of the anchor and the entry still short of it -> target the anchor
    mode = "target" if (c0 * sg < a0 * sg and entry * sg < a0 * sg) else "trail"
    target = a0
    k = p.swing_trail
    hit = False
    for j in range(t + 1, hold_end + 1):
        o, hi, lo, cl = float(o_[j]), float(h_[j]), float(l_[j]), float(c_[j])
        fav, adv = (lo, hi) if short else (hi, lo)  # favourable / adverse extreme of the candle
        # trailing B: a pivot formed after entry and confirmed by the previous candle raises the stop
        if k and j - 1 - k > t:
            i = j - 1 - k
            w = (h_ if short else l_)[i - k:i + k + 1]
            v = float((h_ if short else l_)[i])
            if (v >= w.max() if short else v <= w.min()) and v * sg > stop * sg:
                stop = v
        # stop (touch)
        if not p.stop_close:
            if j > t + 1 and o * sg <= stop * sg:
                return j, o, "STOP_GAP"
            if adv * sg <= stop * sg:
                return j, float(stop), "STOP" if (stop - entry) * sg < 0 else "TRAIL"
        # target = EMA anchor
        if mode == "target":
            if not p.regime_fixed and j > t + 1 and math.isfinite(float(ea[j - 1])):
                target = float(ea[j - 1])  # live anchor, known at this candle's open
            lvl = target * (1 - sg * p.regime_margin)
            hit_gap = j > t + 1 and o * sg >= lvl * sg
            if hit_gap or fav * sg >= lvl * sg:
                if p.regime_on_target != "trail":
                    return (j, o, "TARGET_GAP") if hit_gap else (j, float(lvl), "TARGET")
                mode, hit = "trail", True  # trailing A: keep holding, now on the EMA trail exit (from the next candle)
        # after the anchor was hit (owner): close beyond the previous close, or EMA trail touch
        elif hit and p.regime_after == "prev_close_or_touch":
            lv = float(et[j - 1])
            if math.isfinite(lv) and adv * sg <= lv * sg:
                return j, (o if o * sg < lv * sg else lv), "EMA_TOUCH"
            if cl * sg < float(c_[j - 1]) * sg:
                return j, cl, "CLOSE_BEYOND_PREV"
        # EMA trail exit
        elif p.regime_breach == "touch":
            lv = float(et[j - 1])  # previous candle's EMA, known at this candle's open
            if math.isfinite(lv) and adv * sg <= lv * sg:
                return j, (o if o * sg < lv * sg else lv), "EMA_TOUCH"
        elif p.regime_breach in ("close", "next_open") and cl * sg < float(et[j]) * sg:
            if p.regime_breach == "close" or (sess and j >= sess_end):
                return j, cl, "EMA_BREACH"
            if j + 1 > last:  # breach on the latest candle: the next open is not known yet
                if hold_open:
                    return last, float(c_[last]), "OPEN_END"
            else:
                return j + 1, float(o_[j + 1]), "EMA_BREACH"
        if p.stop_close and cl * sg <= stop * sg:
            return j, cl, "STOP_CLOSE" if (stop - entry) * sg < 0 else "TRAIL"
        # trailing C: at +R move the stop to entry
        if p.breakeven_r is not None and risk > 0 and (fav - entry) * sg >= p.breakeven_r * risk \
                and stop * sg < entry * sg:
            stop = entry
    if sess and time_bar > sess_end:
        return sess_end, float(c_[sess_end]), "SESSION_CLOSE"
    if hold_open:
        return last, float(c_[last]), "OPEN_END"
    return time_bar, float(o_[time_bar]), "TIME"


def simulate(m: LaneMarket, mask: np.ndarray, seg: tuple[int, int], p: Exit, buy: float, sell: float,
             short: bool = False, xmask: np.ndarray | None = None, independent: bool = True,
             fired: np.ndarray | None = None, fills: Mapping[int, tuple[int, float] | None] | None = None
             ) -> list[dict[str, Any]]:
    """fills (owner pullback entry): signal t -> (candle j, price) = a limit fill inside candle j instead of the next
    open, or None = no entry for that signal. The trade then counts from candle j - 1 ("t")."""
    a, b = seg
    last = b - 1
    if fired is None:
        fired = np.nonzero(mask[a:b] & ~m.no_signal[a:b])[0] + a
    trades: list[dict[str, Any]] = []
    nxt = -1
    sid = m.session_id
    hold = p.structural and not (m.intraday and not p.overnight)
    for sig_t in fired.tolist():
        t, epx = sig_t, None
        if fills is not None and sig_t in fills:
            if fills[sig_t] is None:
                continue
            j, epx = fills[sig_t]
            t = j - 1
        if sig_t < nxt or t < nxt or t + 1 > last:
            continue
        if m.intraday and not p.overnight and (sid[t + 1] != sid[t] or not m.entry_ok[t]):
            continue
        fill = _path(m, t, p, last, short, xmask, epx)
        if fill is None:
            continue
        e, px, reason = fill
        if hold and m._blocked_cum[min(e, len(m) - 1) + 1] - m._blocked_cum[t + 1] > 0:
            continue  # the trade's own path touches a blocked (discontinuity) bar
        ent = float(m.open[t + 1]) if epx is None else float(epx)
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
                       "mfe": mfe, "mae": mae, "signal_t": sig_t})
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
    symbol: str = ""  # for per-stock exit settings (config/strategies/exits.json)
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
    ex = exit_for(s.doc["exit"], li.symbol, li.timeframe, s.id)
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
