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

CONTRACT = "stock-gplm.upmove.v1"

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


# --------------------------------------------------------------------------- pure core


def weight(age: np.ndarray, p: Params) -> np.ndarray:
    return (1.0 + np.maximum(age, 0) / max(1e-9, p.decay_bars)) ** -p.decay_power


def outcomes(o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: Params, cost: float,
             path_end: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Per candle t: (hit, net). hit = 1 target first, 0 stop first or time-out; NaN where no full path."""
    n = len(c)
    hit = np.full(n, np.nan)
    net = np.full(n, np.nan)
    for t in range(n - 1):
        end = min(t + p.horizon, n - 1) if path_end is None else min(t + p.horizon, int(path_end[t]))
        if end < t + 1 or (path_end is None and t + p.horizon > n - 1):
            continue
        ent = o[t + 1]
        if not ent > 0:
            continue
        up, dn = ent * (1 + p.target), ent * (1 - p.stop)
        res = None
        for k in range(t + 1, end + 1):
            if lo[k] <= dn:
                res = (0.0, -p.stop)
                break
            if h[k] >= up:
                res = (1.0, p.target)
                break
        if res is None:
            res = (0.0, c[end] / ent - 1)
        hit[t], net[t] = res[0], res[1] - cost
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


# --------------------------------------------------------------------------- one stock


def run_stock(data: Any, tf: str, costs: Mapping[str, Any], p: Params, cache_dir: Path | None) -> dict[str, Any]:
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
    cost = float(lane.costs.buy + lane.costs.sell)
    path_end = m.session_last if m.intraday else None
    hit, net = outcomes(m.open, m.high, m.low, m.close, p, cost, path_end)
    valid = ~np.isnan(net) & ~m.no_signal
    last = n - 1
    # base rate of a profitable trade on any candle (recency-weighted, as of each candle -> prefix form)
    vi = np.nonzero(valid)[0]
    wv = weight(last - vi, p)
    base_now = float((wv * (net[vi] > 0)).sum() / wv.sum()) if vi.size else 0.0
    cum_w = np.cumsum(np.where(valid, 1.0, 0.0))
    cum_win = np.cumsum(np.where(valid, (np.nan_to_num(net) > 0).astype(float), 0.0))
    base_at = np.where(cum_w > 0, cum_win / np.maximum(cum_w, 1), base_now)  # plain base rate to date (causal)

    masks: dict[str, np.ndarray] = {}
    fam: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, (f, expr) in SINGLES.items():
        try:
            masks[name] = dsl.signal(expr, ctx)
            fam[name] = f
        except Exception as exc:  # noqa: BLE001 - one unsupported condition never stops the stock
            errors[name] = f"{type(exc).__name__}: {exc}"
    cands: list[tuple[str, str, np.ndarray]] = [(k, "single", v) for k, v in masks.items()]
    if p.pairs:
        for a, b in itertools.combinations(masks, 2):
            if fam[a] != fam[b]:
                cands.append((f"{a} + {b}", "pair", masks[a] & masks[b]))
    live = np.zeros(n, bool)
    rows: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for name, kind, mask in cands:  # singles first, so a pair firing on the same days as a single is dropped
        fire_now = bool(mask[last]) and not bool(mask[last - 1]) if n > 1 else False
        t = firings(mask, valid, p.horizon)
        if t.size < p.min_firings or t.tobytes() in seen:
            continue
        seen.add(t.tobytes())
        s = summarize(net[t], hit[t], weight(last - t, p), base_now, p)
        wf = walk_forward(t, net[t], hit[t], base_at[t], p, last)
        rows.append({"trigger": name, "kind": kind, "firings": int(t.size),
                     "firings_last100": int((last - t < 100).sum()),
                     "last_fired": str(m.sessions[t[-1]]), "fires_now": fire_now,
                     **{k: round(v, 5) for k, v in s.items()}, "base_rate": round(base_now, 4),
                     "lift": round(s["confidence"] / base_now, 3) if base_now else None,
                     **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in wf.items()
                        if k not in ("taken_t", "taken_net")}})
        if fire_now:
            live[last] = True
    rows.sort(key=lambda r: -r["edge"])
    return {"symbol": data.symbol, "timeframe": tf, "status": "OK", "contract": CONTRACT, "params": asdict(p),
            "bars": n, "data_start": str(m.sessions[0]), "data_end": str(m.sessions[-1]),
            "data_fingerprint": lane.data_fingerprint, "cost_round_trip": cost, "base_rate": base_now,
            "candles_with_upmove": int(np.nansum(hit)), "errors": errors, "triggers": rows,
            "seconds": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------- reports


def qualified(r: Mapping[str, Any]) -> bool:
    """Ranked trigger that also paid off when chosen live-like (walk-forward, >= 5 taken firings)."""
    return (r["edge"] > 0 and (r.get("taken") or 0) >= 5 and (r.get("taken_w_mean_net") or 0) > 0
            and (r.get("lift") or 0) > 1)


def report(out: Path, results: Sequence[Mapping[str, Any]], p: Params) -> dict[str, Any]:
    trig_rows, today = [], []
    lines = ["# Up-move learner", "",
             f"Up-move = +{p.target:.1%} within {p.horizon} candles before -{p.stop:.1%} (costs included). "
             f"All history, weight (1 + age/{p.decay_bars:g})^-{p.decay_power:g}.",
             "Confidence = chance the trade ends in profit. RR = average win / average loss. "
             "Edge = Confidence x RR - (1 - Confidence): expected gain per 1 unit of risk.",
             "QUALIFIED = Edge > 0, beats the stock's base rate, and the live-like check (each firing scored "
             "only from earlier firings) made money over >= 5 taken firings.", ""]
    for r in results:
        if r.get("status") != "OK":
            lines.append(f"## {r['symbol']} {r['timeframe']}: {r.get('status')}")
            continue
        q = [x for x in r["triggers"] if qualified(x)]
        lines += [f"## {r['symbol']} {r['timeframe']} ({r['data_start']} .. {r['data_end']}, {r['bars']} candles)",
                  f"Base rate (any candle ends in profit): {r['base_rate']:.1%}. Candles that started an up-move: "
                  f"{r['candles_with_upmove']}. Triggers ranked: {len(r['triggers'])}, qualified: {len(q)}.", "",
                  "| # | Trigger | Firings (last 100) | Confidence | RR | Edge | Avg win / loss | Live-like: taken, "
                  "win rate, weighted avg |", "|---|---|---|---|---|---|---|---|"]
        for i, x in enumerate(q[:10]):
            lines.append(f"| {i + 1} | {x['trigger']} | {x['firings']} ({x['firings_last100']}) | "
                         f"{x['confidence']:.0%} | {x['rr']:.2f} | {x['edge']:+.2f} | "
                         f"{x['avg_win']:+.2%} / -{x['avg_loss']:.2%} | {x['taken']}, {x['taken_win_rate']:.0%}, "
                         f"{x['taken_w_mean_net']:+.2%} |")
        lines.append("")
        for x in r["triggers"]:
            row = {"symbol": r["symbol"], "timeframe": r["timeframe"], "qualified": qualified(x), **x}
            trig_rows.append(row)
            if x["fires_now"]:
                today.append(row)

    def write(name: str, rows: list[dict[str, Any]]) -> None:
        cols: list[str] = []
        for x in rows:
            cols += [k for k in x if k not in cols]
        with (out / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols or ["symbol"])
            w.writeheader()
            w.writerows(rows)

    write("triggers.csv", trig_rows)
    today.sort(key=lambda x: (not x["qualified"], -x["edge"]))
    write("today.csv", today)
    lines += ["## Firing on the latest candle", ""]
    for x in today[:30]:
        lines.append(f"- {x['symbol']} {x['timeframe']}: {x['trigger']} - Confidence {x['confidence']:.0%}, "
                     f"RR {x['rr']:.2f}, Edge {x['edge']:+.2f}{' (QUALIFIED)' if x['qualified'] else ''}")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"stocks": len(results), "triggers": len(trig_rows), "qualified": sum(r["qualified"] for r in trig_rows),
            "firing_now": len(today)}


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    from .context import App
    from .db import sha
    from .ingest import ingest_many

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--home")
    ap.add_argument("--stocks", required=True, help="comma list or @file (one symbol per line)")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--name", required=True)
    ap.add_argument("--target", type=float, default=Params.target)
    ap.add_argument("--stop", type=float, default=Params.stop)
    ap.add_argument("--horizon", type=int, default=Params.horizon)
    ap.add_argument("--decay-bars", type=float, default=Params.decay_bars)
    ap.add_argument("--decay-power", type=float, default=Params.decay_power)
    ap.add_argument("--no-pairs", action="store_true")
    a = ap.parse_args(argv)
    p = Params(a.target, a.stop, a.horizon, a.decay_bars, a.decay_power, pairs=not a.no_pairs)
    src = a.stocks[1:] if a.stocks.startswith("@") else None
    names = [x.strip().split(",")[0].upper() for x in
             (Path(src).read_text(encoding="utf-8-sig").splitlines() if src else a.stocks.split(","))
             if x.strip() and not x.startswith("#")]
    syms = [x if x.endswith("-EQ") else x + "-EQ" for x in names]
    app = App(a.config, a.home, quiet=True)
    try:
        out = app.s.reports_dir / "upmove" / a.name
        (out / "stocks").mkdir(parents=True, exist_ok=True)
        results = []
        for i, sym in enumerate(syms):
            data = next(iter(ingest_many(app.s, app.db, [sym], use_minute=a.tf.endswith("m")).values()), None)
            if data is None:
                results.append({"symbol": sym, "timeframe": a.tf, "status": "NO_DATA"})
                continue
            key = sha({"c": CONTRACT, "fp": getattr(data, "fingerprint", None), "tf": a.tf, "p": asdict(p),
                       "s": SINGLES, "code": app.code_sha, "cost": app.s.section("costs")})
            f = out / "stocks" / f"{sym}-{a.tf}.json"
            if f.is_file():
                old = json.loads(f.read_text(encoding="utf-8"))
                if old.get("key") == key:
                    results.append(old)
                    print(f"[{i + 1}/{len(syms)}] {sym}: reused", flush=True)
                    continue
            r = run_stock(data, a.tf, app.s.section("costs"), p, app.s.cache_dir / "features")
            r["key"] = key
            f.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
            results.append(r)
            print(f"[{i + 1}/{len(syms)}] {sym}: {len(r.get('triggers', []))} triggers in "
                  f"{r.get('seconds')}s", flush=True)
        print(json.dumps(report(out, results, p), indent=1))
        print(f"summary: {out / 'summary.md'}")
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
