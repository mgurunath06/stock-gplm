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
    # behaviour / events (owner 2026-11-22: "bounces from the 50 EMA", "jumps when MACD crosses"): 1 = yes, 0 = no
    "ema20_touch": "low <= ema(close, 20) and close > ema(close, 20)",
    "ema50_touch": "low <= ema(close, 50) and close > ema(close, 50)",
    "ema200_touch": "low <= ema(close, 200) and close > ema(close, 200)",
    "macd_cross_up3": "any_of(cross_above(macd(close), macd_signal(close)), 3)",
    "macd_cross_dn3": "any_of(cross_below(macd(close), macd_signal(close)), 3)",
    "rsi_cross_up30": "any_of(cross_above(rsi(close, 14), 30), 3)",
    "rsi_cross_dn70": "any_of(cross_below(rsi(close, 14), 70), 3)",
    "breakout20": "close > highest(high, 20)[1]",
    "breakdown20": "close < lowest(low, 20)[1]",
    "ema_stack_up": "ema(close, 20) > ema(close, 50) and ema(close, 50) > ema(close, 200)",
    "golden_cross10": "any_of(cross_above(ema(close, 50), ema(close, 200)), 10)",
    "above_supertrend": "close > supertrend(10, 3)",
    "inside_bar": "high < high[1] and low > low[1]",
    "nr7": "(high - low) <= lowest(high - low, 7)",
    "bars_since_high20": "barssince(high >= highest(high, 20))",
}
INTRADAY_FEATURES: dict[str, str] = {
    "minute_of_session": "minute_of_session",
    "day_trend_ema20": 'htf(close / ema(close, 20) - 1, "1d")',
    "day_rsi14": 'htf(rsi(close, 14), "1d")',
    "session_move": "close / session_open - 1",
}


DAILY_FEATURES: dict[str, str] = {}  # 1d and above only (e.g. RSI(300)); from the config file


def _load_features() -> tuple[dict[str, str], dict[str, str], dict[str, str]] | None:
    """config/research/move_features.json (owner 2026-11-22: configurable feature list); built-ins above if absent."""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "config" / "research" / "move_features.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    off = set(d.get("disabled") or [])

    def flat(sec: Mapping[str, Any] | None) -> dict[str, str]:
        return {k: v for grp in (sec or {}).values() for k, v in grp.items() if k not in off}
    return flat(d.get("common")), flat(d.get("intraday")), flat(d.get("daily"))


_CFG_FEATURES = _load_features()
if _CFG_FEATURES and _CFG_FEATURES[0]:
    FEATURES, INTRADAY_FEATURES, DAILY_FEATURES = _CFG_FEATURES


def feature_exprs(intraday: bool) -> dict[str, str]:
    """All feature expressions for a timeframe: common + intraday-only or daily-and-above-only."""
    return {**FEATURES, **(INTRADAY_FEATURES if intraday else DAILY_FEATURES)}
EXIT_CODES = {"STOP": 0, "STOP_GAP": 0, "TRAIL": 0, "TARGET": 1, "TARGET_GAP": 1, "TIME": 2, "SESSION_CLOSE": 3,
              "SIGNAL_EXIT": 4, "OPEN_END": 5}
EXIT_NAMES = {0: "stop", 1: "target", 2: "time", 3: "15:15 square-off", 4: "exit rule", 5: "still open", 9: "other"}


def feature_names(intraday: bool) -> list[str]:
    return list(feature_exprs(intraday))


@dataclass
class Variant:
    vid: str
    tf: str
    entry: str
    exit: dict[str, Any]  # full normalised exit dict (max_hold_bars, swing_stop, when, ...)
    direction: str = "long"
    features: bool = False
    label: dict[str, Any] | None = None  # {"h", "t", "s"}: label EVERY entry bar (move-first discovery), no trades

    def payload(self) -> dict[str, Any]:
        return {"vid": self.vid, "tf": self.tf, "entry": self.entry, "exit": self.exit,
                "direction": self.direction, "features": self.features, "label": self.label}


# ------------------------------------------------------------------ worker side
_APP: dict[str, Any] = {}
_REFS: dict[str, Any] = {}
REF_SYMBOLS = ("IDX_NIFTY_50-EQ",)  # official Nifty 50 (data.index_root, NSE MCP history from 2024) for bench()


def _refs(app: Any) -> dict[str, Any]:
    """Official index series for bench(); loaded once per worker. Missing index data -> bench() has no data."""
    if not _REFS:
        from .ingest import ingest_many
        for sym in REF_SYMBOLS:
            try:
                d = next(iter(ingest_many(app.s, app.db, [sym], use_minute=False).values()), None)
                if d is not None:
                    _REFS[sym] = d
            except Exception:  # noqa: BLE001 - strategies using bench() simply get no signals
                pass
        _REFS.setdefault("_loaded", True)
    return {k: v for k, v in _REFS.items() if k != "_loaded"}


def _load(stock: str, config: str, tfs: Sequence[str], use_minute: bool) -> dict[str, Any]:
    from .context import App
    from .ingest import ingest_many
    from .lanes import build_lane, research_bars
    from .research import _ctx_for

    if config not in _APP:  # one App (settings + audit DB) per worker process, not per stock
        _APP[config] = App(config, None, quiet=True)
    app = _APP[config]
    refs = _refs(app)
    data = next(iter(ingest_many(app.s, app.db, [stock], use_minute=use_minute).values()))
    bars = research_bars(data)
    costs = app.s.section("costs")
    out: dict[str, Any] = {}
    for tf in tfs:
        if tf not in bars or not len(bars[tf]):
            continue
        lane = build_lane(data, tf, costs, bars=bars)
        ctx = _ctx_for(bars[tf], tf, lane, bars, refs, None)
        m = lane.market
        days = np.asarray(m.sessions).astype("datetime64[D]").astype(np.int64).astype(np.int32)
        out[tf] = {"m": m, "ctx": ctx, "buy": lane.costs.buy, "sell": lane.costs.sell,
                   "ses": np.asarray(m.sessions).astype(str), "days": days}
    return out


def _label_bars(m: Any, mask: np.ndarray, a0: int, n: int, lab: Mapping[str, Any], buy: float, sell: float,
                short: bool) -> dict[str, np.ndarray] | None:
    """Label every entry bar (owner 2026-11-22, move-first): enter at the next open, hit = price reaches
    +t x ATR(14) before -s x ATR(14) within h bars (intraday: up to the 15:15 square-off bar). Same bar -> stop first
    (conservative); gaps are not modelled. net = target / stop / horizon-close result after the lane costs."""
    h, T, S = int(lab["h"]), float(lab["t"]), float(lab["s"])
    t = np.flatnonzero(np.asarray(mask, dtype=bool))
    t = t[(t >= a0) & (t + 1 < n)]
    natr = np.asarray(m.natr, dtype=float)
    t = t[np.isfinite(natr[t])]
    if m.intraday:
        end = np.minimum(t + h, np.asarray(m.exit_last)[t + 1].astype(np.int64))
        keep = end >= t + 1
    else:
        end = t + h
        keep = end <= n - 1
    t, end = t[keep], end[keep]
    if not len(t):
        return None
    o, hi, lo, cl = (np.asarray(x, dtype=float) for x in (m.open, m.high, m.low, m.close))
    ent = o[t + 1]
    atr = natr[t] * ent
    sg = -1.0 if short else 1.0
    tgt, stp = ent + sg * T * atr, ent - sg * S * atr
    never = h + 10
    ft = np.full(len(t), never)
    fs = np.full(len(t), never)
    for k in range(1, h + 1):
        j = t + k
        live = j <= end
        if not live.any():
            break
        jj = np.minimum(j, n - 1)
        th = live & ((lo[jj] <= tgt) if short else (hi[jj] >= tgt))
        sh = live & ((hi[jj] >= stp) if short else (lo[jj] <= stp))
        ft = np.where(th & (ft == never), k, ft)
        fs = np.where(sh & (fs == never), k, fs)
    hit = ft < fs
    px = np.where(hit, tgt, np.where(fs < never, stp, cl[end]))
    net = (ent * (1 - sell) - px * (1 + buy)) / ent if short else px * (1 - sell) / (ent * (1 + buy)) - 1
    zeros = np.zeros(len(t), np.float32)
    return {"t": t, "net": net.astype(np.float32), "mfe": zeros, "mae": zeros,
            "held": np.minimum(np.minimum(ft, fs), end - t).astype(np.int32),
            "reason": np.where(hit, 1, np.where(fs < never, 0, 2)).astype(np.int8)}


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
                lab = None
                trades: list = []
                if v.get("label"):
                    lab = _label_bars(m, mask, a0, n, v["label"], L["buy"], L["sell"], v["direction"] == "short")
                else:
                    when = (v["exit"] or {}).get("when")
                    xm = dsl.signal(when, ctx) if when else None
                    trades = simulate(m, mask, (a0, n), Exit.of(v["exit"]), L["buy"], L["sell"],
                                      v["direction"] == "short", xm)
            except (dsl.MissingData, dsl.DSLError, ValueError, KeyError, ZeroDivisionError):
                errors[v["vid"]] = errors.get(v["vid"], 0) + 1
                continue
            if v.get("label"):
                if lab is None:
                    continue
                t = lab.pop("t")
                rec = {"stock": np.full(len(t), si, np.int32), "day": L["days"][t], **lab}
            else:
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
                    exprs = feature_exprs(m.intraday)
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


def win_low(win_w: float, n_eff: float, q: float = 0.2) -> float:
    """Cautious win rate (owner 2026-11-22): Beta lower quantile of the recency-weighted win rate on n_eff trades.
    70% on 8 effective trades -> ~55%; 70% on 60 -> ~65%. Few trades can count, at an honest level."""
    from scipy.stats import beta
    n = max(float(n_eff), 0.0)
    return float(beta.ppf(q, win_w * n + 1.0, (1.0 - win_w) * n + 1.0))


def month_consistency(net: np.ndarray, days: np.ndarray) -> tuple[int, int]:
    """(months with trades, months with positive total net)."""
    if not len(net):
        return 0, 0
    m = period_of(days, "month")
    pos = [net[m == k].sum() > 0 for k in np.unique(m)]
    return len(pos), int(sum(pos))


def period_of(days: np.ndarray, step: str) -> np.ndarray:
    d = days.astype("datetime64[D]")
    return d.astype("datetime64[Y]").astype(int) if step == "year" else d.astype("datetime64[M]").astype(int)
