"""Learn the owner's judgement with ML (owner 2026-11-22): which EMA crosses to take and when to square off.

    $env:PYTHONPATH="src"; python -m stock_gplm.app.owner_ml --symbol RELIANCE        (--tf 15m)
    ... --symbol RELIANCE --pool 200   (WIN model trained on the first 200 universe stocks; --workers 20; resumable,
                                        --fresh redoes all) -> <reports>/tuning/owner-ml-pool-<tf>/POOL_REPORT.md

Uses config/research/owner_strategy.json (signals, base exit version "ml.base_version") and the owner's marks
(owner_trades.csv + the chart exports owner_trades-<SYM>-<tf>.csv).

1. EVENTS: every EMA13 x EMA20 / EMA200 cross (long + short) with what the chart looked like at that candle: distance
   from EMA 13/20/200 (ATR and %), EMA slopes, candle body / wicks / range, volume, time of day, previous day high / low,
   day open, 60m-like and daily trend, RSI, how far price already ran, candles since the last cross.
2. ENTRY MODELS (gradient boosting, recent trades weigh more):
   - YOU: would the owner take this cross? positives = your takes (entry within -2..+6 candles of the cross),
     negatives = your skips / 'far' examples, plus crosses you did not mark in months you marked (weaker weight - you
     said you did not mark everything).
   - WIN: does the trade win with the base exit (after costs)?
   Walk-forward by month, causal: each month is predicted by models trained only on earlier months (WIN: only trades
   that had closed). No split date.
3. EXIT MODEL: on every candle of your trades - hold or square off here? Walk-forward trade by trade; compared with
   the base exit on the same trades.
4. EXPLAIN: which inputs drive each model, your take-rate by distance from EMA 13 ('what is far for you').
5. TODAY: the latest crosses with P(you'd take) and P(win).
Output <reports>/tuning/owner-strategy-<tf>-<SYM>/ML_REPORT.md, ml_events.csv, ml_exit.csv. Every stock is judged on
its own (run per symbol).
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from .owner_strategy import REPO, SPEC, OwnerStrategy, all_owner_rows
from .tune import CONFIG


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if k < len(x):
        out[k:] = x[:-k]
    return out


def _rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up, dn = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    au = np.zeros(len(c))
    ad = np.zeros(len(c))
    a = 1.0 / n
    for i in range(1, len(c)):
        au[i] = au[i - 1] + a * (up[i] - au[i - 1])
        ad[i] = ad[i - 1] + a * (dn[i] - ad[i - 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 - 100 / (1 + au / ad)


class Chart:
    """Per-candle inputs, long-oriented ('higher = better for a long'); flip() turns them for shorts."""

    DIRECTIONAL = ["dist13_atr", "dist20_atr", "dist200_atr", "dist13_pct", "dist200_pct", "gap13_20_atr", "slope13_3",
                   "slope13_10", "slope20_10", "slope200_10", "htf_slope52", "htf_slope80", "body_atr", "vs_prev_close_atr",
                   "vs_pdh_atr", "vs_pdl_atr", "vs_day_open_atr", "ret10_atr", "run_from_low20_atr", "daily_trend_pct",
                   "rsi14_m50"]
    NEUTRAL = ["range_atr", "upper_wick", "lower_wick", "vol_ratio", "minutes", "natr_pct"]

    def __init__(self, o: OwnerStrategy):
        from .strategy_engine import _ema_of
        m = o.m
        self.n = n = len(m)
        c, op, h, lo, v = (np.asarray(x, dtype=float) for x in (m.close, m.open, m.high, m.low, m.volume))
        atr = np.asarray(m.natr, dtype=float) * c
        atr[~(atr > 0)] = np.nan
        e = {k: np.asarray(_ema_of(m, k), dtype=float) for k in (13, 20, 52, 80, 200)}
        sid = o.sid
        starts = np.flatnonzero(np.r_[True, sid[1:] != sid[:-1]])
        seg = np.searchsorted(starts, np.arange(n), side="right") - 1  # session number of each candle
        s_hi = np.maximum.reduceat(h, starts)
        s_lo = np.minimum.reduceat(lo, starts)
        s_close = c[np.r_[starts[1:] - 1, n - 1]]
        pdh = np.r_[np.nan, s_hi[:-1]][seg]
        pdl = np.r_[np.nan, s_lo[:-1]][seg]
        day_open = op[starts][seg]
        dema = np.full(len(starts), np.nan)  # daily EMA 20 of session closes, previous session's value
        a, acc = 2 / 21, np.nan
        for k in range(len(starts)):
            dema[k] = acc
            acc = s_close[k] if np.isnan(acc) else acc + a * (s_close[k] - acc)
        dtrend = dema[seg]
        rng = h - lo
        with np.errstate(divide="ignore", invalid="ignore"):
            body_top, body_bot = np.maximum(op, c), np.minimum(op, c)
            vol20 = np.convolve(v, np.ones(20) / 20, mode="full")[:n]
            low20 = np.array([lo[max(0, i - 19):i + 1].min() for i in range(n)])
            mins = np.array([(int(x[11:13]) * 60 + int(x[14:16]) - 555) if len(x) > 10 else 0 for x in o.times], float)
            self.cols = {
                "dist13_atr": (c - e[13]) / atr, "dist20_atr": (c - e[20]) / atr, "dist200_atr": (c - e[200]) / atr,
                "dist13_pct": 100 * (c - e[13]) / e[13], "dist200_pct": 100 * (c - e[200]) / e[200],
                "gap13_20_atr": (e[13] - e[20]) / atr,
                "slope13_3": (e[13] - _shift(e[13], 3)) / atr, "slope13_10": (e[13] - _shift(e[13], 10)) / atr,
                "slope20_10": (e[20] - _shift(e[20], 10)) / atr, "slope200_10": (e[200] - _shift(e[200], 10)) / atr,
                "htf_slope52": (e[52] - _shift(e[52], 4)) / atr, "htf_slope80": (e[80] - _shift(e[80], 4)) / atr,
                "body_atr": (c - op) / atr, "vs_prev_close_atr": (c - _shift(c, 1)) / atr,
                "vs_pdh_atr": (c - pdh) / atr, "vs_pdl_atr": (c - pdl) / atr, "vs_day_open_atr": (c - day_open) / atr,
                "ret10_atr": (c - _shift(c, 10)) / atr, "run_from_low20_atr": (c - low20) / atr,
                "daily_trend_pct": 100 * (c - dtrend) / dtrend, "rsi14_m50": _rsi(c) - 50,
                "range_atr": rng / atr, "upper_wick": (h - body_top) / np.where(rng > 0, rng, np.nan),
                "lower_wick": (body_bot - lo) / np.where(rng > 0, rng, np.nan), "vol_ratio": v / vol20,
                "minutes": mins, "natr_pct": 100 * np.asarray(m.natr, dtype=float)}
        self.high20 = np.array([h[max(0, i - 19):i + 1].max() for i in range(n)])
        self.c, self.atr = c, atr

    def row(self, t: int, short: bool) -> list[float]:
        out = [(-1.0 if short else 1.0) * float(self.cols[k][t]) for k in self.DIRECTIONAL]
        if short:  # run from the 20-candle high instead of the low; wicks swap roles
            out[self.DIRECTIONAL.index("run_from_low20_atr")] = float((self.high20[t] - self.c[t]) / self.atr[t])
        neu = {k: float(self.cols[k][t]) for k in self.NEUTRAL}
        if short:
            neu["upper_wick"], neu["lower_wick"] = neu["lower_wick"], neu["upper_wick"]
        return out + [neu[k] for k in self.NEUTRAL]

    @property
    def names(self) -> list[str]:
        return self.DIRECTIONAL + self.NEUTRAL


def _reg():
    """Expected profit per trade (owner 2026-11-22: 'win' picked small wins with full-stop losses)."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=300, min_samples_leaf=40,
                                         l2_regularization=1.0, random_state=0)


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=8,
                                          l2_regularization=1.0, random_state=0)


def _auc(y: np.ndarray, p: np.ndarray) -> float | None:
    if len(set(y.tolist())) < 2:
        return None
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p))


class OwnerML:
    def __init__(self, a: argparse.Namespace):
        a.no_check = True
        self.a = a
        self.o = OwnerStrategy(a.symbol, a)
        self.log = self.o.log

    def recency(self, sess: np.ndarray, now_sess: int) -> np.ndarray:
        rc = self.o.spec.get("recency") or {}
        return (1.0 + np.maximum(now_sess - sess, 0) / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))

    def base_variant(self) -> dict[str, Any] | None:
        want = str((self.o.spec.get("ml") or {}).get("base_version", "V2"))
        for v in self.o.spec["check"]["variants"]:
            if v["name"].split(" ")[0] == want:
                return v
        return None

    # ------------------------------------------------------------------ events
    def events(self) -> list[dict[str, Any]]:
        o, ch = self.o, self.chart
        bv = self.base_variant()
        rows = all_owner_rows(REPO / o.spec["check"]["owner_trades"], o.sym, o.tf)
        takes = {s: [o.find(r["signal"]) for r in rows if (r["side"] or "long") == s and r["decision"] == "take"]
                 for s in o.sig}
        skips = {s: [o.find(r["signal"]) for r in rows if (r["side"] or "long") == s and r["decision"] == "skip"]
                 for s in o.sig}
        rule = o.entry_rule(None)
        far = [o.find(x) for x in rule.get("far_examples") or []]
        marked_months = {o.tv(i)[:7] for v in takes.values() for i in v if i is not None}
        ev = []
        for side, arr in o.sig.items():
            S = side == "short"
            tk = np.array([i for i in takes[side] if i is not None], dtype=np.int64)
            sk = [i for i in skips[side] if i is not None] + ([i for i in far if i is not None] if not S else [])
            prev = -10 ** 9
            for t in arr.tolist():
                tr = o.sim(side, np.array([t]), bv)
                x = tr[0] if tr else None
                feats = ch.row(t, S)
                you, w_you = None, 0.0
                if len(tk) and ((tk >= t - 2) & (tk <= t + 6)).any():
                    you, w_you = 1, 1.0
                elif any(abs(i - t) <= 2 for i in sk):
                    you, w_you = 0, 1.0
                elif o.tv(t)[:7] in marked_months:
                    you, w_you = 0, 0.3  # not marked in a month you marked: probably skipped (you did not mark all)
                ev.append({"t": t, "side": side, "time": o.tv(t), "month": o.tv(t)[:7], "kind": o.kind[side].get(t, ""),
                           "x": feats + [1.0 if S else 0.0, 1.0 if "200" in o.kind[side].get(t, "") else 0.0,
                                         float(min(t - prev, 500))],
                           "sess": int(o.sid[t]), "you": you, "w_you": w_you,
                           "win": None if x is None or x["exit_reason"] == "OPEN_END" else int(x["net_return"] > 0),
                           "net": None if x is None else float(x["net_return"]),
                           "closed_at": None if x is None else int(x["exit_index"])})
                prev = t
        ev.sort(key=lambda e: e["t"])
        self.names = ch.names + ["is_short", "is_ema200_cross", "candles_since_prev_cross"]
        return ev

    # ------------------------------------------------------------------ walk-forward
    def walk(self, ev: list[dict[str, Any]], target: str) -> None:
        """Month by month: fit on earlier months only (WIN: only trades closed before the month), predict the month."""
        months = sorted({e["month"] for e in ev})
        first_t = {mo: min(e["t"] for e in ev if e["month"] == mo) for mo in months}
        for k, mo in enumerate(months):
            cut = first_t[mo]
            if target == "win":
                tr = [e for e in ev if e["t"] < cut and e["win"] is not None and e["closed_at"] < cut]
                y = np.array([e["win"] for e in tr])
                w = np.ones(len(tr))
            else:
                tr = [e for e in ev if e["t"] < cut and e["you"] is not None]
                y = np.array([e["you"] for e in tr])
                w = np.array([e["w_you"] for e in tr])
            test = [e for e in ev if e["month"] == mo]
            if len(tr) < 30 or len(set(y.tolist())) < 2:
                continue
            w = w * self.recency(np.array([e["sess"] for e in tr]), test[0]["sess"])
            mdl = _model().fit(np.array([e["x"] for e in tr]), y, sample_weight=w)
            p = mdl.predict_proba(np.array([e["x"] for e in test]))[:, 1]
            for e, pi in zip(test, p):
                e[f"p_{target}"] = float(pi)
            self.log(f"walk-forward {target}: {mo} predicted from {len(tr)} earlier events")

    def final(self, ev: list[dict[str, Any]], target: str) -> tuple[Any, list[tuple[str, float]]] | None:
        from sklearn.inspection import permutation_importance
        if target == "win":
            tr = [e for e in ev if e["win"] is not None]
            y = np.array([e["win"] for e in tr])
            w = np.ones(len(tr))
        else:
            tr = [e for e in ev if e["you"] is not None]
            y = np.array([e["you"] for e in tr])
            w = np.array([e["w_you"] for e in tr])
        if len(tr) < 30 or len(set(y.tolist())) < 2:
            return None
        X = np.array([e["x"] for e in tr])
        w = w * self.recency(np.array([e["sess"] for e in tr]), int(self.o.sid[-1]))
        mdl = _model().fit(X, y, sample_weight=w)
        pi = permutation_importance(mdl, X, y, n_repeats=10, random_state=0, sample_weight=w)
        imp = sorted(zip(self.names, pi.importances_mean.tolist()), key=lambda kv: -kv[1])
        return mdl, imp

    # ------------------------------------------------------------------ exit model
    def exit_rows(self) -> list[dict[str, Any]]:
        o, ch = self.o, self.chart
        from .strategy_engine import _ema_of
        e20 = _ema_of(o.m, 20)
        rows = all_owner_rows(REPO / o.spec["check"]["owner_trades"], o.sym, o.tf)
        out = []
        for n_tr, r in enumerate(sorted((r for r in rows if r["decision"] == "take" and r["exit"]),
                                        key=lambda r: r["signal"])):
            i, je, S = o.find(r["signal"]), o.find(r["exit"]), (r["side"] or "long") == "short"
            if i is None or je is None or je <= i + 1:
                continue
            ent = float(o.m.open[i + 1])
            for k in range(i + 1, je + 1):
                sg = -1.0 if S else 1.0
                f = ch.row(k, S) + [sg * (ch.c[k] - ent) / ch.atr[k], float(k - i),
                                    float((o.m.high[k] >= e20[k]) if S else (o.m.low[k] <= e20[k]))]
                out.append({"trade": n_tr, "entry": r["signal"], "side": "short" if S else "long", "k": k,
                            "time": o.tv(k), "x": f, "y": int(k == je), "sess": int(o.sid[k]), "your_exit": je, "i": i})
        return out

    def exit_walk(self, xr: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trade by trade: fit on your earlier trades, exit this one at the first candle with P(square off) >= 0.5."""
        o = self.o
        bv = self.base_variant()
        trades = sorted({x["trade"] for x in xr})
        res = []
        for tr_id in trades:
            past = [x for x in xr if x["trade"] < tr_id]
            cur = [x for x in xr if x["trade"] == tr_id]
            if len({x["trade"] for x in past}) < 10:
                continue
            y = np.array([x["y"] for x in past])
            w = self.recency(np.array([x["sess"] for x in past]), cur[0]["sess"])
            w = w * np.where(y == 1, max(1.0, (y == 0).sum() / max(1, y.sum())), 1.0)  # exits are rare: balance them
            mdl = _model().fit(np.array([x["x"] for x in past]), y, sample_weight=w)
            p = mdl.predict_proba(np.array([x["x"] for x in cur]))[:, 1]
            hit = next((x["k"] for x, pi in zip(cur, p) if pi >= 0.5), None)
            je, i, side = cur[0]["your_exit"], cur[0]["i"], cur[0]["side"]
            base = o.sim(side, np.array([i]), bv)
            be = int(base[0]["exit_index"]) if base else None
            res.append({"entry": cur[0]["entry"], "side": side, "your_exit": o.tv(je),
                        "ml_exit": o.tv(hit) if hit is not None else "held past yours",
                        "ml_diff": (hit - je) if hit is not None else "", "base_exit": o.tv(be) if be is not None else "",
                        "base_diff": (be - je) if be is not None else ""})
        return res

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        t0 = time.monotonic()
        o = self.o
        if not o.load():
            return 1
        o.calibrate()
        self.log("building chart inputs for every candle")
        self.chart = Chart(o)
        ev = self.events()
        nl = sum(e["you"] is not None for e in ev)
        self.log(f"{len(ev)} crosses; {sum(e['you'] == 1 for e in ev)} you took, {nl} with a your-label, "
                 f"{sum(e['win'] is not None for e in ev)} with a closed result")
        self.walk(ev, "you")
        self.walk(ev, "win")
        bv = self.base_variant()
        root = o.du.root
        md = [f"# ML judgement - {o.sym} {o.tf}", "",
              f"{len(ev)} EMA crosses {ev[0]['time'] if ev else ''} .. {ev[-1]['time'] if ev else ''}. Base exit for WIN: "
              f"**{bv['name'] if bv else 'default'}**. Walk-forward by month (each month predicted only from earlier "
              "months), recent trades weigh more.", ""]
        # --- YOU model
        oos = [e for e in ev if "p_you" in e and e["you"] is not None and e["w_you"] == 1.0]
        oos_all = [e for e in ev if "p_you" in e and e["you"] is not None]
        md += ["## 1. Would you take this cross? (out of sample)", ""]
        if oos_all:
            y = np.array([e["you"] for e in oos_all])
            p = np.array([e["p_you"] for e in oos_all])
            pos = [e for e in oos_all if e["you"] == 1]
            got = sum(e["p_you"] >= 0.5 for e in pos)
            said = [e for e in oos_all if e["p_you"] >= 0.5]
            auc = _auc(y, p)
            md += [f"- Your takes it saw in advance: **{got} of {len(pos)}** called 'take' (P >= 0.5).",
                   f"- Of the crosses it called 'take': {sum(e['you'] == 1 for e in said)} of {len(said)} were yours.",
                   f"- Ranking quality (AUC, 0.5 = coin, 1 = perfect): {auc:.2f}" if auc is not None else "- AUC: n/a",
                   f"- Hard labels only (your takes / skips / far examples): {len(oos)} crosses."]
            self.log(f"YOU model out of sample: {got}/{len(pos)} of your takes called 'take'; "
                     + (f"AUC {auc:.2f}" if auc is not None else "AUC n/a"))
        else:
            md.append("Not enough of your marks yet before the later months - mark more crosses (takes AND skips).")
        # --- WIN model + replay
        md += ["", "## 2. Does it win? (out of sample, base exit, after costs)", ""]
        ow = [e for e in ev if "p_win" in e and e["win"] is not None]
        if ow:
            y = np.array([e["win"] for e in ow])
            p = np.array([e["p_win"] for e in ow])
            auc = _auc(y, p)
            md.append(f"AUC {auc:.2f} on {len(ow)} crosses." if auc is not None else f"{len(ow)} crosses.")
            md += ["", "| Taken when | Trades | Won | Avg | Total |", "|---|---|---|---|---|"]
            for nm, sel in (("every cross (base)", lambda e: True), ("P(win) >= 0.5", lambda e: e["p_win"] >= 0.5),
                            ("P(you) >= 0.5", lambda e: e.get("p_you", 0) >= 0.5),
                            ("both >= 0.5", lambda e: e["p_win"] >= 0.5 and e.get("p_you", 0) >= 0.5)):
                parts = []
                for side in o.sig:
                    fired = np.array([e["t"] for e in ow if e["side"] == side and sel(e)], dtype=np.int64)
                    parts += o.sim(side, fired, bv) if len(fired) else []
                done = [x for x in parts if x["exit_reason"] != "OPEN_END"]
                if done:
                    net = np.array([x["net_return"] for x in done])
                    md.append(f"| {nm} | {len(done)} | {int((net > 0).sum())} ({(net > 0).mean():.0%}) | "
                              f"{100 * net.mean():+.2f}% | {100 * net.sum():+.1f}% |")
                    self.log(f"replay {nm}: {len(done)} trades, {(net > 0).mean():.0%} won, total {100 * net.sum():+.1f}%")
                else:
                    md.append(f"| {nm} | 0 | | | |")
            md += ["", "Replay = only the months it predicted, one position per side at a time."]
        # --- explanations
        md += ["", "## 3. What drives the judgement (final models, all data)", ""]
        for target, title in (("you", "Would you take it"), ("win", "Does it win")):
            fm = self.final(ev, target)
            if fm is None:
                md.append(f"- {title}: not enough data.")
                continue
            _, imp = fm
            md.append(f"- **{title}** - top inputs: " + ", ".join(f"{k} ({v:+.3f})" for k, v in imp[:8]))
        lab = [e for e in ev if e["you"] is not None and e["w_you"] == 1.0] + \
              [e for e in ev if e["you"] == 0 and e["w_you"] < 1.0]
        if lab:
            md += ["", "### Your take-rate by distance from EMA 13 at the cross (what 'far' is for you)", "",
                   "| Distance (ATR, in the trade's direction) | Crosses | You took | Rate | Won (base exit) |",
                   "|---|---|---|---|---|"]
            d13 = self.names.index("dist13_atr")
            for lo_, hi_ in ((-9, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 99)):
                sel = [e for e in lab if lo_ <= e["x"][d13] < hi_]
                if not sel:
                    continue
                k = sum(e["you"] == 1 for e in sel)
                wins = [e["win"] for e in sel if e["win"] is not None]
                md.append(f"| {'< 0.5' if lo_ < 0 else f'{lo_:g} - {hi_:g}' if hi_ < 99 else f'> {lo_:g}'} | {len(sel)} | "
                          f"{k} | {k / len(sel):.0%} | {f'{np.mean(wins):.0%}' if wins else '-'} |")
        # --- exit model
        self.log("exit model: walk-forward over your trades")
        xr = self.exit_rows()
        er = self.exit_walk(xr) if xr else []
        md += ["", "## 4. When would you square off? (exit model, walk-forward over your trades)", ""]
        if er:
            def score(key: str) -> str:
                d = [abs(r[key]) for r in er if r[key] != ""]
                return (f"same candle {sum(x == 0 for x in d)}, within 2: {sum(x <= 2 for x in d)}, within 4: "
                        f"{sum(x <= 4 for x in d)} of {len(er)}") if d else "-"
            md += [f"- **ML exit**: {score('ml_diff')}", f"- Base exit ({bv['name'] if bv else 'default'}): "
                   f"{score('base_diff')}", "", "| Your entry | Side | Your exit | ML exit (diff) | Base exit (diff) |",
                   "|---|---|---|---|---|"]
            md += [f"| {r['entry']} | {r['side']} | {r['your_exit']} | {r['ml_exit']} ({r['ml_diff']}) | {r['base_exit']} "
                   f"({r['base_diff']}) |" for r in reversed(er)]
            self.log(f"exit model: ML {score('ml_diff')} | base {score('base_diff')}")
        else:
            md.append("Not enough of your trades yet (needs 10 earlier trades before the first prediction).")
        # --- today
        fy, fw = self.final(ev, "you"), self.final(ev, "win")
        md += ["", "## 5. Latest crosses (final models)", "", "| Cross | Side | Trigger | P(you'd take) | P(win) |",
               "|---|---|---|---|---|"]
        for e in ev[-8:][::-1]:
            py = fy[0].predict_proba(np.array([e["x"]]))[0, 1] if fy else float("nan")
            pw = fw[0].predict_proba(np.array([e["x"]]))[0, 1] if fw else float("nan")
            md.append(f"| {e['time']} | {e['side']} | {e['kind']} | {py:.0%} | {pw:.0%} |")
        # --- files
        with (root / "ml_events.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "side", "kind", "you", "you_weight", "win", "net_pct", "p_you", "p_win"] + self.names)
            for e in ev:
                w.writerow([e["time"], e["side"], e["kind"], e["you"], e["w_you"], e["win"],
                            "" if e["net"] is None else round(100 * e["net"], 3), round(e.get("p_you", np.nan), 3),
                            round(e.get("p_win", np.nan), 3)] + [round(v, 4) for v in e["x"]])
        with (root / "ml_exit.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(er[0]) if er else ["entry"])
            w.writeheader()
            w.writerows(er)
        md += ["", "Mark more crosses on the chart - especially ones you would SKIP - and rerun: the YOU model learns "
               "most from skips."]
        (root / "ML_REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"done ({time.monotonic() - t0:.0f}s): {root / 'ML_REPORT.md'}")
        return 0


# ====================================================================== pooled WIN model over many stocks
def extract(sym: str, tf: str | None, spec: str, config: str, out: str) -> str:
    """One stock (worker process): every cross with its chart inputs and its base-exit result -> <out>/<SYM>.npz."""
    a = argparse.Namespace(symbol=sym, tf=tf, spec=spec, config=config, no_check=True)
    o = OwnerStrategy(sym, a)
    try:
        if not o.load():
            np.savez_compressed(out, empty=np.array(1))
            return f"{sym}: no data"
        o.calibrate()
        ch = Chart(o)
        ml = OwnerML.__new__(OwnerML)
        ml.o = o
        bv = ml.base_variant()
        X, side, ts, closed, net, win, sess, t_idx, c_idx, kind = [], [], [], [], [], [], [], [], [], []
        for sd, arr in o.sig.items():
            S = sd == "short"
            prev = -10 ** 9
            for t in arr.tolist():
                tr = o.sim(sd, np.array([t]), bv)
                x = tr[0] if tr else None
                X.append(ch.row(t, S) + [1.0 if S else 0.0, 1.0 if "200" in o.kind[sd].get(t, "") else 0.0,
                                         float(min(t - prev, 500))])
                prev = t
                side.append(1 if S else 0)
                ts.append(int(o.end_ns[t]))
                t_idx.append(t)
                sess.append(int(o.sid[t]))
                kind.append(o.kind[sd].get(t, ""))
                if x is None:
                    closed.append(-1), net.append(np.nan), win.append(-1), c_idx.append(-1)
                else:
                    e = int(x["exit_index"])
                    done = x["exit_reason"] != "OPEN_END"
                    closed.append(int(o.end_ns[e]) if done else -1)
                    net.append(float(x["net_return"]))
                    win.append(int(x["net_return"] > 0) if done else -1)
                    c_idx.append(e)
        np.savez_compressed(out, X=np.array(X, np.float32), side=np.array(side, np.int8), ts=np.array(ts, np.int64),
                            closed=np.array(closed, np.int64), net=np.array(net, np.float32),
                            win=np.array(win, np.int8), t=np.array(t_idx, np.int64), c=np.array(c_idx, np.int64),
                            kind=np.array(kind), times=np.array([o.tv(i) for i in t_idx]))
        return f"{sym}: {len(X)} crosses"
    finally:
        o.du.close()


class Pool:
    """WIN model trained on all stocks' crosses, walk-forward by month; each stock judged on its own trades."""

    def __init__(self, a: argparse.Namespace):
        from .discover import Discoverer
        from .walk_forward import read_stocks
        import json as _json
        self.a = a
        cfg = _json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.tf = a.tf or _json.loads(Path(a.spec).read_text(encoding="utf-8")).get("timeframe", "15m")
        uni = read_stocks(a.pool if a.pool.startswith("@") else "@" + str(REPO / "deploy" / "oracle" / "live" /
                                                                          "universe.txt"))
        self.stocks = uni if a.pool.startswith("@") else uni[:int(a.pool)]
        if a.symbol.upper() not in self.stocks:
            self.stocks.insert(0, a.symbol.upper())
        tmp = Path(__import__("tempfile").gettempdir()) / "owner_ml_pool.txt"
        tmp.write_text(self.stocks[0] + "\n", encoding="utf-8")
        self.du = Discoverer(self.tf, f"owner-ml-pool-{self.tf}", cfg, "@" + str(tmp), 1)
        self.log, self.root = self.du.log, self.du.root
        self.ev_dir = self.root / "events"
        self.ev_dir.mkdir(parents=True, exist_ok=True)
        self.spec = _json.loads(Path(a.spec).read_text(encoding="utf-8"))

    def collect(self) -> None:
        """Checkpointed: a stock whose events file exists is not recomputed (--fresh redoes all)."""
        from concurrent.futures import ProcessPoolExecutor, as_completed
        todo = [s for s in self.stocks if self.a.fresh or not (self.ev_dir / f"{s}.npz").is_file()]
        self.log(f"{len(self.stocks)} stocks, {len(self.stocks) - len(todo)} already done, {len(todo)} to extract "
                 f"with {self.a.workers} workers")
        if not todo:
            return
        t0 = time.monotonic()
        with ProcessPoolExecutor(max_workers=self.a.workers) as ex:
            fut = {ex.submit(extract, s, self.tf, self.a.spec, self.a.config, str(self.ev_dir / f"{s}.npz")): s
                   for s in todo}
            for k, f in enumerate(as_completed(fut), 1):
                s = fut[f]
                try:
                    msg = f.result()
                except Exception as exc:  # noqa: BLE001 - one bad stock must not stop the run
                    msg = f"{s}: FAILED {type(exc).__name__}: {exc}"
                el = time.monotonic() - t0
                self.log(f"[{k}/{len(todo)}] {msg} ({el:.0f}s, ~{el / k * (len(todo) - k):.0f}s left)")

    def load(self) -> dict[str, np.ndarray]:
        parts: dict[str, list] = {}
        for s in self.stocks:
            p = self.ev_dir / f"{s}.npz"
            if not p.is_file():
                continue
            z = np.load(p, allow_pickle=False)
            if "X" not in z or not len(z["X"]):
                continue
            n = len(z["X"])
            for k in ("X", "side", "ts", "closed", "net", "win", "t", "c", "kind", "times"):
                parts.setdefault(k, []).append(z[k])
            parts.setdefault("stock", []).append(np.array([s] * n))
        return {k: np.concatenate(v) for k, v in parts.items()}

    def run(self) -> int:
        t0 = time.monotonic()
        self.collect()
        D = self.load()
        n = len(D["ts"])
        if not n:
            self.log("no events")
            return 1
        months = np.array([x[:7] for x in D["times"]])
        um = sorted(set(months.tolist()))
        p = np.full(n, np.nan)  # expected net result per trade, % of price after costs (out of sample)
        natr = D["X"][:, (Chart.DIRECTIONAL + Chart.NEUTRAL).index("natr_pct")].astype(float) / 100.0
        with np.errstate(divide="ignore", invalid="ignore"):
            yR = np.clip(D["net"].astype(float) / natr, -5.0, 10.0)  # profit in ATR units: comparable across stocks
        good = np.isfinite(yR)
        day = (D["ts"] // 86_400_000_000_000).astype(np.int64)
        rc = self.spec.get("recency") or {}
        for mo in um[2:]:
            sel = months == mo
            start = D["ts"][sel].min()
            tr = (D["ts"] < start) & (D["closed"] >= 0) & (D["closed"] < start) & (D["win"] >= 0) & good
            if tr.sum() < 200 or len(set(D["win"][tr].tolist())) < 2:
                continue
            age = (day[sel].min() - day[tr]) * 250 / 365  # calendar -> trading days
            w = (1 + np.maximum(age, 0) / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))
            mdl = _reg().fit(D["X"][tr], yR[tr], sample_weight=w)
            p[sel] = mdl.predict(D["X"][sel]) * natr[sel]
            self.log(f"walk-forward {mo}: trained on {int(tr.sum()):,} closed trades of earlier months, predicted "
                     f"{int(sel.sum()):,} crosses")
        names = Chart.DIRECTIONAL + Chart.NEUTRAL + ["is_short", "is_ema200_cross", "candles_since_prev_cross"]
        md = [f"# Pooled ML - expected profit of the owner strategy's crosses ({self.tf}, {len(set(D['stock'].tolist()))} "
              "stocks)", "",
              f"{n:,} crosses. Base exit: **{(self.spec.get('ml') or {}).get('base_version', 'V2')}**. Walk-forward by month: "
              "each month predicted by a model trained only on trades of ALL stocks that had closed before it (recent "
              "weigh more). Each stock is then judged on its own trades.", ""]
        ok = ~np.isnan(p) & (D["win"] >= 0) & np.isfinite(D["net"])
        auc = _auc(D["win"][ok], p[ok]) if ok.any() else None
        rho = None
        if ok.sum() > 10:
            from scipy.stats import spearmanr
            rho = float(spearmanr(p[ok], D["net"][ok]).correlation)
        md.append(f"Model = expected profit per trade (after costs). Out of sample: rank correlation with the real result "
                  f"**{rho:.3f}** (0 = none), win ranking AUC {auc:.3f}, on {int(ok.sum()):,} crosses."
                  if auc is not None and rho is not None else "Not enough months.")
        if auc is not None and rho is not None:
            self.log(f"pooled: rank correlation {rho:.3f}, AUC {auc:.3f} on {int(ok.sum()):,} crosses")

        def replay(mask: np.ndarray) -> dict[str, dict[str, Any]]:
            """Per stock and side, one position at a time, in time order; only predicted months."""
            out: dict[str, dict[str, Any]] = {}
            for s in np.unique(D["stock"]):
                for sd in (0, 1):
                    idx = np.flatnonzero((D["stock"] == s) & (D["side"] == sd) & mask & ~np.isnan(p))
                    idx = idx[np.argsort(D["t"][idx])]
                    free = -1
                    for i in idx:
                        if D["t"][i] < free or D["win"][i] < 0:
                            continue
                        r = out.setdefault(s, {"net": [], "day": []})
                        r["net"].append(float(D["net"][i]))
                        r["day"].append(int(day[i]))
                        free = int(D["c"][i])
            return out

        md += ["", "## 1. Replay over the predicted months (all stocks, after costs)", "",
               "| Taken when (expected profit per trade) | Trades | Avg per trade | Total (sum of %) | Won |",
               "|---|---|---|---|---|"]
        base_all = replay(np.ones(n, bool))
        res: dict[str, Any] = {}
        q90 = float(np.nanquantile(p, 0.9)) if np.isfinite(p).any() else 0.0
        for nm, thr in (("every cross", None), ("expected > 0", 0.0), ("expected > +0.10%", 0.001),
                        ("expected > +0.20%", 0.002), ("expected > +0.30%", 0.003),
                        (f"top 10% (> {100 * q90:+.2f}%)", q90)):
            rp = base_all if thr is None else replay(np.nan_to_num(p, nan=-9) > thr)
            res[nm] = rp
            net = np.array([x for r in rp.values() for x in r["net"]])
            if len(net):
                md.append(f"| {nm} | {len(net):,} | **{100 * net.mean():+.3f}%** | {100 * net.sum():+.0f}% | "
                          f"{(net > 0).mean():.0%} |")
                self.log(f"replay {nm}: {len(net):,} trades, avg {100 * net.mean():+.3f}%, total {100 * net.sum():+.0f}%, "
                         f"{(net > 0).mean():.0%} won")
        # per stock verdict (own trades, recent weigh more)
        L = self.spec["labels"]
        now = int(day.max())

        def label(r: dict[str, Any] | None) -> tuple[str, str]:
            if not r or not r["net"]:
                return "LC", "no trades"
            net = np.array(r["net"])
            age = (now - np.array(r["day"])) * 250 / 365
            w = (1 + age / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))
            win_w, avg_w = float(w[net > 0].sum() / w.sum()), float((w * net).sum() / w.sum())
            k, losses = len(net), int((net <= 0).sum())
            lab = "LC"
            if avg_w > 0 and net.sum() > 0:
                if k >= L["HC"]["min_trades"] and win_w >= L["HC"]["min_win_w"]:
                    lab = "HC"
                elif L["HCLT"]["min_trades"] <= k <= L["HCLT"]["max_trades"] and losses <= (
                        0 if k < L["HCLT"].get("small_below", 5) else L["HCLT"].get("max_losses", 1)):
                    lab = "HCLT"
                elif win_w >= L["MC"]["min_win_w"]:
                    lab = "MC"
            return lab, f"{k} trades, {(net > 0).mean():.0%} won, total {100 * net.sum():+.1f}%"

        fkey = "expected > +0.10%"
        filt = res[fkey]
        rows = []
        for s in sorted(set(D["stock"].tolist())):
            lb, tb = label(base_all.get(s))
            lf, tf_ = label(filt.get(s))
            rows.append((s, lb, tb, lf, tf_, sum(filt.get(s, {}).get("net", [])) if s in filt else 0.0))
        cnt = {k: sum(1 for r in rows if r[3] == k) for k in ("HC", "HCLT", "MC", "LC")}
        md += ["", f"## 2. Each stock on its own (predicted months; filter = {fkey})", "",
               f"With the ML filter: HC {cnt['HC']}, HCLT {cnt['HCLT']}, MC {cnt['MC']}, LC {cnt['LC']} stocks.", "",
               "| Stock | Every cross | Label | With ML filter | Label |", "|---|---|---|---|---|"]
        for r in sorted(rows, key=lambda r: ({"HC": 0, "HCLT": 1, "MC": 2, "LC": 3}[r[3]], -r[5])):
            md.append(f"| {'**' + r[0] + '**' if r[0] == self.a.symbol.upper() else r[0]} | {r[2]} | {r[1]} | {r[4]} | "
                      f"**{r[3]}** |")
        # final model: what drives it, today
        from sklearn.inspection import permutation_importance
        tr = (D["win"] >= 0)
        age = (now - day[tr]) * 250 / 365
        w = (1 + age / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))
        self.log(f"final model on {int(tr.sum()):,} closed trades")
        tr = tr & good
        mdl = _reg().fit(D["X"][tr], yR[tr], sample_weight=w[good[D["win"] >= 0]])
        sub = np.flatnonzero(tr)
        sub = sub[np.random.default_rng(0).permutation(len(sub))[:20000]]
        pi = permutation_importance(mdl, D["X"][sub], yR[sub], n_repeats=5, random_state=0)
        imp = sorted(zip(names, pi.importances_mean.tolist()), key=lambda kv: -kv[1])
        md += ["", "## 3. What makes a cross win (all stocks)", "",
               "Top inputs: " + ", ".join(f"{k} ({v:+.4f})" for k, v in imp[:10]), "",
               "| Distance from EMA 13 at the cross (ATR) | Crosses | Won | Avg per trade |", "|---|---|---|---|"]
        d13 = names.index("dist13_atr")
        for lo_, hi_ in ((-9, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99)):
            m_ = tr & (D["X"][:, d13] >= lo_) & (D["X"][:, d13] < hi_)
            if m_.any():
                md.append(f"| {'< 0.5' if lo_ < 0 else (f'{lo_:g} - {hi_:g}' if hi_ < 99 else f'> {lo_:g}')} | "
                          f"{int(m_.sum()):,} | {D['win'][m_].mean():.0%} | {100 * np.nanmean(D['net'][m_]):+.3f}% |")
        last_day = int(day.max())
        recent = np.flatnonzero(day >= last_day - 3)
        pr = mdl.predict(D["X"][recent]) * natr[recent] if len(recent) else np.array([])
        order = recent[np.argsort(-pr)] if len(recent) else recent
        md += ["", "## 4. Latest crosses (last 3 days), highest expected profit first", "",
               "| Stock | Cross | Side | Trigger | Expected per trade |", "|---|---|---|---|---|"]
        pmap = dict(zip(recent.tolist(), pr.tolist()))
        for i in order[:40]:
            md.append(f"| {D['stock'][i]} | {D['times'][i]} | {'short' if D['side'][i] else 'long'} | {D['kind'][i]} | "
                      f"{100 * pmap[int(i)]:+.2f}% |")
        with (self.root / "pool_events.csv").open("w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["stock", "time", "side", "kind", "win", "net_pct", "expected_pct_oos"])
            for i in range(n):
                wr.writerow([D["stock"][i], D["times"][i], "short" if D["side"][i] else "long", D["kind"][i],
                             int(D["win"][i]), round(100 * float(D["net"][i]), 3) if np.isfinite(D["net"][i]) else "",
                             "" if np.isnan(p[i]) else round(100 * float(p[i]), 3)])
        (self.root / "POOL_REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"done ({time.monotonic() - t0:.0f}s): {self.root / 'POOL_REPORT.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default=None)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--pool", default=None, help="WIN model on many stocks: N = first N of universe.txt, or @file")
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--fresh", action="store_true", help="re-extract every stock (else resume)")
    a = ap.parse_args(argv)
    if a.pool:
        pl = Pool(a)
        try:
            return pl.run()
        finally:
            pl.du.close()
    ml = OwnerML(a)
    try:
        return ml.run()
    finally:
        ml.o.du.close()


if __name__ == "__main__":
    sys.exit(main())
