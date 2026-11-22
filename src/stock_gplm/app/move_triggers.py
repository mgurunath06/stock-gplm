"""Which triggers catch the moves - per stock x timeframe, found by the program (owner 2026-11-22).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.move_triggers --symbol RELIANCE --tf 15m --check "2026-10-13 14:00"
    (options: --check repeatable, candle START time as on TradingView; --replay-months 3 (0 = off);
              --spec config/research/trigger_set.json)

1. MOVES: every leg of the stock (phase_patterns: intraday +2% before -1%, held overnight; 1d +1 ATR / -0.5 ATR).
2. TRIGGERS: hundreds of events generated from trigger_set.json (EMA crosses of every pair, price x EMA, MACD, RSI,
   stochastic, breakouts, volume spikes, VWAP, supertrend / PSAR flips, candle patterns) plus the strategy library.
   A trigger fires on the first candle its condition becomes true.
3. EXITS: structure exits - stop below the last minor / major swing low, target EMA 50/100/200 or 2R/3R, held until
   stop or target (overnight on intraday); real trade engine, costs, one position at a time.
4. FILTER: per trigger x exit, the best of a SHORT list of trader conditions (phase, above/below EMA 200, EMA 200
   rising, day up, daily / hourly trend, volume, RSI) - kept only if it improves the trigger's own label.
   TREND GUARD: no longs while below a falling hourly EMA 200 (trigger_set.json -> trend_guard).
   SAME MOVE ONCE: strategies whose wins came from the same moves are listed once ('similar' count).
5. LABELS (recent trades weigh more - owner's most important rule): HC high confidence, HCLT high confidence but low
   trades, MC medium, LC low/avoid (trigger_set.json -> labels).
6. CATCH: for every move, which triggers fired in the 10 candles before it; per trigger how many moves it caught and
   how often it fired without a move.
7. CHECK: --check candles (owner examples): which triggers fired around them and their labels.
8. REPLAY (causal, no split date): every 10 sessions of the last 3 months, standing at that moment with closed trades
   only, take signals of the top HC/HCLT strategies until the next refit, one position at a time -> every trade the
   program would have asked you to take, with date and time.
9. TODAY: HC / HCLT / MC strategies firing on the last candle.
Output <reports>/tuning/move-triggers-<tf>-<SYM>/REPORT.md, strategies.csv, replay_trades.csv, candidates.json
(HC + HCLT, library format, not in the library until approved).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from .phase_patterns import PATTERNS_CFG, PHASES, Finder
from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "config" / "research" / "trigger_set.json"
TF_MIN = {"5m": 5, "15m": 15, "60m": 60}
RANK = {"HC": 3, "HCLT": 2, "MC": 1, "LC": 0}


def generate(spec: dict[str, Any], intraday: bool) -> dict[str, str]:
    g = spec.get("generate") or {}
    P = [int(x) for x in spec.get("ema_periods", [])]
    E = lambda n: f"ema(close, {n})"  # noqa: E731
    out: dict[str, str] = {}
    if g.get("ema_cross"):
        for i, a in enumerate(P):
            for b in P[i + 1:]:
                out[f"EMA{a}x{b}"] = f"cross_above({E(a)}, {E(b)})"
    if g.get("price_cross_ema"):
        for a in P:
            out[f"Close_x_EMA{a}"] = f"cross_above(close, {E(a)})"
    if g.get("macd"):
        out["MACD_x_signal"] = "cross_above(macd(close), macd_signal(close))"
        out["MACD_x_signal_below0"] = "cross_above(macd(close), macd_signal(close)) and macd(close) < 0"
        out["MACD_x_zero"] = "cross_above(macd(close), 0)"
    r = g.get("rsi") or {}
    for n in r.get("periods", []):
        for lv in r.get("levels", []):
            out[f"RSI{n}_x_{lv}"] = f"cross_above(rsi(close, {n}), {lv})"
    s = g.get("stoch") or {}
    for lv in s.get("levels", []):
        out[f"Stoch14_x_{lv}"] = f"cross_above(stoch_k(14), {lv})"
    if s:
        out["Stoch14_K_x_D"] = "cross_above(stoch_k(14), stoch_d(14))"
    for n in g.get("breakout", []):
        out[f"Breakout_{n}"] = f"cross_above(close, highest(high, {n})[1])"
    for k in g.get("volume_spike", []):
        out[f"VolSpike{k:g}x_green"] = f"volume > {k} * sma(volume, 20) and close > open"
    if g.get("flips"):
        out["Supertrend_flip_up"] = "supertrend_dir(10, 3) > 0 and supertrend_dir(10, 3)[1] < 0"
        out["PSAR_flip_up"] = "cross_above(close, psar())"
    if g.get("candles"):
        out["Bull_engulfing"] = "close > open and close[1] < open[1] and close > open[1] and open < close[1]"
        out["Green_hammer"] = "close > open and (open - low) >= 2 * (close - open) and (high - close) <= (close - open)"
        out["Inside_bar_breakout"] = "high[1] < high[2] and low[1] > low[2] and close > high[1]"
        out["Three_green"] = ("close > open and close[1] > open[1] and close[2] > open[2] and close > close[1] and "
                              "close[1] > close[2]")
    if g.get("vwap") and intraday:
        out["VWAP_reclaim"] = "cross_above(close, vwap)"
    out.update(spec.get("extra") or {})
    return out


def library_triggers(sym: str, tf: str) -> dict[str, str]:
    from .strategy_library import StrategyLibrary
    lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], Path(tempfile.mkdtemp())).load(record=False)
    out = {}
    for s in lib.strategies.values():
        d = s.doc
        if (not d.get("enabled", True) or s.blocked or d.get("direction", "long") != "long" or d.get("params")
                or not s.applies(f"{sym}-EQ", tf)):
            continue
        out[f"LIB:{s.id}"] = str(d["entry"])
    return out


class MoveTriggers:
    def __init__(self, sym: str, tf: str, a: argparse.Namespace):
        self.sym, self.tf, self.a = sym.upper().removesuffix("-EQ"), tf, a
        self.spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
        self.f = Finder(argparse.Namespace(tf=tf, symbol=self.sym, side="long", patterns=a.patterns,
                                           config=a.config))
        if self.spec.get("recency"):
            self.f.pc["recency"] = self.spec["recency"]
        self.log = self.f.log
        self.cache: dict[Any, list] = {}
        self.exits = self.spec["exits"]

    # ------------------------------------------------------------------ helpers
    def tv(self, i: int) -> str:
        """Candle START time (TradingView), IST; date on 1d."""
        f = self.f
        if not f.intraday:
            return str(np.datetime64(int(f.days[i]), "D"))
        ts = np.datetime64(int(f.end_ns[i]), "ns") + np.timedelta64(330 - TF_MIN.get(self.tf, 0), "m")
        return str(ts.astype("datetime64[m]")).replace("T", " ")

    def find(self, text: str) -> int | None:
        for i in range(self.f.n):
            if self.tv(i).startswith(text):
                return i
        return None

    def cond_text(self, cond: str | None) -> str:
        return cond or ""

    def cond_dsl(self, cond: str) -> str:
        return f"({self.filters[cond][0]})"

    def distinct(self, cands: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
        """Owner 2026-11-22: count a big move once - skip a strategy whose winning trades closed in the same
        sessions as an already listed one (overlap > same_move_overlap). Returns (strategy, similar-count) order."""
        lim = float(self.spec.get("same_move_overlap", 0.6))
        f = self.f
        shown: list[tuple[dict[str, Any], set]] = []
        for c in cands:
            kn = self.closed(self.trades(c["key"], c["mask"], c["exit"]), f.cut)
            wins = {int(f.sid[t["exit_index"]]) for t in kn if t["net_return"] > 0}
            ids = wins or {int(f.sid[t["t"]]) for t in kn}
            dup = None
            for s, sset in shown:
                if ids and sset and len(ids & sset) / min(len(ids), len(sset)) > lim:
                    dup = s
                    break
            if dup is not None:
                dup["similar"] = dup.get("similar", 0) + 1
                continue
            c["similar"] = 0
            shown.append((c, ids))
            if len(shown) >= top:
                break
        return [s for s, _ in shown]

    def label(self, s: dict[str, Any]) -> str:
        L = self.spec["labels"]
        if s["trades"] == 0 or s["avg_w"] <= 0 or s["total"] <= 0:
            return "LC"
        hc, hl, mc = L["HC"], L["HCLT"], L["MC"]
        if s["trades"] >= hc["min_trades"] and s["win_w"] >= hc["min_win_w"]:
            return "HC"
        if hl["min_trades"] <= s["trades"] <= hl["max_trades"]:
            allowed = 0 if s["trades"] < hl.get("small_below", 5) else hl.get("max_losses", 1)
            if s["losses"] <= allowed:
                return "HCLT"
        if s["win_w"] >= mc["min_win_w"]:
            return "MC"
        return "LC"

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        from . import strategy_dsl as dsl
        f = self.f
        if not f.load():
            return False
        trig = generate(self.spec, f.intraday)
        if self.spec.get("include_library", True):
            trig.update(library_triggers(self.sym, self.tf))
        self.exprs: dict[str, str] = {}
        self.events: dict[str, np.ndarray] = {}
        ctx = f.L["ctx"]

        def state(expr: str) -> np.ndarray | None:
            try:
                return np.asarray(dsl.signal(expr, ctx), bool)
            except Exception:  # noqa: BLE001 - not computable on this stock / timeframe
                return None

        # trend guard (owner 2026-11-22): no longs while below a falling EMA 200 (hourly)
        g = self.spec.get("trend_guard") or {}
        self.guard_expr, block = None, np.zeros(f.n, bool)
        if g.get("enabled", False):
            for ex in g.get("block_when", []):
                b = state(ex)
                if b is not None:
                    self.guard_expr, block = ex, b
                    break
            self.log(f"trend guard: {'blocks ' + format(int(block[f.a0:].sum()), ',') + ' candles: ' + self.guard_expr if self.guard_expr else 'not computable - off'}")
        self.block = block
        # short-list filters (owner 2026-11-22)
        self.filters: dict[str, tuple[str, np.ndarray]] = {}
        for name, ex in ((self.spec.get("filter") or {}).get("conditions") or {}).items():
            fm = state(ex)
            if fm is not None:
                self.filters[name] = (ex, fm)
        self.log(f"filters: {', '.join(self.filters) or 'none'}")
        bad = 0
        for name, expr in trig.items():
            mk = state(f"{f.du.floor} and ({expr})")
            if mk is None:
                bad += 1
                continue
            ev = mk & ~np.r_[False, mk[:-1]] & ~block
            ev[:f.a0] = False
            if ev.any():
                self.exprs[name], self.events[name] = expr, ev
        self.log(f"{len(self.events)} triggers fire on {self.sym} {self.tf} ({bad} could not be computed); "
                 f"{len(self.exits)} exits")
        return True

    def trades(self, key: Any, mask: np.ndarray, en: str) -> list[dict[str, Any]]:
        from .strategy_engine import Exit, simulate
        k = (key, en)
        if k not in self.cache:
            f = self.f
            self.cache[k] = simulate(f.L["m"], mask, (f.a0, f.n), Exit.of(self.exits[en]), f.L["buy"], f.L["sell"],
                                     False)
        return self.cache[k]

    def closed(self, trades: list[dict[str, Any]], cut: int) -> list[dict[str, Any]]:
        return [t for t in trades if t["exit_index"] < cut and t["exit_reason"] != "OPEN_END"]

    def stats(self, known: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(known)
        if not n:
            s = {"trades": 0, "win": 0.0, "avg": 0.0, "pf": 0.0, "total": 0.0, "win_w": 0.0, "avg_w": 0.0,
                 "pf_w": 0.0, "n_eff": 0.0, "losses": 0, "score": -9.0}
            s["label"], s["rank"] = "LC", 0
            return s
        net = np.array([t["net_return"] for t in known], dtype=float)
        w = self.f.wt(np.array([t["t"] for t in known], dtype=np.int64))
        gl, glw = -net[net <= 0].sum(), -(w * net)[net <= 0].sum()
        s = {"trades": n, "win": float((net > 0).mean()), "avg": float(net.mean()),
             "pf": float(net[net > 0].sum() / gl) if gl > 0 else 99.0, "total": float(net.sum()),
             "win_w": float(w[net > 0].sum() / w.sum()), "avg_w": float((w * net).sum() / w.sum()),
             "pf_w": float((w * net)[net > 0].sum() / glw) if glw > 0 else 99.0,
             "n_eff": float(w.sum() ** 2 / (w ** 2).sum()), "losses": int((net <= 0).sum())}
        s["score"] = s["avg_w"] * np.sqrt(s["n_eff"])
        s["label"] = self.label(s)
        s["rank"] = RANK[s["label"]]
        return s

    # ------------------------------------------------------------------ evaluation standing at `cut`
    def best_filter(self, c: dict[str, Any], cut: int) -> dict[str, Any] | None:
        f = self.f
        known = self.closed(self.trades(c["name"], c["mask"], c["exit"]), cut)
        if len(known) < 5:
            return None
        if not self.filters:
            return None
        ts = np.array([t["t"] for t in known], dtype=np.int64)
        net = np.array([t["net_return"] for t in known], dtype=float)
        w = f.wt(ts)
        names = list(self.filters)
        Mt = np.stack([self.filters[nm][1][ts] for nm in names]).astype(float)
        k = Mt.sum(1)
        sw = Mt @ w
        with np.errstate(divide="ignore", invalid="ignore"):
            avg_w = (Mt @ (w * net)) / sw
            neff = sw ** 2 / (Mt @ (w * w))
            score = avg_w * np.sqrt(neff)
        ok = (k >= int(self.spec["filter"].get("min_keep", 3))) & (k < len(ts)) & np.isfinite(score) & (avg_w > 0)
        if not ok.any():
            return None
        cond = names[int(np.argmax(np.where(ok, score, -np.inf)))]
        mask2 = c["mask"] & self.filters[cond][1]
        key = (c["name"], cond)
        s2 = self.stats(self.closed(self.trades(key, mask2, c["exit"]), cut))
        if (s2["rank"], s2["score"]) <= (c["stats"]["rank"], c["stats"]["score"]):
            return None
        return {"name": c["name"], "exit": c["exit"], "cond": cond, "key": key, "mask": mask2, "stats": s2,
                "alone": c["stats"]}

    def evaluate(self, cut: int) -> list[dict[str, Any]]:
        f = self.f
        f.set_cut(cut)
        cands = []
        for name, ev in self.events.items():
            for en in self.exits:
                s = self.stats(self.closed(self.trades(name, ev, en), cut))
                if s["trades"]:
                    cands.append({"name": name, "exit": en, "cond": None, "key": name, "mask": ev, "stats": s,
                                  "alone": s})
        pool = sorted([c for c in cands if c["stats"]["trades"] >= 5], key=lambda c: -c["stats"]["score"])
        for c in pool[:int(self.spec["filter"].get("top_combos", 250))]:
            fc = self.best_filter(c, cut)
            if fc:
                cands.append(fc)
        cands.sort(key=lambda c: (c["stats"]["rank"], c["stats"]["score"]), reverse=True)
        return cands

    def text(self, c: dict[str, Any]) -> str:
        return c["name"] + (f" AND {self.cond_text(c['cond'])}" if c["cond"] is not None else "")

    def fmt(self, s: dict[str, Any]) -> str:
        if not s["trades"]:
            return "no closed trades"
        return (f"{s['trades']} trades, {s['trades'] - s['losses']} won, avg {100 * s['avg']:+.2f}%, PF "
                f"{min(s['pf'], 99):.2f}, total {100 * s['total']:+.1f}% | recent-weighted win {s['win_w']:.0%}, avg "
                f"{100 * s['avg_w']:+.2f}%")

    # ------------------------------------------------------------------ replay
    def replay(self, months: float, every: int, top: int) -> tuple[list[dict], list[str]]:
        from .strategy_engine import Exit, simulate
        f = self.f
        start_day = int(f.days[-1]) - int(round(30.4 * months))
        first = int(np.searchsorted(f.days, start_day))
        sids = np.unique(f.sid[first:])
        cuts = [int(np.searchsorted(f.sid, s)) for s in sids[::every]] + [f.n]
        rows: list[dict] = []
        notes: list[str] = []
        free = -1
        for k in range(len(cuts) - 1):
            c, nc = cuts[k], cuts[k + 1]
            t0 = time.monotonic()
            picks = self.distinct([x for x in self.evaluate(c) if x["stats"]["rank"] >= RANK["HCLT"]], top)
            notes.append(f"{self.tv(c - 1)}: " + ("; ".join(f"{x['stats']['label']} {self.text(x)} [{x['exit']}]"
                                                              for x in picks) or "no HC/HCLT strategy"))
            sig = sorted((int(t), i) for i, x in enumerate(picks) for t in np.flatnonzero(x["mask"][c:nc]) + c)
            took = 0
            for t, i in sig:
                if t < free or t + 1 >= f.n:
                    continue
                x = picks[i]
                tr = simulate(f.L["m"], x["mask"], (f.a0, f.n), Exit.of(self.exits[x["exit"]]), f.L["buy"],
                              f.L["sell"], False, fired=np.array([t]))
                if not tr:
                    continue  # no valid stop / target (e.g. price already above the EMA target)
                tr = tr[0]
                still = tr["exit_reason"] == "OPEN_END"
                rows.append({"signal": self.tv(t), "buy_at": round(tr["entry_price"], 2),
                             "closed": "-" if still else self.tv(tr["exit_index"]),
                             "exit_price": "-" if still else round(tr["exit_price"], 2),
                             "result_pct": "open" if still else round(100 * tr["net_return"], 2),
                             "how": "still running" if still else tr["exit_reason"],
                             "label": x["stats"]["label"], "strategy": self.text(x), "exit": x["exit"],
                             "picked_at": self.tv(c - 1)})
                free = tr["exit_index"] + (f.n if still else 0)
                took += 1
            self.log(f"replay {k + 1}/{len(cuts) - 1}: standing at {self.tv(c - 1)} - {len(picks)} HC/HCLT "
                     f"strategies, {took} trade(s) until {self.tv(nc - 1)} ({time.monotonic() - t0:.0f}s)")
        return rows, notes

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        f = self.f
        t0 = time.monotonic()
        if not self.load():
            return 1
        self.log("evaluating every trigger x exit (+ best filter) on all closed trades, recent weigh more")
        final = self.evaluate(f.n)
        legs = f.legs()
        root = f.du.root.parent / f"move-triggers-{self.tf}-{self.sym}"
        root.mkdir(parents=True, exist_ok=True)
        now = f.n - 1
        counts: dict[str, int] = {}
        for c in final:
            counts[c["stats"]["label"]] = counts.get(c["stats"]["label"], 0) + 1
        best_by_trig: dict[str, dict] = {}
        for c in final:
            best_by_trig.setdefault(c["name"], c)
        md = [f"# Move triggers - {self.sym} {self.tf}", "",
              f"Data {self.tv(f.a0)} .. {self.tv(now)} (candle start times, IST). Today: phase {PHASES[int(f.phase[now])]}."
              f" {len(self.events)} triggers x {len(self.exits)} exits (+ best filter each). Labels, recent-weighted: "
              f"HC {counts.get('HC', 0)}, HCLT {counts.get('HCLT', 0)}, MC {counts.get('MC', 0)}, LC {counts.get('LC', 0)}.",
              ""]
        # today
        today = [c for c in final if c["stats"]["rank"] >= RANK["MC"] and c["mask"][now]]
        md += ["## 1. Signals on the last candle", ""]
        md += ([f"- **{c['stats']['label']}** {self.text(c)} [{c['exit']}]: {self.fmt(c['stats'])}" for c in today[:20]]
               or ["None of the HC / HCLT / MC strategies fired on the last candle."])
        # strategies
        guard = (f"Trend guard on: no longs while `{self.guard_expr}`." if self.guard_expr else "Trend guard off.")
        md += ["", "## 2. Best strategies (trigger [+ short-list filter] x exit)", "",
               f"{guard} Strategies that caught the same moves are listed once ('similar' = how many more caught "
               "mostly the same moves). 'Trigger alone' = the label without any filter.", "",
               "| Label | Trigger alone | Trigger [+ filter] | Exit | Closed trades | Similar | Last trade |",
               "|---|---|---|---|---|---|---|"]
        top_distinct = self.distinct(final, 40)
        for c in top_distinct:
            kn = self.closed(self.trades(c["key"], c["mask"], c["exit"]), f.n)
            last = f"{self.tv(kn[-1]['t'])} {100 * kn[-1]['net_return']:+.1f}%" if kn else "-"
            md.append(f"| **{c['stats']['label']}** | {c['alone']['label']} | {self.text(c)} | {c['exit']} | "
                      f"{self.fmt(c['stats'])} | {c.get('similar', 0)} | {last} |")
        # catch
        W = int(self.spec.get("catch_window", 10))
        md += ["", f"## 3. Which triggers catch the moves ({len(legs)} moves; trigger within {W} candles before a move "
               "start)", "", "| Trigger | Moves caught | Fired | Fired before a move | Best label |",
               "|---|---|---|---|---|"]
        crow = []
        for name, ev in self.events.items():
            E = np.flatnonzero(ev)
            if not len(E) or not len(legs):
                continue
            lo = np.searchsorted(E, legs - W)
            hi = np.searchsorted(E, legs + 2, side="right")
            caught = int((hi > lo).sum())
            lo2 = np.searchsorted(legs, E - 2)
            hi2 = np.searchsorted(legs, E + W, side="right")
            before = int((hi2 > lo2).sum())
            crow.append((caught, name, len(E), before))
        crow.sort(reverse=True)
        for caught, name, nE, before in crow[:30]:
            bl = best_by_trig.get(name, {}).get("stats", {}).get("label", "-")
            md.append(f"| {name} | {caught} of {len(legs)} ({caught / len(legs):.0%}) | {nE} | {before} "
                      f"({before / nE:.0%}) | {bl} |")
        md += ["", "Recent moves and the triggers that fired just before them (best label first):", "",
               "| Move start | Biggest move | Triggers within the window |", "|---|---|---|"]
        e = len(f.exits) - 1
        for s0 in legs[::-1][:30]:
            fired = [n for n, ev in self.events.items() if ev[max(0, s0 - W):s0 + 3].any()]
            fired.sort(key=lambda n: -RANK.get(best_by_trig.get(n, {}).get("stats", {}).get("label", "LC"), 0))
            md.append(f"| {self.tv(s0)} | {100 * f.peak[e, s0]:+.1f}% | " + ", ".join(
                f"{n} ({best_by_trig.get(n, {}).get('stats', {}).get('label', '-')})" for n in fired[:10]) + " |")
        # owner examples
        for x in self.a.check or []:
            i = self.find(x)
            md += ["", f"## Check {x}", ""]
            if i is None:
                md.append(f"No {self.tf} candle starts at {x}.")
                continue
            near = [(int(t), n) for n, ev in self.events.items() for t in np.flatnonzero(ev[max(0, i - 3):i + 4])
                    + max(0, i - 3)]
            near.sort()
            md += [f"- {self.tv(t)}: {n} - best: {best_by_trig[n]['stats']['label']} {self.text(best_by_trig[n])} "
                   f"[{best_by_trig[n]['exit']}] {self.fmt(best_by_trig[n]['stats'])}" for t, n in near[:40]
                   if n in best_by_trig] or ["No trigger fired within 3 candles."]
        # replay
        months = self.a.replay_months if self.a.replay_months is not None else float(self.spec["replay"]["months"])
        if months > 0:
            rp = self.spec["replay"]
            self.log(f"causal replay of the last {months:g} months")
            rows, notes = self.replay(months, int(rp["refit_sessions"]), int(rp.get("top", 5)))
            done = np.array([r["result_pct"] for r in rows if r["result_pct"] != "open"], dtype=float)
            res = (f"{len(done)} closed trades, {int((done > 0).sum())} won, avg {done.mean():+.2f}%, total "
                   f"{done.sum():+.1f}%" if len(done) else "no closed trades")
            md += ["", f"## Replay: every trade the program would have asked you to take (last {months:g} months)", "",
                   "Causal, no split date: at each refit only trades closed before that moment were known (recent "
                   "weigh more); signals of the top HC/HCLT strategies taken until the next refit, one position at a "
                   "time. Times = candle START (TradingView); buy at the next candle's open.", "",
                   f"**{res}**", "",
                   "| Signal candle | Buy at | Closed | Exit price | Result | How | Label | Strategy | Exit |",
                   "|---|---|---|---|---|---|---|---|---|"]
            md += [f"| {r['signal']} | {r['buy_at']} | {r['closed']} | {r['exit_price']} | "
                   + (f"{r['result_pct']:+.2f}%" if r["result_pct"] != "open" else "open")
                   + f" | {r['how']} | {r['label']} | {r['strategy']} | {r['exit']} |" for r in rows]
            md += ["", "Strategies picked at each refit:", ""] + [f"- {x}" for x in notes]
            with (root / "replay_trades.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["signal", "buy_at", "closed", "exit_price", "result_pct", "how",
                                                   "label", "strategy", "exit", "picked_at"])
                w.writeheader()
                w.writerows(rows)
        (root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        # all strategies + candidates
        f.set_cut(f.n)
        with (root / "strategies.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["label", "trigger", "filter", "exit", "trades", "won", "avg_pct", "pf", "total_pct", "win_w",
                        "avg_w_pct"])
            for c in final:
                s = c["stats"]
                w.writerow([s["label"], c["name"], self.cond_text(c["cond"]), c["exit"], s["trades"],
                            s["trades"] - s["losses"], round(100 * s["avg"], 3), round(min(s["pf"], 99), 3),
                            round(100 * s["total"], 2), round(s["win_w"], 3), round(100 * s["avg_w"], 3)])
        cands = []
        for c in self.distinct([c for c in final if c["stats"]["rank"] >= RANK["HCLT"]], 10 ** 6):
            x = self.exprs[c["name"]]
            core = f"(({x}) and not (({x})[1]))"
            entry = (f"{f.du.floor} and {core}" + (f" and {self.cond_dsl(c['cond'])}" if c["cond"] is not None else "")
                     + (f" and not ({self.guard_expr})" if self.guard_expr else ""))
            sid = f"{self.sym}-{self.tf}-{c['stats']['label']}-{c['name'].replace(':', '_')}-{c['exit']}"
            if c["cond"] is not None:
                sid += "-" + c["cond"]
            cands.append({"id": sid, "name": f"{self.sym} {self.tf}: {self.text(c)} [{c['exit']}]", "direction": "long",
                          "description": f"move_triggers {date.today()}: {c['stats']['label']} - {self.fmt(c['stats'])}",
                          "entry": entry, "exit": self.exits[c["exit"]], "enabled": True,
                          "applicability": {"symbols": [f"{self.sym}-EQ"], "timeframes": [self.tf]},
                          "tags": ["stock-specific", "paper-only", "move_triggers", f"label:{c['stats']['label']}"]})
        (root / "candidates.json").write_text(json.dumps(
            {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": cands,
             "keep_original": []}, indent=1), encoding="utf-8")
        self.log(f"{self.sym} {self.tf}: HC {counts.get('HC', 0)}, HCLT {counts.get('HCLT', 0)}, MC {counts.get('MC', 0)}"
                 f"; {len(cands)} HC/HCLT candidates ({time.monotonic() - t0:.0f}s); report {root / 'REPORT.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--check", action="append", help='candle START time as on TradingView, e.g. "2026-10-13 14:00"')
    ap.add_argument("--replay-months", type=float, default=None)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--patterns", default=str(PATTERNS_CFG))
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    mt = MoveTriggers(a.symbol, a.tf, a)
    try:
        return mt.run()
    finally:
        mt.f.du.close()


if __name__ == "__main__":
    sys.exit(main())
