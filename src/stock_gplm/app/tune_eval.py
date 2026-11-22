"""Fast pooled evaluation of strategy variants for LLM fine-tuning (owner 2026-11-22).

A pool of persistent worker processes; each owns a shard of stocks, loads every stock ONCE (bars, lane, DSL
context) and then evaluates any number of variant batches against it. A variant = entry expression + full
exit dict + direction + timeframe. Trades are simulated by the same engine as walk_forward
(``strategy_engine.simulate``, costs from the lane, 15:15 square-off / 14:45 last entry intraday) over the
whole window from ``start``; the orchestrator splits them by signal date into TUNE and CONFIRM.

Workers return compact numpy arrays per variant (stock index, signal day, net return, mfe, mae, bars held,
exit reason, optional entry features) so pooled statistics over hundreds of stocks stay cheap.

Exit note: tuning uses the strategy's own exit (plus variant overrides), not the per-stock learned
``exits.json`` - one uniform exit per variant so pooled numbers compare like with like.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import traceback
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

# Entry-time features for loser analysis: name -> DSL expression (evaluated at the signal bar).
FEATURES: dict[str, str] = {
    "dist_ema200": "close / ema(close, 200) - 1",
    "dist_ema50": "close / ema(close, 50) - 1",
    "dist_ema20": "close / ema(close, 20) - 1",
    "rsi14": "rsi(close, 14)",
    "rsi2": "rsi(close, 2)",
    "atr_pct": "atr(14) / close",
    "adx14": "adx(14)",
    "vol_ratio20": "volume / median(volume, 20)",
    "gap": "open / close[1] - 1",
    "ret1": "close / close[1] - 1",
    "ret5": "close / close[5] - 1",
    "ret20": "close / close[20] - 1",
    "range_pos20": "(close - lowest(low, 20)) / (highest(high, 20) - lowest(low, 20))",
    "body_pct": "(close - open) / open",
    "upper_wick_pct": "upper_wick / close",
    "bb_width20": "bb_width(close, 20, 2)",
    "day_of_week": "day_of_week",
}
INTRADAY_FEATURES: dict[str, str] = {
    "minute_of_session": "minute_of_session",
    "day_trend_ema20": 'htf(close / ema(close, 20) - 1, "1d")',
    "day_rsi14": 'htf(rsi(close, 14), "1d")',
    "session_move": "close / session_open - 1",
}
EXIT_CODES = {"STOP": 0, "STOP_GAP": 0, "TRAIL": 0, "TARGET": 1, "TARGET_GAP": 1, "TIME": 2, "SESSION_CLOSE": 3,
              "SIGNAL_EXIT": 4, "OPEN_END": 5}
EXIT_NAMES = {0: "stop", 1: "target", 2: "time", 3: "15:15 square-off", 4: "exit rule", 5: "still open", 9: "other"}


def feature_names(intraday: bool) -> list[str]:
    return list(FEATURES) + (list(INTRADAY_FEATURES) if intraday else [])


@dataclass
class Variant:
    vid: str
    tf: str
    entry: str
    exit: dict[str, Any]  # full normalised exit dict (max_hold_bars, swing_stop, when, ...)
    direction: str = "long"
    features: bool = False

    def payload(self) -> dict[str, Any]:
        return {"vid": self.vid, "tf": self.tf, "entry": self.entry, "exit": self.exit,
                "direction": self.direction, "features": self.features}


# ------------------------------------------------------------------ worker side
_APP: dict[str, Any] = {}


def _load(stock: str, config: str, tfs: Sequence[str], use_minute: bool) -> dict[str, Any]:
    from .context import App
    from .ingest import ingest_many
    from .lanes import build_lane, research_bars
    from .research import _ctx_for

    if config not in _APP:  # one App (settings + audit DB) per worker process, not per stock
        _APP[config] = App(config, None, quiet=True)
    app = _APP[config]
    data = next(iter(ingest_many(app.s, app.db, [stock], use_minute=use_minute).values()))
    bars = research_bars(data)
    costs = app.s.section("costs")
    out: dict[str, Any] = {}
    for tf in tfs:
        if tf not in bars or not len(bars[tf]):
            continue
        lane = build_lane(data, tf, costs, bars=bars)
        ctx = _ctx_for(bars[tf], tf, lane, bars, {}, None)
        m = lane.market
        days = np.asarray(m.sessions).astype("datetime64[D]").astype(np.int64).astype(np.int32)
        out[tf] = {"m": m, "ctx": ctx, "buy": lane.costs.buy, "sell": lane.costs.sell,
                   "ses": np.asarray(m.sessions).astype(str), "days": days}
    return out


def _evaluate_stock(si: int, lanes: Mapping[str, Any], variants: Sequence[Mapping[str, Any]], start: str,
                    acc: dict[str, list], errors: dict[str, int]) -> None:
    from . import strategy_dsl as dsl
    from .strategy_engine import Exit, simulate

    by_tf: dict[str, list] = {}
    for v in variants:
        by_tf.setdefault(v["tf"], []).append(v)
    for tf, vs in by_tf.items():
        L = lanes.get(tf)
        if L is None:
            continue
        m, ctx = L["m"], L["ctx"]
        ctx.cache.clear()  # variant sub-expressions differ per batch; keep memory flat
        n = len(m)
        a0 = int(np.searchsorted(L["ses"], start))
        if a0 >= n - 1:
            continue
        masks: dict[str, np.ndarray | None] = {}
        feats: dict[str, np.ndarray] | None = None
        for v in vs:
            try:
                if v["entry"] not in masks:
                    masks[v["entry"]] = dsl.signal(v["entry"], ctx)
                mask = masks[v["entry"]]
                when = (v["exit"] or {}).get("when")
                xm = dsl.signal(when, ctx) if when else None
                trades = simulate(m, mask, (a0, n), Exit.of(v["exit"]), L["buy"], L["sell"],
                                  v["direction"] == "short", xm)
            except (dsl.MissingData, dsl.DSLError, ValueError, KeyError, ZeroDivisionError):
                errors[v["vid"]] = errors.get(v["vid"], 0) + 1
                continue
            if not trades:
                continue
            t = np.fromiter((x["t"] for x in trades), np.int64, len(trades))
            rec = {"stock": np.full(len(t), si, np.int32), "day": L["days"][t],
                   "net": np.fromiter((x["net_return"] for x in trades), np.float32, len(t)),
                   "mfe": np.fromiter((x["mfe"] for x in trades), np.float32, len(t)),
                   "mae": np.fromiter((x["mae"] for x in trades), np.float32, len(t)),
                   "held": np.fromiter((x["bars_held"] for x in trades), np.int32, len(t)),
                   "reason": np.fromiter((EXIT_CODES.get(str(x["exit_reason"]).upper(), 9) for x in trades),
                                         np.int8, len(t))}
            if v.get("features"):
                if feats is None:
                    feats = {}
                    exprs = {**FEATURES, **(INTRADAY_FEATURES if m.intraday else {})}
                    for name, ex in exprs.items():
                        try:
                            feats[name] = np.asarray(dsl.evaluate(dsl.parse(ex), ctx), dtype=float)
                        except Exception:  # noqa: BLE001 - a missing feature is just absent
                            feats[name] = np.full(n, np.nan)
                names = feature_names(m.intraday)
                rec["feat"] = np.stack([feats[k][t] for k in names], axis=1).astype(np.float32)
            acc.setdefault(v["vid"], []).append(rec)


def _worker(conn, shard: Sequence[str], offset: int, config: str, tfs: Sequence[str], use_minute: bool) -> None:
    """Persistent worker: lazily load each stock once, then answer evaluation requests until 'stop'."""
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    cache: dict[str, Any] = {}
    failed: dict[str, str] = {}
    while True:
        msg = conn.recv()
        if msg.get("cmd") == "stop":
            break
        try:
            acc: dict[str, list] = {}
            errors: dict[str, int] = {}
            for k, stock in enumerate(shard):
                if stock in failed:
                    continue
                if stock not in cache:
                    try:
                        cache[stock] = _load(stock, config, tfs, use_minute)
                    except Exception as exc:  # noqa: BLE001 - one bad stock must not stop the pool
                        failed[stock] = f"{type(exc).__name__}: {exc}"[:200]
                        continue
                _evaluate_stock(offset + k, cache[stock], msg["variants"], msg["start"], acc, errors)
            out = {vid: {key: np.concatenate([r[key] for r in recs]) for key in recs[0]} for vid, recs in acc.items()}
            conn.send({"ok": True, "result": out, "errors": errors, "failed": failed, "loaded": len(cache)})
        except Exception:  # noqa: BLE001
            conn.send({"ok": False, "error": traceback.format_exc()[-2000:]})


class EvalPool:
    """Shards ``stocks`` over ``workers`` persistent processes. ``evaluate`` returns pooled arrays per vid."""

    def __init__(self, stocks: Sequence[str], config: str, tfs: Sequence[str], workers: int, use_minute: bool):
        self.stocks = list(stocks)
        ctx = mp.get_context("spawn")
        workers = max(1, min(workers, len(self.stocks)))
        self.conns, self.procs = [], []
        size = -(-len(self.stocks) // workers)
        for w in range(workers):
            shard = self.stocks[w * size:(w + 1) * size]
            if not shard:
                continue
            a, b = ctx.Pipe()
            p = ctx.Process(target=_worker, args=(b, shard, w * size, config, list(tfs), use_minute), daemon=True)
            p.start()
            self.conns.append(a)
            self.procs.append(p)
        self.failed: dict[str, str] = {}

    def evaluate(self, variants: Sequence[Variant], start: str) -> tuple[dict[str, dict[str, np.ndarray]], dict]:
        req = {"cmd": "eval", "variants": [v.payload() for v in variants], "start": start}
        for c in self.conns:
            c.send(req)
        parts: dict[str, list] = {}
        errors: dict[str, int] = {}
        for c in self.conns:
            r = c.recv()
            if not r["ok"]:
                raise RuntimeError("tuning worker failed:\n" + r["error"])
            self.failed.update(r["failed"])
            for vid, arrs in r["result"].items():
                parts.setdefault(vid, []).append(arrs)
            for vid, k in r["errors"].items():
                errors[vid] = errors.get(vid, 0) + k
        out = {vid: {key: np.concatenate([p[key] for p in ps]) for key in ps[0]} for vid, ps in parts.items()}
        return out, errors

    def close(self) -> None:
        for c in self.conns:
            try:
                c.send({"cmd": "stop"})
            except (BrokenPipeError, OSError):
                pass
        for p in self.procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()


# ------------------------------------------------------------------ statistics
def weights(days: np.ndarray, end_day: int, decay: float) -> np.ndarray:
    """Recency weight (1 + age/decay)^-2, age in calendar-ordered distinct trading days before ``end_day``.

    Uses distinct signal days present in the pooled trades (same idea as walk_forward._weights_by_day)."""
    if not len(days):
        return np.zeros(0)
    uniq = np.unique(np.concatenate([days, [end_day]]))
    rank = np.searchsorted(uniq, days)
    age = (len(uniq) - 1) - rank
    return (1.0 + age / decay) ** -2


def stats(net: np.ndarray, w: np.ndarray, period: np.ndarray | None = None) -> dict[str, float]:
    """Pooled stats: plain and recency-weighted win / avg / PF, effective trades, positive periods."""
    n = len(net)
    if n == 0 or w.sum() <= 0:
        return {"trades": 0, "win": 0.0, "avg": 0.0, "pf": 0.0, "win_w": 0.0, "avg_w": 0.0, "pf_w": 0.0,
                "n_eff": 0.0, "periods": 0, "periods_pos": 0}
    net = net.astype(float)
    win = net > 0
    gl = -net[~win].sum()
    gw_w, gl_w = float((w * net)[win].sum()), float(-(w * net)[~win].sum())
    out = {"trades": int(n), "win": float(win.mean()), "avg": float(net.mean()),
           "pf": float(net[win].sum() / gl) if gl > 0 else 99.0,
           "win_w": float(w[win].sum() / w.sum()), "avg_w": float((w * net).sum() / w.sum()),
           "pf_w": gw_w / gl_w if gl_w > 0 else 99.0, "n_eff": float(w.sum() ** 2 / (w ** 2).sum())}
    if period is not None:
        pos = [net[period == p].sum() > 0 for p in np.unique(period)]
        out["periods"], out["periods_pos"] = len(pos), int(sum(pos))
    return out


def period_of(days: np.ndarray, step: str) -> np.ndarray:
    d = days.astype("datetime64[D]")
    return d.astype("datetime64[Y]").astype(int) if step == "year" else d.astype("datetime64[M]").astype(int)
