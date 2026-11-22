"""One solid strategy for ONE stock (owner 2026-11-22: no generic strategies - RELIANCE first, then per stock).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.stock_strategy --tf 1d --symbol RELIANCE
    (options: --side long|short; --holds 5,10,20 --targets 2,3,4 --stops 1,1.5,2 (exit grid, ATR(14) multiples);
              --first-test-year 2016; --min-trades 25; --half-life 3 (years); --beam 40)

1. Label every liquid bar of the stock under each exit in the grid (enter next open; target T x ATR / stop S x ATR /
   time exit after H bars; same bar -> stop first; costs included).
2. Entry conditions: every feature (config/research/move_features.json) above / below its 20/40/60/80% level, levels
   taken from the years BEFORE the first test year only; yes/no features as true / false.
3. Search (beam): best single conditions -> pairs of the best singles -> triples, each with every exit. Each rule is
   traded one position at a time (no overlapping trades) and scored by year:
   score = cautious average net per trade = recency-weighted average (half-life in years) - 1 standard error;
   it also needs >= min trades, trades in >= 4 years, and >= 60% of its years profitable.
4. WALK-FORWARD (the honest check): for each test year Y, the rule + exit is picked using only years before Y and
   traded unchanged in Y. The year-by-year record says whether this method really works on this stock.
5. Final strategy = the pick on ALL years (no date lock), re-run through the real trade engine, written as a
   library-format strategy.json - NOT in the library until the owner approves; then paper-trade it unchanged.

Outputs <reports>/tuning/stock-strategy-<tf>-<SYMBOL>-<side>/: REPORT.md, strategy.json, walk_forward.csv,
trades.csv (final strategy, real engine). One stock takes a few minutes; nothing is cached between runs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from . import tune_eval as te
from .discover import Discoverer
from .tune import CONFIG

QS = (0.2, 0.4, 0.6, 0.8)
Cond = tuple[int, str, float]  # (feature index, "ge" | "lt" | "yes" | "no", level)


def _date(d: int) -> str:
    return str(np.datetime64(int(d), "D"))


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def _camel(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in s.replace("-", "_").split("_") if p)


class Builder:
    def __init__(self, a: argparse.Namespace):
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.a = a
        self.sym = a.symbol.upper().removesuffix("-EQ")
        tmp = Path(tempfile.gettempdir()) / f"stock_strategy_{self.sym}.txt"
        tmp.write_text(self.sym + "\n", encoding="utf-8")
        self.du = Discoverer(a.tf, f"stock-strategy-{a.tf}-{self.sym}-{a.side}", cfg, "@" + str(tmp), 1)
        self.tf, self.side, self.log = a.tf, a.side, self.du.log
        self.names = te.feature_names(self.du.intraday)
        self.exprs = te.feature_exprs(self.du.intraday)
        self.exits = [(int(h), t, s) for h in _floats(a.holds) for t in _floats(a.targets) for s in _floats(a.stops)]
        self.cache: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        hmin = min(e[0] for e in self.exits)
        fk = next(k for k, e in enumerate(self.exits) if e[0] == hmin)
        vs = [te.Variant(f"SS~{k}", self.tf, self.du.floor, {"max_hold_bars": h, "target_atr": t, "stop_atr": s},
                         self.side, k == fk, {"h": h, "t": t, "s": s}) for k, (h, t, s) in enumerate(self.exits)]
        self.log(f"{self.sym} {self.tf} {self.side}: labelling every liquid bar under {len(vs)} exits "
                 f"(hold {self.a.holds} bars x target {self.a.targets} ATR x stop {self.a.stops} ATR)")
        res = self.du.evaluate(vs)
        r0 = res[vs[fk].vid].get("raw")
        if r0 is None or "feat" not in r0 or len(r0["net"]) < 300:
            self.log(f"not enough data for {self.sym} (did it pass the liquidity floor?)")
            return False
        o = np.argsort(r0["bar"], kind="stable")
        self.bar = r0["bar"][o].astype(np.int64)
        self.day = r0["day"][o].astype(np.int64)
        self.feat = r0["feat"][o].astype(float)
        self.year = te.period_of(self.day, "year") + 1970
        self.y0, self.y1 = int(self.year.min()), int(self.year.max())
        self.yi = self.year - self.y0
        self.NY = self.y1 - self.y0 + 1
        n, E = len(self.bar), len(self.exits)
        self.net = np.full((E, n), np.nan)
        self.held = np.ones((E, n), np.int64)
        for k, v in enumerate(vs):
            r = res[v.vid].get("raw")
            if r is None:
                continue
            rb = r["bar"].astype(np.int64)
            pos = np.minimum(np.searchsorted(self.bar, rb), n - 1)
            ok = self.bar[pos] == rb
            self.net[k, pos[ok]] = r["net"][ok]
            self.held[k, pos[ok]] = np.maximum(r["held"][ok], 1)
        self.log(f"{n:,} bars {_date(self.day[0])} .. {_date(self.day[-1])}; years {self.y0}-{self.y1}")
        return True

    def build_conditions(self) -> None:
        base = self.year < self.a.first_test_year
        conds: list[Cond] = []
        for j in range(len(self.names)):
            x = self.feat[base, j]
            x = x[np.isfinite(x)]
            if len(x) < 100:
                continue
            u = np.unique(x)
            if len(u) < 2:
                continue
            if np.isin(u, (0.0, 1.0)).all():
                conds += [(j, "yes", 1.0), (j, "no", 0.0)]
                continue
            for e in np.unique([float(f"{q:.4g}") for q in np.quantile(x, QS)]):
                if e > u[0]:
                    conds += [(j, "ge", float(e)), (j, "lt", float(e))]
        self.conds = conds
        F = self.feat
        M = np.zeros((len(conds), len(self.bar)), bool)
        for c, (j, kind, lv) in enumerate(conds):
            x = F[:, j]
            fin = np.isfinite(x)
            M[c] = fin & ((x >= lv) if kind == "ge" else (x < lv) if kind == "lt" else (x > 0.5) if kind == "yes"
                          else (x <= 0.5))
        self.M = M
        self.log(f"{len(conds)} entry conditions from {len(self.names)} features (levels from years before "
                 f"{self.a.first_test_year})")

    # ------------------------------------------------------------------ text
    def ctext(self, c: Cond) -> str:
        j, kind, lv = c
        nm = self.names[j]
        return {"ge": f"{nm} >= {lv:g}", "lt": f"{nm} < {lv:g}", "yes": nm, "no": f"not {nm}"}[kind]

    def cdsl(self, c: Cond) -> str:
        j, kind, lv = c
        ex = self.exprs[self.names[j]]
        return {"ge": f"({ex}) >= {lv:g}", "lt": f"({ex}) < {lv:g}", "yes": f"({ex})", "no": f"not ({ex})"}[kind]

    def rtext(self, rule: tuple[int, ...]) -> str:
        return " AND ".join(self.ctext(self.conds[c]) for c in rule)

    def etext(self, e: int) -> str:
        h, t, s = self.exits[e]
        return f"target {t:g} ATR / stop {s:g} ATR / max {h} bars"

    # ------------------------------------------------------------------ trades
    def trades(self, rule: tuple[int, ...], e: int) -> tuple[np.ndarray, np.ndarray]:
        """(per-year aggregates [count, sum, sumsq, wins] x NY, row indices of the trades) - one position at a time."""
        key = (rule, e)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        mask = np.logical_and.reduce(self.M[list(rule)]) & np.isfinite(self.net[e])
        sig = np.flatnonzero(mask)
        out: list[int] = []
        if len(sig):
            b, held = self.bar[sig], self.held[e]
            i = 0
            while i < len(sig):
                r = sig[i]
                out.append(r)
                i = int(np.searchsorted(b, b[i] + held[r], "left"))
        idx = np.asarray(out, np.int64)
        nt, yi = self.net[e, idx], self.yi[idx]
        agg = np.stack([np.bincount(yi, minlength=self.NY).astype(float),
                        np.bincount(yi, weights=nt, minlength=self.NY),
                        np.bincount(yi, weights=nt * nt, minlength=self.NY),
                        np.bincount(yi, weights=(nt > 0).astype(float), minlength=self.NY)])
        self.cache[key] = (agg, idx)
        return agg, idx

    def score(self, agg: np.ndarray, w: np.ndarray, strict: bool = True) -> tuple[float, dict[str, Any]] | None:
        """Cautious avg/trade on the years with weight > 0. strict: also the trade / year / profitable-year rules."""
        use = w > 0
        cnt, sm, sq, wins = agg[0] * use, agg[1] * use, agg[2] * use, agg[3] * use
        n = float(cnt.sum())
        if n < (self.a.min_trades if strict else 2 * self.a.min_trades):
            return None
        yrs = cnt > 0
        mean = float(sm.sum() / n)
        se = math.sqrt(max(float(sq.sum() / n) - mean * mean, 1e-12) / n)
        avg_w = float((w * sm).sum() / (w * cnt).sum())
        pos = float(((sm > 0) & yrs).sum() / max(yrs.sum(), 1))
        if strict and (yrs.sum() < 4 or pos < 0.6 or avg_w <= 0):
            return None
        return avg_w - se, {"trades": int(n), "win": float(wins.sum() / n), "avg": mean, "avg_w": avg_w,
                            "years": int(yrs.sum()), "years_pos": pos}

    def best_exit(self, rule: tuple[int, ...], w: np.ndarray, strict: bool = True):
        best = None
        for e in range(len(self.exits)):
            s = self.score(self.trades(rule, e)[0], w, strict)
            if s and (best is None or s[0] > best[0]):
                best = (s[0], rule, e, s[1])
        return best

    def weights(self, end_year: int) -> np.ndarray:
        """Year weights for a pick made at the start of end_year: years before it, half-life in years."""
        ys = np.arange(self.y0, self.y1 + 1)
        return np.where(ys < end_year, 0.5 ** ((end_year - 1 - ys) / self.a.half_life), 0.0)

    def select(self, end_year: int, top: int = 0) -> list[tuple]:
        w = self.weights(end_year)
        singles = [b for c in range(len(self.conds)) if (b := self.best_exit((c,), w, strict=False))]
        singles.sort(key=lambda r: -r[0])
        beam = [r[1][0] for r in singles[:self.a.beam]]
        found = [b for c in beam if (b := self.best_exit((c,), w))]
        pairs = []
        for i, c1 in enumerate(beam):
            for c2 in beam[i + 1:]:
                if self.conds[c1][0] == self.conds[c2][0]:
                    continue
                b = self.best_exit(tuple(sorted((c1, c2))), w)
                if b:
                    pairs.append(b)
        pairs.sort(key=lambda r: -r[0])
        found += pairs
        seen = set()
        for p in pairs[:15]:
            used = {self.conds[c][0] for c in p[1]}
            for c in beam:
                if self.conds[c][0] in used:
                    continue
                rule = tuple(sorted((*p[1], c)))
                if rule in seen:
                    continue
                seen.add(rule)
                b = self.best_exit(rule, w)
                if b:
                    found.append(b)
        found.sort(key=lambda r: -r[0])
        return found[:max(top, 1)]

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if not self.load():
            return 1
        self.build_conditions()
        root = self.du.root
        a = self.a
        wf_rows, wf_trades = [], []
        t0 = time.monotonic()
        test_years = list(range(max(a.first_test_year, self.y0 + 4), self.y1 + 1))
        for k, Y in enumerate(test_years):
            pick = self.select(Y)
            if not pick:
                self.log(f"walk-forward {Y} ({k + 1}/{len(test_years)}): no rule passed on years before {Y} - no trades")
                wf_rows.append({"year": Y, "rule": "-", "exit": "-", "trades": 0, "win": 0.0, "avg": 0.0, "sum": 0.0})
                continue
            sc, rule, e, info = pick[0]
            _, idx = self.trades(rule, e)
            ty = idx[self.year[idx] == Y]
            nt = self.net[e, ty]
            wf_trades += [(Y, int(self.day[r]), float(self.net[e, r])) for r in ty]
            wf_rows.append({"year": Y, "rule": self.rtext(rule), "exit": self.etext(e), "trades": len(nt),
                            "win": float((nt > 0).mean()) if len(nt) else 0.0,
                            "avg": float(nt.mean()) if len(nt) else 0.0, "sum": float(nt.sum())})
            self.log(f"walk-forward {Y} ({k + 1}/{len(test_years)}): picked [{self.rtext(rule)}] {self.etext(e)} "
                     f"(cautious {100 * sc:+.2f}%/trade before {Y}) -> {Y}: {len(nt)} trades, "
                     f"sum {100 * nt.sum():+.1f}% ({time.monotonic() - t0:.0f}s, {len(self.cache):,} rules tried)")
        self.log("final pick on all years")
        final = self.select(self.y1 + 1, top=10)
        md = self.report_head(wf_rows, wf_trades)
        if final:
            md += self.final_section(final)
        else:
            md += ["## Final strategy", "", "No rule passed the rules on all years - no strategy for this stock.", ""]
        with (root / "walk_forward.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(wf_rows[0]) if wf_rows else ["year"])
            w.writeheader()
            w.writerows(wf_rows)
        (root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"report: {root / 'REPORT.md'}")
        return 0

    def report_head(self, rows: list[dict], trades: list[tuple]) -> list[str]:
        a = self.a
        nets = np.array([t[2] for t in trades])
        md = [f"# One-stock strategy - {self.sym} {self.tf} {self.side}", "",
              f"Data {_date(self.day[0])} .. {_date(self.day[-1])}. Exits tried: {len(self.exits)} (target/stop in "
              f"ATR(14), max hold in bars; costs included; same bar -> stop first). Entry conditions: "
              f"{len(self.conds)}. Rules tried: {len(self.cache):,}. Score = recency-weighted avg net per trade "
              f"(half-life {a.half_life:g} years) minus 1 standard error; needs >= {a.min_trades} trades, trades in "
              f">= 4 years, >= 60% of years profitable. One position at a time.", "",
              "## 1. Walk-forward (honest): each year traded with the rule picked on the years before it", ""]
        if len(nets):
            gl = -nets[nets <= 0].sum()
            yp = sum(1 for r in rows if r["sum"] > 0)
            yt = sum(1 for r in rows if r["trades"])
            md += [f"**{len(nets)} trades, win {(nets > 0).mean():.0%}, avg {100 * nets.mean():+.2f}%/trade, PF "
                   f"{(nets[nets > 0].sum() / gl) if gl > 0 else 99:.2f}, total {100 * nets.sum():+.1f}% (sum of "
                   f"trade returns), {yp} of {yt} traded years profitable.**", ""]
        else:
            md += ["**No walk-forward trades.**", ""]
        md += ["| Year | Rule picked (on years before) | Exit | Trades | Win | Avg | Sum |", "|---|---|---|---|---|---|---|"]
        md += [f"| {r['year']} | {r['rule']} | {r['exit']} | {r['trades']} | {r['win']:.0%} | {100 * r['avg']:+.2f}% | "
               f"{100 * r['sum']:+.1f}% |" for r in rows]
        md += ["", "If the picked rule changes every year, the stock has no stable pattern under these features - "
                   "trust the walk-forward total, not the final rule's own history.", ""]
        return md

    def final_section(self, final: list[tuple]) -> list[str]:
        sc, rule, e, info = final[0]
        h, t, s = self.exits[e]
        core = " and ".join(self.cdsl(self.conds[c]) for c in rule)
        entry = f"{self.du.floor} and {core}"
        ex = {"max_hold_bars": h, "target_atr": t, "stop_atr": s}
        sid = f"STK-{self.sym}-{self.tf}-" + "".join(
            _camel(self.names[self.conds[c][0]]) + ("Hi" if self.conds[c][1] in ("ge", "yes") else "Lo") for c in rule)
        self.log(f"final: [{self.rtext(rule)}] {self.etext(e)} - checking with the real trade engine")
        v = te.Variant(f"{sid}~ENG", self.tf, entry, ex, self.side)
        raw = self.du.evaluate([v])[v.vid].get("raw")
        md = ["## 2. Final strategy (picked on ALL years - for live/paper; its own history below is in-sample)", "",
              f"**{sid}**: {self.rtext(rule)}; {self.etext(e)}; {self.side}.", "",
              f"Search result: {info['trades']} trades, win {info['win']:.0%}, avg {100 * info['avg']:+.2f}%, "
              f"recency-weighted avg {100 * info['avg_w']:+.2f}%, cautious {100 * sc:+.2f}%/trade, "
              f"{info['years_pos']:.0%} of {info['years']} years profitable.", ""]
        eng: dict[str, Any] = {}
        if raw is not None and len(raw["net"]):
            net, day = raw["net"].astype(float), raw["day"].astype(np.int64)
            yr = te.period_of(day, "year") + 1970
            gl = -net[net <= 0].sum()
            eng = {"trades": int(len(net)), "win": round(float((net > 0).mean()), 4), "avg": round(float(net.mean()), 5),
                   "pf": round(float(net[net > 0].sum() / gl) if gl > 0 else 99.0, 3)}
            md += [f"Real trade engine (gaps, exits as live): {eng['trades']} trades, win {eng['win']:.0%}, avg "
                   f"{100 * eng['avg']:+.2f}%, PF {eng['pf']:.2f}.", "", "| Year | Trades | Win | Avg | Sum |",
                   "|---|---|---|---|---|"]
            for y in np.unique(yr):
                m = yr == y
                md.append(f"| {y} | {int(m.sum())} | {(net[m] > 0).mean():.0%} | {100 * net[m].mean():+.2f}% | "
                          f"{100 * net[m].sum():+.1f}% |")
            with (self.du.root / "trades.csv").open("w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["signal_date", "net_pct", "bars_held", "exit"])
                for d, x, hb, rs in zip(day, net, raw["held"], raw["reason"]):
                    wr.writerow([_date(d), round(100 * x, 3), int(hb), te.EXIT_NAMES.get(int(rs), "other")])
        else:
            md.append("Real trade engine: no trades (check the entry expression).")
        md += ["", "Runners-up (all years):", "", "| Rule | Exit | Trades | Win | Avg | Cautious | Years + |",
               "|---|---|---|---|---|---|---|"]
        md += [f"| {self.rtext(r)} | {self.etext(x)} | {i['trades']} | {i['win']:.0%} | {100 * i['avg']:+.2f}% | "
               f"{100 * c:+.2f}% | {i['years_pos']:.0%} of {i['years']} |" for c, r, x, i in final[1:]]
        doc = {"id": sid, "name": f"{self.sym}: {self.rtext(rule)}", "direction": self.side,
               "description": f"One-stock strategy for {self.sym} {self.tf}, built {date.today()} by stock_strategy "
                              f"(walk-forward evidence in REPORT.md).",
               "entry": entry, "exit": ex, "enabled": True,
               "applicability": {"symbols": [f"{self.sym}-EQ"], "timeframes": [self.tf]},
               "tags": ["stock-specific", "discovered"],
               "evidence_tuning": {"search": {k: (round(v, 5) if isinstance(v, float) else v) for k, v in info.items()},
                                   "cautious": round(sc, 5), "engine": eng}}
        (self.du.root / "strategy.json").write_text(json.dumps(
            {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": [doc],
             "keep_original": []}, indent=1), encoding="utf-8")
        return md + ["", "strategy.json written - not in the library until the owner approves; then paper-trade it "
                         "unchanged.", ""]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--holds", default="5,10,20")
    ap.add_argument("--targets", default="2,3,4")
    ap.add_argument("--stops", default="1,1.5,2")
    ap.add_argument("--first-test-year", type=int, default=2016)
    ap.add_argument("--min-trades", type=int, default=25)
    ap.add_argument("--half-life", type=float, default=3.0)
    ap.add_argument("--beam", type=int, default=40)
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    b = Builder(a)
    try:
        return b.run()
    finally:
        b.du.close()


if __name__ == "__main__":
    sys.exit(main())
