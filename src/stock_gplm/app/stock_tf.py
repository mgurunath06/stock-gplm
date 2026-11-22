"""Fine-tune a strategy per stock x timeframe, by itself (owner 2026-11-22: "the program does it, not me").

    $env:PYTHONPATH="src"; python -m stock_gplm.app.stock_tf --symbol RELIANCE --tfs 15m
    (options: --stocks 200 | @file instead of --symbol; --tfs 1d,60m,15m,5m; --replay-months 3 (0 = off);
              --redo; --side long|short)

Per stock x TF, no questions asked, RECENT TRADES WEIGH MORE everywhere (owner's most important rule; weight =
(1 + age_sessions/100)^-2 as in confidence.py, config/research/phase_patterns.json -> recency):
1. phase_patterns: phases (EMA 9/13/20/50 stack), moves (intraday +2% before -1%, held overnight; 1d +1 ATR before
   -0.5 ATR), patterns per phase, scored recency-weighted.
2. The best patterns of every phase -> readable strategies (DSL: phase + conditions, same exits).
3. Each through the REAL trade engine (costs, gaps, one position at a time); DSL must match the research signals.
4. Gate on trades already CLOSED at the moment of the decision, recency-weighted: trades, effective trades,
   weighted PF and average (builder.gate). Best passing = that stock x TF's strategy for TODAY.
5. CAUSAL REPLAY (no split date): over the last N months, every 10 sessions the program stands at that moment,
   knows only trades closed before it, picks the strategy as in 1-4 and takes its signals until the next refit, one
   position at a time. The list = every trade the program would have asked you to take, with date and time.
Output <reports>/tuning/stock-tf-<tf>-<SYM>-<side>/BUILD.md, replay_trades.csv, candidates.json; one row per stock x
TF in <reports>/tuning/stock-tf-summary.csv; passing strategies in stock-tf-candidates.json. Checkpointed: a finished
stock x TF is skipped unless --redo.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from .phase_patterns import PATTERNS_CFG, PHASES, Finder
from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]


def _stats(net: np.ndarray, w: np.ndarray | None = None) -> dict[str, Any]:
    n = len(net)
    if not n:
        return {"trades": 0, "win": 0.0, "avg": 0.0, "pf": 0.0, "total": 0.0, "win_w": 0.0, "avg_w": 0.0,
                "pf_w": 0.0, "n_eff": 0.0}
    w = np.ones(n) if w is None else w
    gl, glw = -net[net <= 0].sum(), -(w * net)[net <= 0].sum()
    return {"trades": int(n), "win": float((net > 0).mean()), "avg": float(net.mean()),
            "pf": float(net[net > 0].sum() / gl) if gl > 0 else 99.0, "total": float(net.sum()),
            "win_w": float(w[net > 0].sum() / w.sum()), "avg_w": float((w * net).sum() / w.sum()),
            "pf_w": float((w * net)[net > 0].sum() / glw) if glw > 0 else 99.0,
            "n_eff": float(w.sum() ** 2 / (w ** 2).sum())}


def _fmt(s: dict[str, Any], weighted: bool = True) -> str:
    if not s["trades"]:
        return "no trades"
    out = (f"{s['trades']} trades, win {s['win']:.0%}, avg {100 * s['avg']:+.2f}%, PF {min(s['pf'], 99):.2f}, "
           f"total {100 * s['total']:+.1f}%")
    if weighted:
        out += (f" | recent-weighted: win {s['win_w']:.0%}, avg {100 * s['avg_w']:+.2f}%, PF {min(s['pf_w'], 99):.2f}"
                f" ({s['n_eff']:.1f} eff. trades)")
    return out


class Builder:
    def __init__(self, sym: str, tf: str, a: argparse.Namespace):
        self.sym, self.tf, self.a = sym.upper().removesuffix("-EQ"), tf, a
        self.f = Finder(argparse.Namespace(tf=tf, symbol=self.sym, side=a.side, patterns=a.patterns, config=a.config))
        self.bc = self.f.pc["builder"]
        self.log = self.f.log
        self.masks: dict[str, np.ndarray] = {}
        self.engine: dict[str, list] = {}

    # ------------------------------------------------------------------ DSL
    def expr(self, j: int) -> str:
        f = self.f
        nm = f.names[j]
        if j < f.base_n:
            return f.exprs[nm]
        base, lb = nm.rsplit("_chg", 1)
        e = f.exprs[base]
        return f"(({e}) - ({e})[{lb}])"

    def cond_dsl(self, c: int, alt: bool = False) -> str:
        j, kind, lv = self.f.conds[c]
        ex = self.expr(j)
        if kind == "ge":
            return f"({ex}) >= {lv!r}"
        if kind == "lt":
            return f"({ex}) < {lv!r}"
        if alt:  # yes/no as plain boolean
            return f"({ex})" if kind == "yes" else f"not ({ex})"
        return f"({ex}) > 0.5" if kind == "yes" else f"({ex}) <= 0.5"

    def phase_dsl(self, q: int) -> str:
        ph = self.f.pc["phase"]
        if q == 1:
            return f"({ph['bullish']})"
        if q == -1:
            return f"({ph['bearish']})"
        return f"not ({ph['bullish']}) and not ({ph['bearish']})"

    def exit_doc(self, e: int) -> dict[str, Any]:
        f = self.f
        h = int(f.exits[e])
        if f.atr_mode:
            return {"max_hold_bars": h, "stop_atr": f.loss, "target_atr": f.gain}
        ex: dict[str, Any] = {"max_hold_bars": h, "stop_pct": f.loss, "target_pct": f.gain}
        if f.intraday and f.overnight:
            ex["overnight"] = True
        return ex

    # ------------------------------------------------------------------ pick + check, standing at f.cut
    def picks(self) -> list[tuple[int, tuple[int, ...], dict]]:
        f, bc = self.f, self.bc
        out = []
        for q, lst in f.res.items():
            keep = []
            for rule, x in lst:
                n = len(x["times"])
                base = f.base[q][x["e"]]
                if n >= bc["min_times"] and x["avg_w"] > 0 and base > 0 and x["rate_w"] >= bc["min_x_base"] * base:
                    keep.append((rule, x))
            keep.sort(key=lambda rx: -rx[1]["avg_w"] * np.sqrt(rx[1]["n_eff"]))
            out += [(q, r, x) for r, x in f.distinct(keep, int(bc["candidates_per_phase"]))]
        return out

    def check(self, q: int, rule: tuple[int, ...], x: dict) -> dict[str, Any] | None:
        from . import strategy_dsl as dsl
        from .strategy_engine import Exit, simulate
        f = self.f
        L = f.L
        m, ctx = L["m"], L["ctx"]
        cut = f.cut
        research = f.ok & (f.phase == q) & np.logical_and.reduce(f.M[list(rule)])
        mask = entry = None
        for alt in (False, True):
            core = " and ".join([self.phase_dsl(q)] + [self.cond_dsl(c, alt) for c in rule])
            entry = f"{f.du.floor} and {core}"
            if entry in self.masks:
                mask = self.masks[entry]
                break
            try:
                mask = np.asarray(dsl.signal(entry, ctx), bool)
                self.masks[entry] = mask
                break
            except Exception:  # noqa: BLE001 - try the plain-boolean form, then give up on this pattern
                mask = None
        if mask is None:
            return None
        rs = research[f.a0:cut].sum()
        agree = float((mask & research)[f.a0:cut].sum() / rs) if rs else 0.0
        ex = self.exit_doc(x["e"])
        key = entry + json.dumps(ex, sort_keys=True)
        if key not in self.engine:
            self.engine[key] = simulate(m, mask, (f.a0, f.n), Exit.of(ex), L["buy"], L["sell"], f.short)
        trades = self.engine[key]
        known = [t for t in trades if t["exit_index"] < cut]  # only results known at the moment of the decision
        net = np.array([t["net_return"] for t in known], dtype=float)
        ts = np.array([t["t"] for t in known], dtype=np.int64)
        s = _stats(net, f.wt(ts) if len(ts) else None)
        g = self.bc["gate"]
        why = []
        if s["trades"] < g["min_trades"]:
            why.append(f"trades {s['trades']} < {g['min_trades']}")
        if s["n_eff"] < g.get("min_eff_trades", 0):
            why.append(f"eff. trades {s['n_eff']:.1f} < {g['min_eff_trades']}")
        if s["pf_w"] < g["min_pf"]:
            why.append(f"weighted PF {s['pf_w']:.2f} < {g['min_pf']}")
        if s["avg_w"] <= g["min_avg"]:
            why.append("weighted avg <= 0")
        if agree < g["min_dsl_agreement"]:
            why.append(f"DSL agreement {agree:.0%}")
        return {"phase": q, "rule": rule, "text": f"[{PHASES[q]}] " + f.rtext(rule), "entry": entry, "exit": ex,
                "mask": mask, "stats": s, "agree": agree, "passed": not why, "why": "; ".join(why),
                "trades": trades, "known": known, "conf": x["conf"], "streak": x["streak"],
                "score": s["avg_w"] * np.sqrt(max(s["n_eff"], 0.0))}

    def build(self, cut: int) -> tuple[dict | None, list[dict]]:
        self.f.analyse(cut)
        checked = [r for q, rule, x in self.picks() if (r := self.check(q, rule, x))]
        checked.sort(key=lambda r: (r["passed"], r["score"]), reverse=True)
        best = checked[0] if checked and checked[0]["passed"] else None
        return best, checked

    # ------------------------------------------------------------------ causal replay
    def replay(self, months: float, every: int) -> tuple[list[dict], list[str]]:
        f = self.f
        start_day = int(f.days[-1]) - int(round(30.4 * months))
        first = int(np.searchsorted(f.days, start_day))
        sids = np.unique(f.sid[first:])
        cuts = [int(np.searchsorted(f.sid, s)) for s in sids[::every]] + [f.n]
        rows: list[dict] = []
        notes: list[str] = []
        free = -1  # one position at a time: next entry only after the previous trade closed
        for k in range(len(cuts) - 1):
            c, nc = cuts[k], cuts[k + 1]
            t0 = time.monotonic()
            best, _ = self.build(c)
            if not best:
                notes.append(f"{f.when(c - 1)}: no strategy passed - no trades until {f.when(nc - 1)}")
                self.log(f"replay {k + 1}/{len(cuts) - 1}: standing at {f.when(c - 1)} - no strategy passed "
                         f"({time.monotonic() - t0:.0f}s)")
                continue
            notes.append(f"{f.when(c - 1)}: {best['text']} (max {best['exit']['max_hold_bars']} candles) - known "
                         f"then: {_fmt(best['stats'])}")
            from .strategy_engine import Exit, simulate
            took = 0
            fired = np.flatnonzero(best["mask"][c:nc]) + c
            fired = fired[fired >= free]
            trades = simulate(f.L["m"], best["mask"], (f.a0, f.n), Exit.of(best["exit"]), f.L["buy"], f.L["sell"],
                              f.short, fired=fired) if len(fired) else []
            for t in trades:  # one position at a time inside the window (engine), and after the previous window
                if t["t"] < free:
                    continue
                rows.append({"signal": f.when(t["t"]), "entry_price": round(t["entry_price"], 2),
                             "closed": f.when(t["exit_index"]), "exit_price": round(t["exit_price"], 2),
                             "result_pct": round(100 * t["net_return"], 2), "how": t["exit_reason"],
                             "strategy": best["text"], "picked_at": f.when(c - 1)})
                free = t["exit_index"]
                took += 1
            # a signal too recent for a result (trade still running at the last candle)
            hold = int(best["exit"]["max_hold_bars"])
            for t in fired:
                if t >= free and t + hold > f.n - 1 and t + 1 < f.n:
                    rows.append({"signal": f.when(int(t)), "entry_price": round(float(f.o[t + 1]), 2),
                                 "closed": "-", "exit_price": "-", "result_pct": "open", "how": "still running",
                                 "strategy": best["text"], "picked_at": f.when(c - 1)})
                    free = f.n
                    took += 1
                    break
            self.log(f"replay {k + 1}/{len(cuts) - 1}: standing at {f.when(c - 1)} -> {best['text']}; "
                     f"{took} trade(s) until {f.when(nc - 1)} ({time.monotonic() - t0:.0f}s)")
        return rows, notes

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        f = self.f
        t0 = time.monotonic()
        if not f.load():
            return {"status": "no data"}
        self.log("today: learning from all candles (recent weigh more)")
        best, checked = self.build(f.n)
        root = f.du.root.parent / f"stock-tf-{self.tf}-{self.sym}-{f.side}"
        root.mkdir(parents=True, exist_ok=True)
        g = self.bc["gate"]
        rc = f.pc.get("recency") or {}
        md = [f"# Stock x TF strategy - {self.sym} {self.tf} {f.side}", "",
              f"Built {date.today()} by stock_tf. Move = {f.wtext}. Today ({f.when(f.n - 1)}): phase {PHASES[f.P]}. "
              f"Recent trades weigh more: weight (1 + age_sessions/{rc.get('decay_sessions', 100)})^-"
              f"{rc.get('power', 2)}. Gate on closed trades, recency-weighted: >= {g['min_trades']} trades, >= "
              f"{g.get('min_eff_trades', 0)} effective, weighted PF >= {g['min_pf']}, weighted avg > 0, DSL agreement "
              f">= {g['min_dsl_agreement']:.0%}. Real trade engine, costs in.", ""]
        cand = None
        if best:
            last = ", ".join(f"{f.when(t['t'])} {100 * t['net_return']:+.1f}%" for t in best["known"][-5:][::-1])
            md += ["## 1. Strategy for today", "", f"**{best['text']}**; exit "
                   + ", ".join(f"{k} {v}" for k, v in best["exit"].items()), "",
                   f"{_fmt(best['stats'])}. Pattern confidence {best['conf']} (streak {best['streak']}). "
                   f"Last 5 trades: {last}.", ""]
            ph = {1: "Bull", 0: "Side", -1: "Bear"}[best["phase"]]
            names = "".join(f.names[f.conds[c][0]].replace("_", " ").title().replace(" ", "")[:12]
                            for c in best["rule"])
            sid = f"{self.sym}-{self.tf}-{ph}-{names}"
            cand = {"id": sid, "name": f"{self.sym} {self.tf}: {best['text']}", "direction": f.side,
                    "description": f"Built {date.today()} by stock_tf. {_fmt(best['stats'])}.",
                    "entry": best["entry"], "exit": best["exit"], "enabled": True,
                    "applicability": {"symbols": [f"{self.sym}-EQ"], "timeframes": [self.tf]},
                    "tags": ["stock-specific", "paper-only", "stock_tf"],
                    "evidence_tuning": {"stats": best["stats"], "agree": best["agree"], "conf": best["conf"]}}
            (root / "candidates.json").write_text(json.dumps(
                {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": [cand],
                 "keep_original": []}, indent=1), encoding="utf-8")
        else:
            md += ["## 1. Strategy for today", "", "**None passed the gate** - no strategy for this stock x TF today.",
                   ""]
        table = ["| Pattern | Exit | Closed trades (plain, then recent-weighted) | DSL agree | Verdict |",
                 "|---|---|---|---|---|"]
        table += [f"| {r['text']} | {r['exit']['max_hold_bars']} candles | {_fmt(r['stats'])} | {r['agree']:.0%} | "
                  f"{'PASS' if r['passed'] else r['why']} |" for r in checked[:40]]
        months = self.a.replay_months if self.a.replay_months is not None else float(self.bc["replay"]["months"])
        rp: dict[str, Any] = {}
        if months > 0:
            self.log(f"causal replay of the last {months:g} months (refit every {self.bc['replay']['refit_sessions']} "
                     "sessions)")
            rows, notes = self.replay(months, int(self.bc["replay"]["refit_sessions"]))
            done = np.array([r["result_pct"] for r in rows if r["result_pct"] != "open"], dtype=float) / 100.0
            rp = _stats(done)
            md += [f"## 2. Replay: every trade the program would have asked you to take (last {months:g} months)", "",
                   "No split date: at each refit the program knew only trades closed before that moment, weighted "
                   "recent-first, picked the strategy exactly as for today, and took its signals until the next refit, "
                   "one position at a time. Signal = the candle that triggered (time = candle close, IST); you buy at "
                   "the next candle's open.", "",
                   f"**Result: {_fmt(rp, weighted=False)}**" + (f"; {sum(1 for r in rows if r['result_pct'] == 'open')}"
                                                                  " still running" if rows else ""), "",
                   "| Signal candle | Buy at (next open) | Closed | Exit price | Result | How | Strategy used |",
                   "|---|---|---|---|---|---|---|"]
            md += [f"| {r['signal']} | {r['entry_price']} | {r['closed']} | {r['exit_price']} | "
                   + (f"{r['result_pct']:+.2f}%" if r["result_pct"] != "open" else "open")
                   + f" | {r['how']} | {r['strategy']} |" for r in rows]
            if not rows:
                md.append("| (no trades) | | | | | | |")
            md += ["", "Strategy picked at each refit:", ""] + [f"- {x}" for x in notes] + [""]
            with (root / "replay_trades.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["signal", "entry_price", "closed", "exit_price", "result_pct", "how",
                                                   "strategy", "picked_at"])
                w.writeheader()
                w.writerows(rows)
        md += [f"## 3. Patterns checked for today ({len(checked)})", ""] + table
        (root / "BUILD.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"{self.sym} {self.tf}: " + (f"STRATEGY {best['text']} - {_fmt(best['stats'])}" if best
                                               else "no strategy passed")
                 + (f"; replay {_fmt(rp, weighted=False)}" if rp else "")
                 + f" ({time.monotonic() - t0:.0f}s); {root / 'BUILD.md'}")
        return {"status": "ok", "passed": bool(best), "strategy": cand,
                "result": _fmt(best["stats"]) if best else "", "text": best["text"] if best else "",
                "replay": _fmt(rp, weighted=False) if rp else "", "report": str(root / "BUILD.md"),
                "phase_today": PHASES[f.P] if best or checked else ""}


def main(argv: list[str] | None = None) -> int:
    from .walk_forward import read_stocks
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", help="one stock, e.g. RELIANCE")
    ap.add_argument("--stocks", help="N (first N of the universe) or @file")
    ap.add_argument("--tfs", default="15m")
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--replay-months", type=float, default=None, help="causal replay length (default config; 0 = off)")
    ap.add_argument("--redo", action="store_true", help="rebuild stock x TF already done")
    ap.add_argument("--patterns", default=str(PATTERNS_CFG))
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    if a.symbol:
        stocks = [a.symbol.upper().removesuffix("-EQ")]
    elif a.stocks:
        uni = read_stocks(a.stocks if a.stocks.startswith("@")
                          else "@" + str(REPO / "deploy" / "oracle" / "live" / "universe.txt"))
        stocks = uni if a.stocks.startswith("@") else uni[:int(a.stocks)]
    else:
        ap.error("give --symbol or --stocks")
    tfs = [t.strip() for t in a.tfs.split(",") if t.strip()]
    from .settings import load
    for tf in tfs:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        tdir = load(REPO / cfg["timeframes"][tf]["config"]).reports_dir / "tuning"
        tdir.mkdir(parents=True, exist_ok=True)
        state_p = tdir / "stock-tf-state.json"
        state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.is_file() else {}
        for k, sym in enumerate(stocks):
            key = f"{sym}|{tf}|{a.side}"
            if key in state and not a.redo:
                print(f"=== {sym} {tf} ({k + 1}/{len(stocks)}): done before - skipped (use --redo)", flush=True)
                continue
            print(f"=== {sym} {tf} ({k + 1}/{len(stocks)}) ===", flush=True)
            b = None
            try:
                b = Builder(sym, tf, a)
                res = b.run()
            except Exception as exc:  # noqa: BLE001 - one stock failing must not stop the run
                print(f"{sym} {tf} failed: {type(exc).__name__}: {exc}", flush=True)
                res = {"status": f"failed: {type(exc).__name__}: {exc}"[:300]}
            finally:
                if b is not None:
                    b.f.du.close()
            state[key] = {**res, "built": date.today().isoformat()}
            tmp = state_p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            tmp.replace(state_p)
        rows = [{"stock": k.split("|")[0], "tf": k.split("|")[1], "side": k.split("|")[2],
                 "status": v.get("status"), "passed": v.get("passed", False), "phase_today": v.get("phase_today", ""),
                 "strategy": v.get("text", ""), "result": v.get("result", ""), "replay": v.get("replay", ""),
                 "report": v.get("report", "")}
                for k, v in state.items()]
        with (tdir / "stock-tf-summary.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["stock"])
            w.writeheader()
            w.writerows(rows)
        cands = [v["strategy"] for v in state.values() if v.get("strategy")]
        (tdir / "stock-tf-candidates.json").write_text(json.dumps(
            {"contract": "stock-gplm.tuning-candidates.v1", "strategies": cands, "keep_original": []}, indent=1),
            encoding="utf-8")
        print(f"{tf}: {sum(1 for r in rows if r['passed'])} of {len(rows)} stock x TF have a strategy; "
              f"summary {tdir / 'stock-tf-summary.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
