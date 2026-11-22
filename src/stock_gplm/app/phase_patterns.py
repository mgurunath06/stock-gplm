"""Phase patterns for ONE stock (owner 2026-11-22): what worked the last 3 times the stock was in today's phase.

    $env:PYTHONPATH="src"; python -m stock_gplm.app.phase_patterns --tf 1d --symbol RELIANCE   [--side long|short]

* PHASE of every bar (own TF): BULLISH = EMA9 > EMA13 > EMA20 > EMA50, BEARISH = EMA9 < EMA13 < EMA20 < EMA50,
  SIDEWAYS = anything else (config/research/phase_patterns.json).
* PATTERN = 1-3 conditions on indicator values (MACD below 0, close above VWAP, RSI(3) 55-70, volume 2x previous bar,
  volume slowly falling, ... = config/research/move_features.json + extra_features), levels at the 20/40/60/80% points.
* A TIME = the first bar of a new run of signal bars while the stock is in that phase (enter next open).
* WORKED = the trade reached +1% after costs (brokerage/STT/charges of the lane) before the stop (S x ATR) or the time
  exit (H bars); each pattern uses the exit (H, S) from the grid that suits it best.
* QUALIFIES = its last 3 times in that phase all worked -> Medium confidence. Older times in the same phase move it:
  Medium-high / High / Very high with more older times that also worked, or "Low (recent luck)" when they mostly failed.
* Output <reports>/tuning/phase-patterns-<tf>-<SYMBOL>-<side>/: PATTERNS.md (today's phase, patterns active today, best
  patterns per phase), patterns.csv (every qualifying pattern). All history from 2010 is used; no date split.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import tune_eval as te
from .discover import Discoverer
from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]
PATTERNS_CFG = REPO / "config" / "research" / "phase_patterns.json"
PHASES = {1: "BULLISH", 0: "SIDEWAYS", -1: "BEARISH"}
Cond = tuple[int, str, float]


def _date(d: int) -> str:
    return str(np.datetime64(int(d), "D"))


class Finder:
    def __init__(self, a: argparse.Namespace):
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.pc = json.loads(Path(a.patterns).read_text(encoding="utf-8"))
        self.sym = a.symbol.upper().removesuffix("-EQ")
        tmp = Path(tempfile.gettempdir()) / f"phase_patterns_{self.sym}.txt"
        tmp.write_text(self.sym + "\n", encoding="utf-8")
        self.du = Discoverer(a.tf, f"phase-patterns-{a.tf}-{self.sym}-{a.side}", cfg, "@" + str(tmp), 1)
        self.tf, self.short, self.side, self.log = a.tf, a.side == "short", a.side, self.du.log
        ex = self.pc["exits"].get(a.tf) or self.pc["exits"]["1d"]
        self.exits = [(int(h), float(s)) for h in ex["holds"] for s in ex["stops_atr"]]
        self.R = int(self.pc["recent_wins_needed"])
        self.gain = float(self.pc["worked_net_pct"]) / 100.0

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        from . import strategy_dsl as dsl
        self.log(f"{self.sym} {self.tf}: loading candles and computing indicators")
        L = te._load(self.du.stocks[0], self.du.config, [self.tf], bool(self.du.t["use_minute"])).get(self.tf)
        if L is None:
            self.log(f"no {self.tf} data for {self.sym}")
            return False
        m, ctx = L["m"], L["ctx"]
        n = len(m)
        self.n, self.days = n, L["days"]
        a0 = int(np.searchsorted(L["ses"], self.du.t["tune_start"]))
        floor = np.asarray(dsl.signal(self.du.floor, ctx), bool)
        exprs = {**te.feature_exprs(m.intraday), **(self.pc.get("extra_features") or {})}
        names, cols = [], []
        for nm, ex in exprs.items():
            try:
                x = np.broadcast_to(np.asarray(dsl.evaluate(dsl.parse(ex), ctx), dtype=float), (n,))
            except Exception as exc:  # noqa: BLE001 - a feature that cannot be computed is skipped
                self.log(f"  feature {nm} skipped ({type(exc).__name__})")
                continue
            names.append(nm)
            cols.append(np.array(x, dtype=float))
        self.names, self.exprs = names, exprs
        self.F = np.stack(cols, axis=1)
        bull = np.asarray(dsl.signal(self.pc["phase"]["bullish"], ctx), bool)
        bear = np.asarray(dsl.signal(self.pc["phase"]["bearish"], ctx), bool)
        self.phase = np.where(bull, 1, np.where(bear, -1, 0))
        self.ok = floor & (np.arange(n) >= a0)
        E = len(self.exits)
        self.win = np.zeros((E, n), bool)
        self.net = np.full((E, n), np.nan)
        self.done = np.zeros((E, n), bool)
        for k, (h, s) in enumerate(self.exits):
            t, net, win = self.label(m, n, h, s, L["buy"], L["sell"])
            self.net[k, t], self.win[k, t], self.done[k, t] = net, win, True
        self.log(f"{n:,} bars {_date(self.days[0])} .. {_date(self.days[-1])}; {len(names)} indicators; phase today: "
                 f"{PHASES[int(self.phase[-1])]}")
        return True

    def label(self, m: Any, n: int, h: int, S: float, buy: float, sell: float):
        """Every bar: enter next open; worked = +gain net (after costs) reached before the stop / time exit."""
        natr = np.asarray(m.natr, dtype=float)
        t = np.arange(n - 1)
        t = t[np.isfinite(natr[t])]
        if m.intraday:
            end = np.minimum(t + h, np.asarray(m.exit_last)[t + 1].astype(np.int64))
            keep = end >= t + 1
        else:
            end = t + h
            keep = end <= n - 1
        t, end = t[keep], end[keep]
        o, hi, lo, cl = (np.asarray(x, dtype=float) for x in (m.open, m.high, m.low, m.close))
        ent = o[t + 1]
        atr = natr[t] * ent
        g = self.gain
        if self.short:
            tgt, stp = ent * (1 - sell - g) / (1 + buy), ent + S * atr
        else:
            tgt, stp = ent * (1 + g) * (1 + buy) / (1 - sell), ent - S * atr
        never = h + 10
        ft, fs = np.full(len(t), never), np.full(len(t), never)
        for k in range(1, h + 1):
            j = t + k
            live = j <= end
            if not live.any():
                break
            jj = np.minimum(j, n - 1)
            th = live & ((lo[jj] <= tgt) if self.short else (hi[jj] >= tgt))
            sh = live & ((hi[jj] >= stp) if self.short else (lo[jj] <= stp))
            ft = np.where(th & (ft == never), k, ft)
            fs = np.where(sh & (fs == never), k, fs)
        win = ft < fs  # same bar -> stop first (conservative)
        px = np.where(win, tgt, np.where(fs < never, stp, cl[end]))
        net = (ent * (1 - sell) - px * (1 + buy)) / ent if self.short else px * (1 - sell) / (ent * (1 + buy)) - 1
        return t, net, win

    def build_conditions(self) -> None:
        conds: list[Cond] = []
        F = self.F[self.ok]
        for j in range(F.shape[1]):
            x = F[:, j]
            x = x[np.isfinite(x)]
            if len(x) < 100:
                continue
            u = np.unique(x)
            if len(u) < 2:
                continue
            if np.isin(u, (0.0, 1.0)).all():
                conds += [(j, "yes", 1.0), (j, "no", 0.0)]
                continue
            for e in np.unique([float(f"{q:.4g}") for q in np.quantile(x, self.pc["levels"])]):
                if e > u[0]:
                    conds += [(j, "ge", float(e)), (j, "lt", float(e))]
        self.conds = conds
        M = np.zeros((len(conds), self.n), bool)
        for c, (j, kind, lv) in enumerate(conds):
            x = self.F[:, j]
            fin = np.isfinite(x)
            M[c] = fin & ((x >= lv) if kind == "ge" else (x < lv) if kind == "lt" else (x > 0.5) if kind == "yes"
                          else (x <= 0.5))
        self.M = M
        self.log(f"{len(conds)} conditions")

    # ------------------------------------------------------------------ judging
    def confidence(self, older: int, rate: float) -> tuple[str, int]:
        lad = self.pc["confidence_ladder"]
        for i, r in enumerate(lad):
            if older >= r["older_min"] and rate >= r["older_win_min"]:
                return r["label"], len(lad) - i
        lo = self.pc.get("low_confidence") or {}
        if lo and older >= lo["older_min"] and rate < lo["older_win_below"]:
            return lo["label"], -1
        return self.pc["base_confidence"], 0

    def judge(self, sig: np.ndarray) -> dict[str, Any] | None:
        """Best exit for which the last R times in phase all worked; None if no exit qualifies."""
        st = np.flatnonzero(sig & ~np.r_[False, sig[:-1]])
        if len(st) < self.R:
            return None
        best = None
        for e in range(len(self.exits)):
            s = st[self.done[e, st]]
            if len(s) < self.R:
                continue
            w = self.win[e, s]
            if not w[-self.R:].all():
                continue
            ow = w[:-self.R]
            no = len(ow)
            rate = float(ow.mean()) if no else 0.0
            label, rank = self.confidence(no, rate)
            key = (rank, te.win_low(rate, no) if no else 0.0, no)
            if best is None or key > best["key"]:
                best = {"key": key, "e": e, "times": s, "older": no, "older_won": int(ow.sum()), "rate": rate,
                        "conf": label, "rank": rank, "avg": float(np.mean(self.net[e, s]))}
        return best

    def search(self, P: int) -> list[tuple[tuple[int, ...], dict]]:
        base = self.ok & (self.phase == P)
        found: list[tuple[tuple[int, ...], dict]] = []
        seen: set[bytes] = set()

        def add(rule: tuple[int, ...], sig: np.ndarray) -> dict | None:
            r = self.judge(sig)
            if r is None:
                return None
            k = r["times"].tobytes() + bytes([r["e"]])
            if k in seen:  # same signals as a simpler rule already found
                return None
            seen.add(k)
            found.append((rule, r))
            return r

        live = [c for c in range(len(self.conds)) if (self.M[c] & base).sum() >= self.R]
        for c in live:
            add((c,), self.M[c] & base)
        t0 = time.monotonic()
        pairs: list[tuple[tuple[int, int], dict]] = []
        for i, c1 in enumerate(live):
            s1 = self.M[c1] & base
            for c2 in live[i + 1:]:
                if self.conds[c1][0] == self.conds[c2][0]:
                    continue
                r = add((c1, c2), s1 & self.M[c2])
                if r:
                    pairs.append(((c1, c2), r))
            if i % 100 == 99:
                self.log(f"  {PHASES[P]}: pairs {i + 1}/{len(live)} conditions, {len(found):,} qualifying "
                         f"({time.monotonic() - t0:.0f}s)")
        pairs.sort(key=lambda x: x[1]["key"], reverse=True)
        tried: set[tuple[int, ...]] = set()
        for (c1, c2), _ in pairs[:int(self.pc["triples_from_best_pairs"])]:
            s12 = self.M[c1] & self.M[c2] & base
            used = {self.conds[c1][0], self.conds[c2][0]}
            for c in live:
                rule = tuple(sorted((c1, c2, c)))
                if self.conds[c][0] in used or rule in tried:
                    continue
                tried.add(rule)
                add(rule, s12 & self.M[c])
        found.sort(key=lambda x: x[1]["key"], reverse=True)
        self.log(f"{PHASES[P]}: {len(found):,} qualifying patterns ({time.monotonic() - t0:.0f}s)")
        return found

    # ------------------------------------------------------------------ text
    def ctext(self, c: Cond) -> str:
        j, kind, lv = c
        nm = self.names[j]
        return {"ge": f"{nm} >= {lv:g}", "lt": f"{nm} < {lv:g}", "yes": nm, "no": f"not {nm}"}[kind]

    def rtext(self, rule: tuple[int, ...]) -> str:
        return " AND ".join(self.ctext(self.conds[c]) for c in rule)

    def row(self, rule: tuple[int, ...], r: dict) -> str:
        h, s = self.exits[r["e"]]
        last = ", ".join(_date(self.days[t]) for t in r["times"][-self.R:])
        older = f"{r['older_won']} of {r['older']} ({r['rate']:.0%})" if r["older"] else "none"
        return (f"| {self.rtext(rule)} | stop {s:g} ATR, max {h} bars | **{r['conf']}** | {last} | {older} | "
                f"{100 * r['avg']:+.2f}% |")

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if not self.load():
            return 1
        self.build_conditions()
        now = self.n - 1
        P = int(self.phase[now])
        k = now
        while k > 0 and self.phase[k - 1] == P:
            k -= 1
        res = {p: self.search(p) for p in (P, *[q for q in (1, 0, -1) if q != P])}
        top = int(self.pc["report_top"])
        head = ["| Pattern | Exit | Confidence | Last 3 times (all worked) | Older times in phase that worked | "
                "Avg net, all times |", "|---|---|---|---|---|---|"]
        md = [f"# Phase patterns - {self.sym} {self.tf} {self.side}", "",
              f"Last bar {_date(self.days[now])}: phase **{PHASES[P]}** since {_date(self.days[k])} "
              f"({now - k + 1} bars). Phase: bullish = EMA9 > EMA13 > EMA20 > EMA50, bearish = the reverse, "
              "sideways = anything else.",
              f"A pattern qualifies when its last {self.R} times in the phase all reached +{100 * self.gain:g}% after "
              "costs before the stop / time exit (Medium). Older times in the same phase raise the confidence when "
              "they also worked, or mark it 'Low (recent luck)' when they mostly failed. With thousands of "
              "combinations some 3-in-a-row are luck - trust patterns with many older times behind them.", ""]
        cur = res[P]
        active = [(r, x) for r, x in cur if np.logical_and.reduce(self.M[list(r), now]) and self.ok[now]]
        md += [f"## 1. Active today ({PHASES[P]}, pattern true on the last bar)", ""]
        md += head + [self.row(r, x) for r, x in active[:top]] if active else ["None of the qualifying patterns is "
                                                                               "true on the last bar."]
        md += ["", f"## 2. Best patterns for the current phase ({PHASES[P]}) - watch list", ""]
        md += head + [self.row(r, x) for r, x in cur[:top]]
        for q in (1, 0, -1):
            if q == P:
                continue
            md += ["", f"## {PHASES[q]} phase (for when the phase changes)", ""]
            md += head + [self.row(r, x) for r, x in res[q][:top]]
        root = self.du.root
        (root / "PATTERNS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        with (root / "patterns.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["phase", "pattern", "hold_bars", "stop_atr", "confidence", "times", "older", "older_won",
                        "avg_net_pct", "last_time", "active_today"])
            for q, lst in res.items():
                for r, x in lst:
                    h, s = self.exits[x["e"]]
                    act = q == P and bool(np.logical_and.reduce(self.M[list(r), now]))
                    w.writerow([PHASES[q], self.rtext(r), h, s, x["conf"], len(x["times"]), x["older"],
                                x["older_won"], round(100 * x["avg"], 3), _date(self.days[x["times"][-1]]), act])
        self.log(f"report: {root / 'PATTERNS.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--patterns", default=str(PATTERNS_CFG))
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    f = Finder(a)
    try:
        return f.run()
    finally:
        f.du.close()


if __name__ == "__main__":
    sys.exit(main())
