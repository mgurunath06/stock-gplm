"""Check a family of readable pattern variants with the real trade engine (owner 2026-11-22).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.pattern_check --spec config/research/wakeup_15m.json --symbol RELIANCE
    (options: --stocks 200 | @file   also check every variant on other stocks' data; --workers 16)

The spec file holds the variants (DSL entry, no Python per strategy), the exits and the timeframe.
1. FOCUS STOCK: every trade of every variant x exit, with candle time, result and exit reason; win rate, average,
   profit factor, the streak counted back from today (worked trades in a row), last 3 trades.
2. OTHER STOCKS (with --stocks): the same variants on each stock - on how many stocks did it work? The more stocks
   and trades where it worked, the more confidence in the pattern itself.
3. candidates.json: the best variant x exit for the focus stock (library format) - NOT in the library until the owner
   approves; then paper-trade it unchanged.
Output: <reports>/tuning/pattern-check-<name>-<SYMBOL>/ REPORT.md, trades.csv, per_stock.csv, candidates.json.
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

from . import tune_eval as te
from .discover import FLOOR_DAILY, FLOOR_INTRADAY
from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]


def stats(net: np.ndarray) -> dict[str, Any]:
    n = len(net)
    if not n:
        return {"trades": 0, "win": 0.0, "avg": 0.0, "pf": 0.0, "total": 0.0}
    gl = -net[net <= 0].sum()
    return {"trades": n, "win": float((net > 0).mean()), "avg": float(net.mean()),
            "pf": float(net[net > 0].sum() / gl) if gl > 0 else 99.0, "total": float(net.sum())}


def fmt(s: dict[str, Any]) -> str:
    if not s["trades"]:
        return "no trades"
    return (f"{s['trades']} trades, win {s['win']:.0%}, avg {100 * s['avg']:+.2f}%, PF {min(s['pf'], 99):.2f}, "
            f"total {100 * s['total']:+.1f}%")


class Checker:
    def __init__(self, a: argparse.Namespace):
        from .settings import load
        self.a = a
        self.spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.tf = self.spec["tf"]
        self.t = cfg["timeframes"][self.tf]
        self.config = str(REPO / self.t["config"])
        self.sym = a.symbol.upper().removesuffix("-EQ")
        self.root = load(Path(self.config)).reports_dir / "tuning" / f"pattern-check-{self.spec['name']}-{self.sym}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.short = self.spec.get("direction", "long") == "short"
        self.floor = FLOOR_DAILY if self.tf == "1d" else FLOOR_INTRADAY
        self.combos = [(vn, en, f"{self.floor} and ({core})", ex)
                       for vn, core in self.spec["variants"].items() for en, ex in self.spec["exits"].items()]

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with (self.root / "progress.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ------------------------------------------------------------------ 1. focus stock, in process
    def focus(self) -> list[dict[str, Any]]:
        from . import strategy_dsl as dsl
        from .strategy_engine import Exit, simulate
        self.log(f"{self.sym} {self.tf}: loading candles")
        L = te._load(self.sym, self.config, [self.tf], bool(self.t["use_minute"])).get(self.tf)
        if L is None:
            self.log(f"no {self.tf} data for {self.sym}")
            return []
        m, ctx = L["m"], L["ctx"]
        n = len(m)
        a0 = int(np.searchsorted(L["ses"], self.t["tune_start"]))
        end_ns = np.asarray(m.end_ns, dtype=np.int64)

        def when(i: int) -> str:
            if not m.intraday:
                return str(np.datetime64(int(L["days"][i]), "D"))
            ts = np.datetime64(int(end_ns[i]), "ns") + np.timedelta64(330, "m")
            return str(ts.astype("datetime64[m]")).replace("T", " ")

        out = []
        for vn, en, entry, ex in self.combos:
            try:
                mask = np.asarray(dsl.signal(entry, ctx), bool)
            except Exception as exc:  # noqa: BLE001 - a bad expression is reported, not fatal
                self.log(f"  {vn}: expression error {type(exc).__name__}: {exc}")
                continue
            trades = simulate(m, mask, (a0, n), Exit.of(ex), L["buy"], L["sell"], self.short)
            net = np.array([x["net_return"] for x in trades], dtype=float)
            rows = [{"variant": vn, "exit": en, "signal": when(x["t"]), "closed": when(x["exit_index"]),
                     "entry": round(x["entry_price"], 2), "net_pct": round(100 * x["net_return"], 2),
                     "reason": x["exit_reason"], "bars": x["bars_held"]} for x in trades]
            won = net > 0
            fails = np.flatnonzero(~won)
            streak = int(len(won) - (fails[-1] + 1 if len(fails) else 0))
            out.append({"variant": vn, "exit": en, "entry": entry, "exit_doc": ex, "stats": stats(net),
                        "streak": streak, "rows": rows})
            self.log(f"  {vn} / {en}: {fmt(stats(net))}; streak back from today {streak}")
        return out

    # ------------------------------------------------------------------ 2. other stocks, worker pool
    def others(self) -> dict[str, list[dict[str, Any]]]:
        from .walk_forward import read_stocks
        s = self.a.stocks
        uni = read_stocks(s if s.startswith("@") else "@" + str(REPO / "deploy" / "oracle" / "live" / "universe.txt"))
        stocks = uni if s.startswith("@") else uni[:int(s)]
        self.log(f"other stocks: {len(stocks)} on {self.a.workers} workers (first call loads data)")
        pool = te.EvalPool(stocks, self.config, [self.tf], self.a.workers, bool(self.t["use_minute"]))
        try:
            vs = [te.Variant(f"{vn}~{en}", self.tf, entry, ex, "short" if self.short else "long")
                  for vn, en, entry, ex in self.combos]
            t0 = time.monotonic()
            res, errors = pool.evaluate(vs, self.t["tune_start"])
            self.log(f"evaluated {len(vs)} variants x {len(stocks)} stocks in {time.monotonic() - t0:.0f}s"
                     + (f" ({len(pool.failed)} stocks failed to load)" if pool.failed else ""))
        finally:
            pool.close()
        mn = int(self.spec.get("min_trades_per_stock", 5))
        out: dict[str, list[dict[str, Any]]] = {}
        for v in vs:
            r = res.get(v.vid)
            rows = []
            if r is not None:
                for si in np.unique(r["stock"]):
                    net = r["net"][r["stock"] == si].astype(float)
                    rows.append({"stock": stocks[int(si)], **stats(net), "enough": len(net) >= mn})
            out[v.vid] = rows
        return out

    # ------------------------------------------------------------------ report
    def run(self) -> int:
        foc = self.focus()
        oth = self.others() if self.a.stocks else {}
        mn = int(self.spec.get("min_trades_per_stock", 5))
        md = [f"# Pattern check - {self.spec['name']} - {self.sym} {self.tf} {self.spec.get('direction', 'long')}", "",
              self.spec.get("_doc", ""), "",
              f"## 1. {self.sym}: every variant (real trade engine, costs included)", "",
              "| Variant | Exit | Result | Streak back from today | Last 3 trades (newest first) |", "|---|---|---|---|---|"]
        for f in sorted(foc, key=lambda x: -x["stats"]["total"]):
            last = ", ".join(f"{r['signal']} {r['net_pct']:+.1f}%" for r in f["rows"][-3:][::-1]) or "-"
            md.append(f"| {f['variant']} | {f['exit']} | {fmt(f['stats'])} | {f['streak']} | {last} |")
        md.append("")
        if oth:
            md += [f"## 2. Other stocks ({self.a.stocks}): on how many stocks did each variant work?", "",
                   f"Stocks with >= {mn} trades count. 'Worked' = total net > 0 on that stock.", "",
                   "| Variant | Exit | Stocks counted | Worked on | All trades pooled | Best stocks (total) |",
                   "|---|---|---|---|---|---|"]
            for vid, rows in oth.items():
                ok = [r for r in rows if r["enough"]]
                good = [r for r in ok if r["total"] > 0]
                pooled_n = sum(r["trades"] for r in rows)
                pooled_tot = sum(r["total"] for r in rows)
                pooled_win = sum(r["win"] * r["trades"] for r in rows) / pooled_n if pooled_n else 0.0
                best = ", ".join(f"{r['stock']} {100 * r['total']:+.0f}%" for r in
                                 sorted(ok, key=lambda r: -r["total"])[:5])
                vn, en = vid.split("~")
                md.append(f"| {vn} | {en} | {len(ok)} | {len(good)} ({len(good) / max(len(ok), 1):.0%}) | "
                          f"{pooled_n} trades, win {pooled_win:.0%}, avg {100 * pooled_tot / max(pooled_n, 1):+.2f}% | "
                          f"{best} |")
            md.append("")
            with (self.root / "per_stock.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["variant", "exit", "stock", "trades", "win", "avg_pct", "pf", "total_pct"])
                for vid, rows in oth.items():
                    vn, en = vid.split("~")
                    for r in rows:
                        w.writerow([vn, en, r["stock"], r["trades"], round(r["win"], 3), round(100 * r["avg"], 3),
                                    round(min(r["pf"], 99), 3), round(100 * r["total"], 2)])
        with (self.root / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["variant", "exit", "signal", "closed", "entry", "net_pct", "reason",
                                               "bars"])
            w.writeheader()
            for f in foc:
                w.writerows(f["rows"])
        best = max((f for f in foc if f["stats"]["trades"] >= mn and f["stats"]["pf"] > 1),
                   key=lambda f: f["stats"]["total"], default=None)
        if best:
            sid = f"{self.sym}-{self.tf}-{best['variant']}-{best['exit']}"
            md += ["## 3. Candidate", "", f"**{sid}**: {fmt(best['stats'])}. Trades of every variant are in "
                   "trades.csv (check them on the chart). candidates.json is NOT in the library until you approve.", ""]
            doc = {"id": sid, "name": f"{self.sym} {self.tf}: {best['variant']} ({best['exit']})",
                   "direction": self.spec.get("direction", "long"),
                   "description": f"{self.spec['name']} pattern for {self.sym} {self.tf}, checked {date.today()} "
                                  f"by pattern_check.",
                   "entry": best["entry"], "exit": best["exit_doc"], "enabled": True,
                   "applicability": {"symbols": [f"{self.sym}-EQ"], "timeframes": [self.tf]},
                   "tags": ["stock-specific", "discovered", self.spec["name"]],
                   "evidence_tuning": {"focus": best["stats"], "streak": best["streak"]}}
            (self.root / "candidates.json").write_text(json.dumps(
                {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": [doc],
                 "keep_original": []}, indent=1), encoding="utf-8")
        else:
            md += ["## 3. Candidate", "", f"No variant had >= {mn} trades with profit factor > 1 on {self.sym}.", ""]
        (self.root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"report: {self.root / 'REPORT.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--stocks", default="", help="also check other stocks: N (first N of the universe) or @file")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--config", default=str(CONFIG))
    return Checker(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
