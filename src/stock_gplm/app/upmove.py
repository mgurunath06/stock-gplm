"""Up-move learner: learn, per stock, which triggers came before past up-moves (owner design, 2026-11-20).

An **up-move** starts at candle t when buying at the next open reaches ``+target`` (default 3%) within
``horizon`` candles (default 5) before touching ``-stop`` (default 1.5%). If both levels fall in the same
candle, the stop counts first (conservative). Otherwise the trade exits at the close of the last candle.
Every net return has the lane's round-trip costs subtracted.

Triggers are basic conditions (EMA 13 crosses, MACD, RSI, volume, price action, EMA 13 stretch, ...) and
every AND-pair of them (hybrids). A trigger fires on the first candle its condition becomes true, and a new
firing within ``horizon`` candles of the last one is ignored, so overlapping trades are not double-counted.

Each trigger gets three numbers, using **all** history with weight ``(1 + age/decay_bars) ** -decay_power``
(newest candle 1; 100 back 1/4; 1000 back 1/121; never 0), shrunk towards the stock's own base rate:

* **Confidence**: weighted chance the trade ends in profit.
* **RR** (reward:risk): weighted average gain of winning trades / average loss of losing trades.
* **Edge** (the combined ranking number, in R): ``Confidence * RR - (1 - Confidence)`` = expected gain per
  1 unit of risk. Edge > 0 means the trigger has paid off; higher Edge ranks higher.

Honesty check, without a split date: for every firing, Confidence/RR/Edge are recomputed from earlier
firings only (whose trades had already closed). If Edge was > 0 at that moment, the firing counts as
**taken**; the taken firings' real results (recency-weighted) show whether the learning works, like live use.

Usage (PYTHONPATH=src):
  python -m stock_gplm.app.upmove --config config/app/gplm.windows.longdaily.json \
      --stocks RELIANCE,HDFCBANK --tf 1d --name top5
Output: ``<home>/reports/upmove/<name>/``: ``triggers.csv`` (every stock x trigger), ``today.csv`` (triggers
firing on the latest candle), ``summary.md`` and ``stocks/<SYM>-<tf>.json`` (reused while data is unchanged).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

CONTRACT = "stock-gplm.upmove.v2"  # v2 2026-11-20: gap-through stops, 15:15 square-off, 14:45 last entry, ATR label

# name -> (family, DSL condition). Pairs are only formed across different families.
SINGLES: dict[str, tuple[str, str]] = {
    "EMA13 x EMA20": ("ema", "cross_above(ema(close, 13), ema(close, 20))"),
    "EMA13 x EMA50": ("ema", "cross_above(ema(close, 13), ema(close, 50))"),
    "EMA13 x EMA100": ("ema", "cross_above(ema(close, 13), ema(close, 100))"),
    "EMA13 x EMA200": ("ema", "cross_above(ema(close, 13), ema(close, 200))"),
    "close x EMA13": ("ema", "cross_above(close, ema(close, 13))"),
    "close > EMA50": ("trend", "close > ema(close, 50)"),
    "close > EMA200": ("trend", "close > ema(close, 200)"),
    "EMA13 > EMA50": ("trend", "ema(close, 13) > ema(close, 50)"),
    "ADX > 20": ("trend", "adx(14) > 20"),
    "MACD x signal": ("macd", "cross_above(macd(close), macd_signal(close))"),
    "MACD > 0": ("macd", "macd(close) > 0"),
    "RSI x 50": ("rsi", "cross_above(rsi(close, 14), 50)"),
    "RSI x 30": ("rsi", "cross_above(rsi(close, 14), 30)"),
    "RSI 50-70": ("rsi", "rsi(close, 14) > 50 and rsi(close, 14) < 70"),
    "RSI < 35": ("rsi", "rsi(close, 14) < 35"),
    "volume > 1.5x avg": ("volume", "volume > 1.5 * sma(volume, 20)"),
    "volume surge up-day": ("volume", "volume > 2 * sma(volume, 20) and close > open"),
    "OBV > its avg": ("volume", "obv > sma(obv, 20)"),
    "close > prev high": ("price", "close > high[1]"),
    "5-candle breakout": ("price", "close > highest(high, 5)[1]"),
    "20-candle breakout": ("price", "close > highest(high, 20)[1]"),
    "bullish engulfing": ("price", "close > open and close[1] < open[1] and close > open[1] and open < close[1]"),
    "hammer": ("price", "(min(open, close) - low) > 2 * abs(close - open) and "
                        "(high - max(open, close)) < 0.2 * (high - low)"),
    "near 20-candle low": ("price", "low <= lowest(low, 20)[1] * 1.01"),
    "stretched below EMA13": ("stretch", "(close - ema(close, 13)) < -2 * atr(14)"),
    "EMA13 snap-back": ("stretch", "lowest(low - ema(close, 13), 3) < -2 * atr(14) and close > high[1] "
                                   "and close < ema(close, 13)"),
    "ATR contraction": ("vol", "atr(14) < 0.8 * sma(atr(14), 50)"),
    # owner Chartink scans that are states, not entries (2026-11-20): building blocks only, named as in Chartink
    "Near EMA200": ("context", "abs(close / ema(close, 200) - 1) <= 0.01"),
    "Bollinger band Squeeze": ("context", "bb_upper(close, 20, 2) - bb_lower(close, 20, 2) <= 2 * atr(14)"),
    "Bullish Hourly Convergence": ("context", "(max(ema(close, 20), ema(close, 50), ema(close, 100), ema(close, 200)) - "
                                              "min(ema(close, 20), ema(close, 50), ema(close, 100), ema(close, 200))) < 0.01 * close "
                                              'and htf(close, "1d") > htf(ema(close, 50), "1d") '
                                              'and htf(close, "1w") > htf(ema(close, 21), "1w")'),
    "Sowmya scan": ("context", 'htf(rsi(close, 14), "1mo") >= 60 and htf(rsi(close, 14), "1w") >= 60 and rsi(close, 14) >= 40'),
    "GM Bull EMA Sorted": ("context", "ema(close, 9) > ema(close, 13) and ema(close, 13) > ema(close, 20) and "
                                      "ema(close, 20) > ema(close, 50) and ema(close, 50) > ema(close, 100) and ema(close, 100) > ema(close, 200)"),
    "Monthly MACD Below 0": ("context", 'htf(macd(close), "1mo") > htf(macd_signal(close), "1mo") and htf(macd(close), "1mo") < 0'),
}


@dataclass(frozen=True)
class Params:
    target: float = 0.03
    stop: float = 0.015
    horizon: int = 5
    decay_bars: float = 100.0
    decay_power: float = 2.0
    prior: float = 5.0          # pseudo-firings at the stock's base rate (few firings cannot look sure)
    min_firings: int = 8        # firings in all history to be ranked
    pairs: bool = True
    triples: bool = True
    confirm: int = 150          # best screened triples scored exactly per stock
    liquidity: bool = True      # skip candles failing the liquidity gate (see liquidity_gate)


# Up-move size per timeframe: (target, stop, horizon candles). Owner set 1d = 3% in 5 days, stop 1.5%.
TF_DEFAULTS: dict[str, tuple[float, float, int]] = {
    "1w": (0.06, 0.03, 4), "1d": (0.03, 0.015, 5), "60m": (0.015, 0.0075, 6),
    "30m": (0.012, 0.006, 8), "15m": (0.01, 0.005, 8), "5m": (0.006, 0.003, 12), "1m": (0.004, 0.002, 15),
}


def liquidity_gate(tf: str) -> str:
    """Owner liquidity floor (2026-11-20): 20-session median turnover >= Rs 15 Cr and close >= Rs 50, both
    from completed sessions. Intraday lanes use the mean of the previous 20 complete sessions' volume."""
    if tf == "1w":
        return "median(volume * close, 4)[1] >= 750000000 and close[1] >= 50"
    if tf.endswith("m") or tf == "4h":
        return "avg_day_volume(20) * prev_day_close() >= 150000000 and prev_day_close() >= 50"
    return "median(volume * close, 20)[1] >= 150000000 and close[1] >= 50"


# Secondary, volatility-normalised label (owner 2026-11-20): +2 x ATR(14) before -1 x ATR(14), same horizon.
# Reported next to the % label (atr_confidence / atr_lift); ranking still uses the owner's % rule.
ATR_TARGET, ATR_STOP = 2.0, 1.0


# --------------------------------------------------------------------------- pure core


def weight(age: np.ndarray, p: Params) -> np.ndarray:
    return (1.0 + np.maximum(age, 0) / max(1e-9, p.decay_bars)) ** -p.decay_power


def outcomes(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: Params, cost: float,
             path_end: np.ndarray | None = None, target: np.ndarray | None = None,
             stop: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Per candle t: (hit, net). hit = 1 target first, 0 stop first or time-out; NaN where no full path.

    Entry at open[t+1]; the path is candles t+1 .. min(t+horizon, path_end[t]) (intraday: square-off bar).
    Without ``path_end`` a full ``horizon`` path is required. Vectorised over candles, looped over horizon.
    ``target`` / ``stop``: optional per-candle fractions (e.g. ATR-based); default ``p.target`` / ``p.stop``.
    A stop that gaps through fills at the (worse) open, as in the strategy engine.
    """
    n = len(c)
    hit = np.full(n, np.nan)
    net = np.full(n, np.nan)
    if n < 2:
        return hit, net
    t = np.arange(n - 1)
    ent = o[1:].astype(float)
    if path_end is None:
        end = t + p.horizon
        ok = end <= n - 1
    else:
        end = np.minimum(t + p.horizon, np.asarray(path_end[:-1], np.int64))
        ok = end >= t + 1
    ok &= ent > 0
    end = np.minimum(end, n - 1)
    tg = np.full(n - 1, p.target) if target is None else np.asarray(target, float)[:-1]
    st = np.full(n - 1, p.stop) if stop is None else np.asarray(stop, float)[:-1]
    ok &= np.isfinite(tg) & np.isfinite(st)
    up, dn = ent * (1 + tg), ent * (1 - st)
    done = ~ok
    rh = np.zeros(n - 1)
    rn = np.zeros(n - 1)
    for k in range(1, p.horizon + 1):
        idx = np.minimum(t + k, n - 1)
        live = ~done & (t + k <= end)
        s = live & (lo[idx] <= dn)
        rn[s] = np.minimum(o[idx][s], dn[s]) / ent[s] - 1  # gap through the stop: filled at the open
        done |= s
        g = live & ~s & (h[idx] >= up)
        rh[g], rn[g] = 1.0, tg[g]
        done |= g
    to = ok & ~done  # neither level touched: exit at the close of the last path candle
    rn[to] = c[end[to]] / ent[to] - 1
    hit[:-1] = np.where(ok, rh, np.nan)
    net[:-1] = np.where(ok, rn - cost, np.nan)
    return hit, net


def firings(mask: np.ndarray, valid: np.ndarray, horizon: int) -> np.ndarray:
    """First candle of each true run, at least ``horizon`` candles after the previous kept firing."""
    m = mask & valid
    edge = m & ~np.concatenate(([False], m[:-1]))
    out, last = [], -10 ** 9
    for t in np.nonzero(edge)[0].tolist():
        if t - last >= horizon:
            out.append(t)
            last = t
    return np.asarray(out, np.int64)


def summarize(net: np.ndarray, hit: np.ndarray, w: np.ndarray, base_p: float, p: Params) -> dict[str, float]:
    """Recency-weighted, shrunk Confidence / RR / Edge for one set of firings."""
    win = net > 0
    sw = float(w.sum())
    conf = (float((w * win).sum()) + p.prior * base_p) / (sw + p.prior) if sw + p.prior else base_p
    ww, wl = w[win], w[~win]
    avg_win = float((ww * net[win]).sum() / ww.sum()) if ww.sum() else p.target
    avg_loss = float((wl * -net[~win]).sum() / wl.sum()) if wl.sum() else p.stop
    rr = avg_win / max(avg_loss, 1e-6)
    edge = conf * rr - (1 - conf)
    exp_net = float((w * net).sum() / (sw + p.prior)) if sw else 0.0
    hit_rate = float((w * hit).sum() / sw) if sw else float("nan")
    return {"confidence": conf, "rr": rr, "edge": edge, "avg_win": avg_win, "avg_loss": avg_loss,
            "exp_net": exp_net, "target_hit_rate": hit_rate, "weight": sw}


def walk_forward(t: np.ndarray, net: np.ndarray, hit: np.ndarray, base_p: np.ndarray, p: Params,
                 last: int) -> dict[str, Any]:
    """Re-score every firing from earlier, already-closed firings only; record the 'taken' ones."""
    taken_t, taken_net = [], []
    for i in range(len(t)):
        prev = np.nonzero(t[:i] + p.horizon + 1 <= t[i])[0]
        if prev.size < 3:
            continue
        s = summarize(net[prev], hit[prev], weight(t[i] - t[prev], p), float(base_p[i]), p)
        if s["edge"] > 0:
            taken_t.append(int(t[i]))
            taken_net.append(float(net[i]))
    if not taken_t:
        return {"taken": 0, "taken_mean_net": None, "taken_win_rate": None, "taken_w_mean_net": None}
    tn, tt = np.asarray(taken_net), np.asarray(taken_t)
    w = weight(last - tt, p)
    return {"taken": len(tn), "taken_mean_net": float(tn.mean()), "taken_win_rate": float((tn > 0).mean()),
            "taken_w_mean_net": float((w * tn).sum() / w.sum()), "taken_t": tt.tolist(), "taken_net": tn.tolist()}


def screen_triples(S: np.ndarray, fams: Sequence[str], valid: np.ndarray, net: np.ndarray, w: np.ndarray,
                   base_p: float, p: Params, device: str | None = None) -> list[tuple[tuple[int, int, int], float]]:
    """GPU screen of every 3-condition AND across 3 different families (rising edges, no spacing rule).

    Returns the ``confirm`` best (indices, approximate Edge) with >= ``min_firings`` firings.
    """
    import torch

    dev = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    s = S.shape[1]
    combos = [c for c in itertools.combinations(range(s), 3) if len({fams[c[0]], fams[c[1]], fams[c[2]]}) == 3]
    if not combos:
        return []
    Sm = torch.as_tensor(S & valid[:, None], device=dev)
    netv = np.nan_to_num(net)
    win = netv > 0
    V = torch.as_tensor(np.stack([np.ones_like(w), w, w * win, w * netv * win, w * -netv * ~win,
                                  w * ~win], 1), dtype=torch.float32, device=dev)
    idx = torch.as_tensor(np.asarray(combos), device=dev)
    out = torch.empty((len(combos), 6), device=dev)
    chunk = max(64, int(2e8 // max(1, S.shape[0])))
    for a in range(0, len(combos), chunk):
        ix = idx[a:a + chunk]
        M = Sm[:, ix[:, 0]] & Sm[:, ix[:, 1]] & Sm[:, ix[:, 2]]
        E = M.clone()
        E[1:] &= ~M[:-1]
        out[a:a + chunk] = E.T.to(torch.float32) @ V
    o = out.cpu().numpy().astype(float)
    cnt, sw, swin, spos, sneg, slw = o.T
    conf = (swin + p.prior * base_p) / (sw + p.prior)
    with np.errstate(invalid="ignore", divide="ignore"):
        avg_win = np.where(swin > 0, spos / np.maximum(swin, 1e-12), p.target)
        avg_loss = np.where(slw > 0, sneg / np.maximum(slw, 1e-12), p.stop)
    edge = conf * (avg_win / np.maximum(avg_loss, 1e-6)) - (1 - conf)
    edge[cnt < p.min_firings] = -np.inf
    best = np.argsort(-edge)[:p.confirm]
    return [(combos[i], float(edge[i])) for i in best if np.isfinite(edge[i])]


# --------------------------------------------------------------------------- one stock


def run_stock(data: Any, tf: str, costs: Mapping[str, Any], p: Params, cache_dir: Path | None,
              device: str | None = None) -> dict[str, Any]:
    from . import strategy_dsl as dsl
    from .lanes import all_bars, build_lane
    from .research import _ctx_for

    t0 = time.monotonic()
    bars = all_bars(data)
    if tf not in bars or not len(bars[tf]):
        return {"symbol": data.symbol, "timeframe": tf, "status": "NO_DATA"}
    lane = build_lane(data, tf, costs, bars=bars, cache_dir=cache_dir)
    m = lane.market
    ctx = _ctx_for(bars[tf], tf, lane, bars, {}, None)
    n = len(m)
    if n < 60:
        return {"symbol": data.symbol, "timeframe": tf, "status": "TOO_SHORT", "bars": n}
    cost = float(lane.costs.buy + lane.costs.sell)
    path_end = m.exit_last if m.intraday else None  # intraday: squared off at 15:15 IST
    hit, net = outcomes(m.open, m.high, m.low, m.close, p, cost, path_end)
    # secondary label (owner 2026-11-20): +2 x ATR(14) before -1 x ATR(14) within the same horizon
    hit_atr, _ = outcomes(m.open, m.high, m.low, m.close, p, cost, path_end,
                          target=ATR_TARGET * m.natr, stop=ATR_STOP * m.natr)
    valid = ~np.isnan(net) & ~m.no_signal & (m.entry_ok if m.intraday else True)
    if p.liquidity:  # owner 2026-11-20: tradable stocks only (proxy for the missing market-cap filter)
        try:
            valid &= np.asarray(dsl.signal(liquidity_gate(tf), ctx), dtype=bool)
        except Exception:  # noqa: BLE001 - no gate possible on this lane: keep the stock ungated
            pass
    va = valid & ~np.isnan(hit_atr)
    wa = weight(n - 1 - np.nonzero(va)[0], p)
    base_atr = float((wa * hit_atr[va]).sum() / wa.sum()) if wa.sum() else 0.0
    last = n - 1
    vi = np.nonzero(valid)[0]
    wv = weight(last - vi, p)
    base_now = float((wv * (net[vi] > 0)).sum() / wv.sum()) if vi.size else 0.0
    # causal base rate at candle t: only outcomes already closed (t' + horizon + 1 <= t)
    closed_w = np.cumsum(np.where(valid, 1.0, 0.0))
    closed_win = np.cumsum(np.where(valid, (np.nan_to_num(net) > 0).astype(float), 0.0))
    lag = p.horizon + 1
    cw = np.concatenate((np.zeros(lag), closed_w[:-lag])) if n > lag else np.zeros(n)
    cwin = np.concatenate((np.zeros(lag), closed_win[:-lag])) if n > lag else np.zeros(n)
    base_at = np.where(cw > 0, cwin / np.maximum(cw, 1), base_now)

    masks: dict[str, np.ndarray] = {}
    fam: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, (f, expr) in SINGLES.items():
        try:
            masks[name] = dsl.signal(expr, ctx)
            fam[name] = f
        except Exception as exc:  # noqa: BLE001 - one unsupported condition never stops the stock
            errors[name] = f"{type(exc).__name__}: {exc}"
    names = list(masks)
    cands: list[tuple[str, str, np.ndarray]] = [(k, "single", masks[k]) for k in names]
    if p.pairs:
        for a, b in itertools.combinations(names, 2):
            if fam[a] != fam[b]:
                cands.append((f"{a} + {b}", "pair", masks[a] & masks[b]))
    screened = 0
    if p.triples and len(names) >= 3:
        try:
            S = np.stack([masks[k] for k in names], 1)
            top = screen_triples(S, [fam[k] for k in names], valid, net, weight(last - np.arange(n), p),
                                 base_now, p, device)
            screened = len(top)
            for (i, j, k), _e in top:
                cands.append((f"{names[i]} + {names[j]} + {names[k]}", "triple",
                              masks[names[i]] & masks[names[j]] & masks[names[k]]))
        except Exception as exc:  # noqa: BLE001 - GPU trouble never stops the stock
            errors["triples"] = f"{type(exc).__name__}: {exc}"
    rows: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for name, kind, mask in cands:  # singles first, so a hybrid firing on the same days as a simpler one is dropped
        fire_now = bool(mask[last]) and not bool(mask[last - 1])
        t = firings(mask, valid, p.horizon)
        if t.size < p.min_firings or t.tobytes() in seen:
            continue
        seen.add(t.tobytes())
        s = summarize(net[t], hit[t], weight(last - t, p), base_now, p)
        wf = walk_forward(t, net[t], hit[t], base_at[t], p, last)
        ta = t[~np.isnan(hit_atr[t])]
        wt = weight(last - ta, p)
        atr_conf = float(((wt * hit_atr[ta]).sum() + p.prior * base_atr) / (wt.sum() + p.prior))
        rows.append({"trigger": name, "kind": kind, "firings": int(t.size),
                     "firings_last100": int((last - t < 100).sum()),
                     "last_fired": str(m.sessions[t[-1]]), "fires_now": fire_now,
                     **{k: round(v, 5) for k, v in s.items()}, "base_rate": round(base_now, 4),
                     "lift": round(s["confidence"] / base_now, 3) if base_now else None,
                     "atr_confidence": round(atr_conf, 4), "atr_base_rate": round(base_atr, 4),
                     "atr_lift": round(atr_conf / base_atr, 3) if base_atr else None,
                     **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in wf.items()
                        if k not in ("taken_t", "taken_net")}})
    rows.sort(key=lambda r: -r["edge"])
    return {"symbol": data.symbol, "timeframe": tf, "status": "OK", "contract": CONTRACT, "params": asdict(p),
            "bars": n, "data_start": str(m.sessions[0]), "data_end": str(m.sessions[-1]),
            "data_fingerprint": lane.data_fingerprint, "cost_round_trip": cost, "base_rate": base_now,
            "candles_with_upmove": int(np.nansum(hit)), "triples_confirmed": screened, "errors": errors,
            "triggers": rows, "seconds": round(time.monotonic() - t0, 2)}


def _job(args: tuple) -> dict[str, Any]:
    data, tf, costs, p, cache_dir, device, key, path = args
    try:
        r = run_stock(data, tf, costs, p, cache_dir, device)
    except Exception as exc:  # noqa: BLE001 - one stock never stops the run
        r = {"symbol": data.symbol, "timeframe": tf, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
    r["key"] = key
    Path(path).write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    return {k: r.get(k) for k in ("symbol", "status", "seconds", "error")} | {"n": len(r.get("triggers") or [])}


# --------------------------------------------------------------------------- reports


def qualified(r: Mapping[str, Any]) -> bool:
    """Ranked trigger that also paid off when chosen live-like (walk-forward, >= 5 taken firings)."""
    return (r["edge"] > 0 and (r.get("taken") or 0) >= 5 and (r.get("taken_w_mean_net") or 0) > 0
            and (r.get("lift") or 0) > 1)


def report(out: Path, results: Sequence[Mapping[str, Any]], p: Params, tf: str) -> dict[str, Any]:
    trig_rows, today = [], []
    ok = [r for r in results if r.get("status") == "OK"]
    lines = [f"# Up-move learner ({tf})", "",
             f"Up-move = +{p.target:.2%} within {p.horizon} candles before -{p.stop:.2%} (costs included). "
             f"All history, weight (1 + age/{p.decay_bars:g})^-{p.decay_power:g}.",
             "Confidence = chance the trade ends in profit. RR = average win / average loss. "
             "Edge = Confidence x RR - (1 - Confidence): expected gain per 1 unit of risk.",
             "QUALIFIED = Edge > 0, beats the stock's base rate, and the live-like check (each firing scored "
             "only from earlier firings) made money over >= 5 taken firings.",
             f"Stocks: {len(results)} ({len(ok)} OK).", ""]
    per_trigger: dict[str, list[int]] = {}
    for r in ok:
        for x in r["triggers"]:
            row = {"symbol": r["symbol"], "timeframe": r["timeframe"], "qualified": qualified(x), **x}
            trig_rows.append(row)
            st = per_trigger.setdefault(x["trigger"], [0, 0])
            st[0] += 1
            st[1] += int(row["qualified"])
            if x["fires_now"]:
                today.append(row)
    # triggers that qualify on many stocks are the believable ones
    common = sorted(((k, v[1], v[0]) for k, v in per_trigger.items() if v[0] >= 5), key=lambda z: -z[1] / z[2])
    lines += ["## Triggers that qualify on the most stocks", "", "| Trigger | Qualified on | Ranked on | Share |",
              "|---|---|---|---|"]
    for k, q, nn in common[:25]:
        lines.append(f"| {k} | {q} | {nn} | {q / nn:.0%} |")
    lines += ["", "## Best qualified trigger per stock (top 60 by Edge)", "",
              "| Stock | Trigger | Firings (last 100) | Confidence | RR | Edge | Live-like: taken, win rate, w-avg |",
              "|---|---|---|---|---|---|---|"]
    best = []
    for r in ok:
        q = [x for x in r["triggers"] if qualified(x)]
        if q:
            best.append((r["symbol"], q[0]))
    for sym, x in sorted(best, key=lambda z: -z[1]["edge"])[:60]:
        lines.append(f"| {sym} | {x['trigger']} | {x['firings']} ({x['firings_last100']}) | {x['confidence']:.0%} | "
                     f"{x['rr']:.2f} | {x['edge']:+.2f} | {x['taken']}, {x['taken_win_rate']:.0%}, "
                     f"{x['taken_w_mean_net']:+.2%} |")

    def write(name: str, rows: list[dict[str, Any]]) -> None:
        cols: list[str] = []
        for x in rows[:2000]:
            cols += [k for k in x if k not in cols]
        with (out / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols or ["symbol"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    write("triggers.csv", trig_rows)
    today.sort(key=lambda x: (not x["qualified"], -x["edge"]))
    write("today.csv", today)
    lines += ["", "## Qualified triggers firing on the latest candle", ""]
    for x in [t for t in today if t["qualified"]][:60]:
        lines.append(f"- {x['symbol']} ({x['last_fired']}): {x['trigger']} - Confidence {x['confidence']:.0%}, "
                     f"RR {x['rr']:.2f}, Edge {x['edge']:+.2f}")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"stocks": len(results), "ok": len(ok), "triggers": len(trig_rows),
            "qualified": sum(r["qualified"] for r in trig_rows), "qualified_firing_now":
            sum(1 for t in today if t["qualified"])}


# --------------------------------------------------------------------------- CLI


def read_stocks(spec: str) -> list[str]:
    src = spec[1:] if spec.startswith("@") else None
    names = [x.strip().split(",")[0].upper() for x in
             (Path(src).read_text(encoding="utf-8-sig").splitlines() if src else spec.split(","))
             if x.strip() and not x.startswith("#")]
    return [x if x.endswith("-EQ") else x + "-EQ" for x in names]


def main(argv: Sequence[str] | None = None) -> int:
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from .context import App
    from .db import sha
    from .ingest import ingest_many

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--home")
    ap.add_argument("--stocks", required=True, help="comma list or @file (one symbol per line)")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--name", required=True)
    ap.add_argument("--target", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--horizon", type=int)
    ap.add_argument("--decay-bars", type=float, default=Params.decay_bars)
    ap.add_argument("--decay-power", type=float, default=Params.decay_power)
    ap.add_argument("--no-pairs", action="store_true")
    ap.add_argument("--no-triples", action="store_true")
    ap.add_argument("--workers", type=int, default=6, help="parallel stock processes (each may use the GPU)")
    ap.add_argument("--device", help="cuda | cpu (default: cuda when available)")
    a = ap.parse_args(argv)
    tgt, stp, hz = TF_DEFAULTS.get(a.tf, TF_DEFAULTS["1d"])
    p = Params(a.target or tgt, a.stop or stp, a.horizon or hz, a.decay_bars, a.decay_power,
               pairs=not a.no_pairs, triples=not a.no_triples)
    syms = read_stocks(a.stocks)[: a.max_symbols or None]
    app = App(a.config, a.home, quiet=True)
    try:
        out = app.s.reports_dir / "upmove" / a.name
        (out / "stocks").mkdir(parents=True, exist_ok=True)
        costs = app.s.section("costs")
        cache = app.s.cache_dir / "features"
        pending, paths = {}, []
        ex = ProcessPoolExecutor(max_workers=a.workers) if a.workers > 1 else None
        done = reused = 0
        t0 = time.monotonic()
        for i, sym in enumerate(syms):
            f = out / "stocks" / f"{sym}-{a.tf}.json"
            paths.append(f)
            try:
                data = next(iter(ingest_many(app.s, app.db, [sym], use_minute=a.tf.endswith("m")).values()), None)
            except Exception as exc:  # noqa: BLE001
                f.write_text(json.dumps({"symbol": sym, "timeframe": a.tf, "status": "NO_DATA",
                                         "error": str(exc)}), encoding="utf-8")
                continue
            if data is None:
                f.write_text(json.dumps({"symbol": sym, "timeframe": a.tf, "status": "NO_DATA"}), encoding="utf-8")
                continue
            key = sha({"c": CONTRACT, "fp": getattr(data, "fingerprint", None), "tf": a.tf, "p": asdict(p),
                       "s": SINGLES, "code": app.code_sha, "cost": costs})
            if f.is_file():
                try:
                    if json.loads(f.read_text(encoding="utf-8")).get("key") == key:
                        reused += 1
                        continue
                except (OSError, ValueError):
                    pass
            job = (data, a.tf, costs, p, cache, a.device, key, str(f))
            if ex is None:
                _job(job)
                done += 1
            else:
                pending[ex.submit(_job, job)] = sym
                while len(pending) >= a.workers * 2:
                    fut = next(as_completed(pending))
                    pending.pop(fut)
                    done += 1
            if (i + 1) % 25 == 0:
                print(f"[{i + 1}/{len(syms)}] computed {done}, reused {reused}, "
                      f"{time.monotonic() - t0:.0f}s", flush=True)
        if ex is not None:
            for fut in as_completed(list(pending)):
                done += 1
            ex.shutdown(wait=True)
        results = [json.loads(f.read_text(encoding="utf-8")) for f in paths if f.is_file()]
        print(json.dumps(report(out, results, p, a.tf), indent=1))
        print(f"summary: {out / 'summary.md'}  ({time.monotonic() - t0:.0f}s, computed {done}, reused {reused})")
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
