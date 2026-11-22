"""Fine-tune a strategy per stock x timeframe, by itself (owner 2026-11-22: "the program does it, not me").

    $env:PYTHONPATH="src"; python -m stock_gplm.app.stock_tf --symbol HDFCBANK --tfs 15m
    (options: --stocks 200 | @file instead of --symbol; --tfs 1d,60m,15m,5m; --redo; --side long|short)

Per stock x TF, with no questions asked:
1. phase_patterns: phases (EMA 9/13/20/50 stack), every move (intraday +2% before -1%, held overnight; 1d +1 ATR
   before -0.5 ATR), the -10..+10 candles around move starts, and the patterns that worked per phase.
2. Turn the best patterns of every phase into readable strategies (DSL: phase + conditions, same exits).
3. Run each through the REAL trade engine on that stock (costs, gaps, one position at a time); check the DSL matches
   the research signals (agreement).
4. Gate (config/research/phase_patterns.json -> builder.gate): enough trades, profit factor, average > 0,
   profitable in BOTH halves of the data. The best that passes = that stock x TF's strategy.
Output <reports>/tuning/stock-tf-<tf>-<SYM>-<side>/BUILD.md, trades.csv, candidates.json; one summary row per stock x
TF in <reports>/tuning/stock-tf-summary.csv; all passing strategies in stock-tf-candidates.json (approve -> library
config/strategies/library/owner/stock-specific.json, paper first). Checkpointed: a finished stock x TF is skipped
unless --redo.
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


def _stats(net: np.ndarray) -> dict[str, Any]:
    n = len(net)
    if not n:
        return {"trades": 0, "win": 0.0, "avg": 0.0, "pf": 0.0, "total": 0.0}
    gl = -net[net <= 0].sum()
    return {"trades": int(n), "win": float((net > 0).mean()), "avg": float(net.mean()),
            "pf": float(net[net > 0].sum() / gl) if gl > 0 else 99.0, "total": float(net.sum())}


def _fmt(s: dict[str, Any]) -> str:
    return (f"{s['trades']} trades, win {s['win']:.0%}, avg {100 * s['avg']:+.2f}%, PF {min(s['pf'], 99):.2f}, "
            f"total {100 * s['total']:+.1f}%") if s["trades"] else "no trades"


class Builder:
    def __init__(self, sym: str, tf: str, a: argparse.Namespace):
        self.sym, self.tf, self.a = sym.upper().removesuffix("-EQ"), tf, a
        self.f = Finder(argparse.Namespace(tf=tf, symbol=self.sym, side=a.side, patterns=a.patterns, config=a.config))
        self.bc = self.f.pc["builder"]
        self.log = self.f.log

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

    # ------------------------------------------------------------------ pick + check
    def picks(self) -> list[tuple[int, tuple[int, ...], dict]]:
        f, bc = self.f, self.bc
        out = []
        for q, lst in f.res.items():
            keep = []
            for rule, x in lst:
                n = len(x["times"])
                base = f.base[q][x["e"]]
                rate = x["all_won"] / n if n else 0.0
                if n >= bc["min_times"] and x["avg"] > 0 and base > 0 and rate >= bc["min_x_base"] * base:
                    keep.append((rule, x))
            keep.sort(key=lambda rx: -rx[1]["avg"] * np.sqrt(len(rx[1]["times"])))
            out += [(q, r, x) for r, x in f.distinct(keep, int(bc["candidates_per_phase"]))]
        return out

    def check(self, q: int, rule: tuple[int, ...], x: dict) -> dict[str, Any] | None:
        from . import strategy_dsl as dsl
        from .strategy_engine import Exit, simulate
        f = self.f
        L = f.L
        m, ctx = L["m"], L["ctx"]
        n, a0 = f.n, f.a0
        research = f.ok & (f.phase == q) & np.logical_and.reduce(f.M[list(rule)])
        for alt in (False, True):
            core = " and ".join([self.phase_dsl(q)] + [self.cond_dsl(c, alt) for c in rule])
            entry = f"{f.du.floor} and {core}"
            try:
                mask = np.asarray(dsl.signal(entry, ctx), bool)
                break
            except Exception:  # noqa: BLE001 - try the plain-boolean form, then give up on this pattern
                mask = None
        if mask is None:
            return None
        rs = research[a0:].sum()
        agree = float((mask & research)[a0:].sum() / rs) if rs else 0.0
        ex = self.exit_doc(x["e"])
        trades = simulate(m, mask, (a0, n), Exit.of(ex), L["buy"], L["sell"], f.short)
        net = np.array([t["net_return"] for t in trades], dtype=float)
        ts = np.array([t["t"] for t in trades], dtype=np.int64)
        mid = a0 + (n - a0) // 2
        h1, h2 = float(net[ts < mid].sum()), float(net[ts >= mid].sum())
        s = _stats(net)
        g = self.bc["gate"]
        why = []
        if s["trades"] < g["min_trades"]:
            why.append(f"trades {s['trades']} < {g['min_trades']}")
        if s["pf"] < g["min_pf"]:
            why.append(f"PF {s['pf']:.2f} < {g['min_pf']}")
        if s["avg"] <= g["min_avg"]:
            why.append("avg <= 0")
        if g.get("both_halves_profitable") and not (h1 > 0 and h2 > 0):
            why.append(f"halves {100 * h1:+.1f}% / {100 * h2:+.1f}%")
        if agree < g["min_dsl_agreement"]:
            why.append(f"DSL agreement {agree:.0%}")
        return {"phase": q, "rule": rule, "text": f"[{PHASES[q]}] " + f.rtext(rule), "entry": entry, "exit": ex,
                "stats": s, "h1": h1, "h2": h2, "agree": agree, "passed": not why, "why": "; ".join(why),
                "trades": trades, "conf": x["conf"], "streak": x["streak"]}

    # ------------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        f = self.f
        t0 = time.monotonic()
        if f.run() != 0:
            return {"status": "no data"}
        cands = self.picks()
        self.log(f"builder: {len(cands)} patterns -> real trade engine")
        checked = []
        for i, (q, rule, x) in enumerate(cands):
            r = self.check(q, rule, x)
            if r:
                checked.append(r)
            if (i + 1) % 20 == 0:
                self.log(f"  checked {i + 1}/{len(cands)}; passed so far {sum(c['passed'] for c in checked)}")
        checked.sort(key=lambda r: (r["passed"], r["stats"]["total"]), reverse=True)
        best = checked[0] if checked and checked[0]["passed"] else None
        root = f.du.root.parent / f"stock-tf-{self.tf}-{self.sym}-{f.side}"
        root.mkdir(parents=True, exist_ok=True)
        g = self.bc["gate"]
        md = [f"# Stock x TF strategy - {self.sym} {self.tf} {f.side}", "",
              f"Built {date.today()} by stock_tf from {f.du.root / 'PATTERNS.md'}. Move = {f.wtext}. "
              f"Today: phase {PHASES[f.P]}. Gate: >= {g['min_trades']} trades, PF >= {g['min_pf']}, avg > 0, "
              f"profitable in both halves, DSL agreement >= {g['min_dsl_agreement']:.0%}. Real trade engine, costs in.",
              ""]
        if best:
            last = ", ".join(f"{f.when(t['t'])} {100 * t['net_return']:+.1f}%" for t in best["trades"][-5:][::-1])
            md += ["## Strategy", "", f"**{best['text']}**; exit "
                   + ", ".join(f"{k} {v}" for k, v in best["exit"].items()), "",
                   f"{_fmt(best['stats'])}; halves {100 * best['h1']:+.1f}% / {100 * best['h2']:+.1f}%; "
                   f"pattern confidence {best['conf']} (streak {best['streak']}). Last 5 trades: {last}.", ""]
        else:
            md += ["## Strategy", "", "**None passed the gate** - no strategy for this stock x TF yet.", ""]
        md += [f"## Patterns checked ({len(checked)})", "",
               "| Pattern | Exit | Result | Halves | DSL agree | Verdict |", "|---|---|---|---|---|---|"]
        for r in checked[:40]:
            md.append(f"| {r['text']} | {r['exit']['max_hold_bars']} candles | {_fmt(r['stats'])} | "
                      f"{100 * r['h1']:+.1f}% / {100 * r['h2']:+.1f}% | {r['agree']:.0%} | "
                      f"{'PASS' if r['passed'] else r['why']} |")
        (root / "BUILD.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        cand = None
        if best:
            ph = {1: "Bull", 0: "Side", -1: "Bear"}[best["phase"]]
            names = "".join(f.names[f.conds[c][0]].replace("_", " ").title().replace(" ", "")[:12]
                            for c in best["rule"])
            sid = f"{self.sym}-{self.tf}-{ph}-{names}"
            cand = {"id": sid, "name": f"{self.sym} {self.tf}: {best['text']}", "direction": f.side,
                    "description": f"Built {date.today()} by stock_tf. {_fmt(best['stats'])}; halves "
                                   f"{100 * best['h1']:+.1f}% / {100 * best['h2']:+.1f}%.",
                    "entry": best["entry"], "exit": best["exit"], "enabled": True,
                    "applicability": {"symbols": [f"{self.sym}-EQ"], "timeframes": [self.tf]},
                    "tags": ["stock-specific", "paper-only", "stock_tf"],
                    "evidence_tuning": {"stats": best["stats"], "h1": best["h1"], "h2": best["h2"],
                                        "agree": best["agree"], "conf": best["conf"]}}
            (root / "candidates.json").write_text(json.dumps(
                {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": [cand],
                 "keep_original": []}, indent=1), encoding="utf-8")
            with (root / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["signal", "closed", "entry", "net_pct", "reason", "bars"])
                for t in best["trades"]:
                    w.writerow([f.when(t["t"]), f.when(t["exit_index"]), round(t["entry_price"], 2),
                                round(100 * t["net_return"], 2), t["exit_reason"], t["bars_held"]])
        self.log(f"{self.sym} {self.tf}: " + (f"STRATEGY {best['text']} - {_fmt(best['stats'])}" if best
                                               else "no strategy passed") + f" ({time.monotonic() - t0:.0f}s); "
                 f"{root / 'BUILD.md'}")
        return {"status": "ok", "passed": bool(best), "strategy": cand,
                "result": _fmt(best["stats"]) if best else "", "text": best["text"] if best else "",
                "report": str(root / "BUILD.md"), "phase_today": PHASES[f.P], "checked": len(checked)}


def main(argv: list[str] | None = None) -> int:
    from .walk_forward import read_stocks
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", help="one stock, e.g. HDFCBANK")
    ap.add_argument("--stocks", help="N (first N of the universe) or @file")
    ap.add_argument("--tfs", default="15m")
    ap.add_argument("--side", choices=["long", "short"], default="long")
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
                 "strategy": v.get("text", ""), "result": v.get("result", ""), "report": v.get("report", "")}
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
