"""One stock, every move, every indicator value - then find the groupings (owner 2026-11-22).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.move_table --tf 1d --symbol RELIANCE
    (options: --move 10,2.5,1.5  = H bars, target ATR, stop ATR (default: first move in tuning.json discover.moves);
              --side long|short; --top 25)

A MOVE = price reaches +T x ATR(14) before -S x ATR(14) within H bars (entry next open, costs included) - the same
definition as move-first discovery. Consecutive bars that all lead into the same move are ONE move (first bar = its
start). Output folder <reports>/tuning/move-table-<tf>-<SYMBOL>/:
  MOVES.csv      one row per move: date, window, result, bars held, then every feature value at the move's start
  ALL_BARS.csv   every bar (move or not) with the same columns - open in Excel to explore yourself
  PATTERNS.md    what is different at move starts, the natural groups of moves (clustering) with the rule each group
                 implies and how often that rule was REALLY followed by a move (all bars, not only the moves),
                 best 2- and 3-condition rules, a small decision tree - each checked on the unseen CONFIRM years.
Patterns are found on the TUNE window only; CONFIRM is the check.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from . import tune_eval as te
from .discover import Discoverer
from .tune import CONFIG

QS = [0.2, 0.4, 0.6, 0.8]


def _date(d: int) -> str:
    return str(np.datetime64(int(d), "D"))


class Table:
    def __init__(self, a: argparse.Namespace):
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.sym = a.symbol.upper().removesuffix("-EQ")
        tmp = Path(tempfile.gettempdir()) / f"move_table_{self.sym}.txt"
        tmp.write_text(self.sym + "\n", encoding="utf-8")
        self.du = Discoverer(a.tf, f"move-table-{a.tf}-{self.sym}", cfg, "@" + str(tmp), 1)
        mv = (cfg.get("discover") or {}).get("moves", {}).get(a.tf) or [{"h": 10, "t": 2.5, "s": 1.5}]
        if a.move:
            h, t, s = (float(x) for x in a.move.split(","))
            self.mv = {"h": int(h), "t": t, "s": s}
        else:
            self.mv = {"h": int(mv[0]["h"]), "t": float(mv[0]["t"]), "s": float(mv[0]["s"])}
        self.side, self.top, self.tf = a.side, a.top, a.tf
        self.names = te.feature_names(self.du.intraday)
        self.log = self.du.log

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        mv = self.mv
        ex = {"max_hold_bars": mv["h"], "target_atr": mv["t"], "stop_atr": mv["s"]}
        v = te.Variant(f"TABLE~{self.side}-{mv['h']}-{mv['t']:g}-{mv['s']:g}", self.tf, self.du.floor, ex, self.side,
                       True, dict(mv))
        self.log(f"{self.sym} {self.tf}: labelling every liquid bar - move = +{mv['t']:g} ATR before -{mv['s']:g} ATR "
                 f"within {mv['h']} bars ({self.side})")
        raw = self.du.evaluate([v])[v.vid].get("raw")
        if raw is None or "feat" not in raw or len(raw["net"]) < 100:
            self.log(f"not enough data for {self.sym} (did it pass the liquidity floor in this period?)")
            return False
        o = np.argsort(raw["bar"], kind="stable")
        self.bar = raw["bar"][o].astype(np.int64)
        self.day = raw["day"][o].astype(np.int64)
        self.hit = (raw["reason"][o] == 1).astype(float)
        self.net = raw["net"][o].astype(float)
        self.held = raw["held"][o].astype(int)
        self.feat = raw["feat"][o].astype(float)
        self.tune = (self.day >= self.du.tune_day) & (self.day < self.du.confirm_day)
        self.conf = self.day >= self.du.confirm_day
        self.blk = self.bar // max(mv["h"], 1)  # non-overlapping windows of H bars = episodes
        self.log(f"{len(self.bar):,} bars labelled ({int(self.tune.sum()):,} tune, {int(self.conf.sum()):,} confirm); "
                 f"bars leading into a move: {int(self.hit.sum()):,}")
        return True

    def episodes(self) -> np.ndarray:
        """Row index of each move's first bar: a new move starts when a hit bar is not adjacent to the previous hit
        bar or the current move is already H bars old."""
        starts, last, first = [], -10 ** 9, -10 ** 9
        for i in np.flatnonzero(self.hit > 0):
            b = self.bar[i]
            if b > last + 1 or b >= first + self.mv["h"]:
                starts.append(i)
                first = b
            last = b
        return np.asarray(starts, dtype=np.int64)

    # ------------------------------------------------------------------ helpers
    def stat(self, sel: np.ndarray) -> dict[str, Any]:
        """Bars / episodes / episode hit rate / cautious hit / avg net for a boolean selection."""
        n = int(sel.sum())
        if not n:
            return {"bars": 0, "eps": 0, "hr": 0.0, "lb": 0.0, "net": 0.0}
        u, inv = np.unique(self.blk[sel], return_inverse=True)
        c = np.bincount(inv)
        hm = np.bincount(inv, weights=self.hit[sel]) / c
        nm = np.bincount(inv, weights=self.net[sel]) / c
        e = len(u)
        hr = float(hm.mean())
        return {"bars": n, "eps": e, "hr": hr, "lb": te.win_low(hr, e), "net": float(nm.mean())}

    def fmt(self, s: dict[str, Any], base: float) -> str:
        if not s["eps"]:
            return "-"
        return (f"{s['eps']} moves-windows / hit {s['hr']:.0%} (x{s['hr'] / base:.1f}) / cautious {s['lb']:.0%} / "
                f"avg {100 * s['net']:+.2f}%" if base > 0 else f"{s['eps']} / {s['hr']:.0%}")

    def write_csv(self, path: Path, rows: np.ndarray, extra: dict[str, np.ndarray]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "bar", "window", *extra.keys(), *self.names])
            for k, i in enumerate(rows):
                win = "tune" if self.tune[i] else ("confirm" if self.conf[i] else "before")
                w.writerow([_date(self.day[i]), int(self.bar[i]), win, *(x[k] for x in extra.values()),
                            *(("" if not np.isfinite(x) else f"{x:.5g}") for x in self.feat[i])])

    # ------------------------------------------------------------------ analysis
    def run(self) -> int:
        if not self.load():
            return 1
        root = self.du.root
        ep = self.episodes()
        self.log(f"moves found: {len(ep)} ({int(self.tune[ep].sum())} tune, {int(self.conf[ep].sum())} confirm)")
        self.write_csv(root / "MOVES.csv", ep, {"result_pct": np.round(100 * self.net[ep], 2),
                                                 "bars_to_target": self.held[ep]})
        allr = np.arange(len(self.bar))
        self.write_csv(root / "ALL_BARS.csv", allr, {"move": self.hit.astype(int),
                                                      "result_pct": np.round(100 * self.net, 2)})
        self.log(f"tables written: {root / 'MOVES.csv'} and ALL_BARS.csv")

        T, C = self.tune, self.conf
        bt, bc = self.stat(T), self.stat(C)
        base = bt["hr"]
        mv = self.mv
        md = [f"# Move patterns - {self.sym} {self.tf}", "",
              f"MOVE = +{mv['t']:g} x ATR(14) before -{mv['s']:g} x ATR(14) within {mv['h']} bars ({self.side}, entry "
              f"next open, costs included). Moves found: **{len(ep)}** ({int(T[ep].sum())} in TUNE "
              f"{_date(self.du.tune_day)}..{_date(self.du.confirm_day)}, {int(C[ep].sum())} in CONFIRM).",
              f"Base rate (any bar is followed by a move): TUNE {bt['hr']:.0%}, CONFIRM {bc['hr']:.0%}. "
              f"Break-even ~{mv['s'] / (mv['t'] + mv['s']):.0%} before costs.",
              "Counts are move-windows (non-overlapping blocks of H bars), so one long setup is not counted many "
              "times. 'Cautious' = 20% lower estimate of the hit rate for that many windows. A rule is useful only "
              "if it is followed by a move clearly more often than the base rate - on TUNE AND on CONFIRM.", ""]

        # pctl transform on TUNE bars (scale-free, robust for clustering)
        F = self.feat
        ok_cols = [j for j in range(F.shape[1]) if np.isfinite(F[T, j]).sum() >= 50]
        pct = np.full(F.shape, 0.5)
        for j in ok_cols:
            ref = np.sort(F[T, j][np.isfinite(F[T, j])])
            x = F[:, j]
            fin = np.isfinite(x)
            pct[fin, j] = np.searchsorted(ref, x[fin], side="right") / len(ref)

        # 1. what is different at move starts
        self.log("step 1/5: feature differences at move starts")
        et = ep[T[ep]]
        rows = []
        for j in ok_cols:
            m_med = float(np.nanmedian(pct[et, j])) if len(et) else 0.5
            rows.append((abs(m_med - 0.5), j, m_med))
        rows.sort(reverse=True)
        md += ["## 1. What is different when a move starts (TUNE)", "",
               "Typical position of each feature at move starts versus all bars (50 = no difference; 80 = moves start "
               "when this feature is in its top 20%).", "",
               "| Feature | At move starts (percentile) | Median value at moves | Median value all bars |", "|---|---|---|---|"]
        for _, j, m_med in rows[:self.top]:
            md.append(f"| {self.names[j]} | {100 * m_med:.0f} | {np.nanmedian(F[et, j]):.4g} | "
                      f"{np.nanmedian(F[T, j]):.4g} |")
        md.append("")

        # 2. natural groups of moves
        self.log("step 2/5: grouping the moves (clustering)")
        md += self.groups(et, pct, ok_cols, base)

        # 3/4. best 2- and 3-condition rules on this stock
        self.log("step 3/5: best 2-condition rules")
        bins = np.full(F.shape, -1, dtype=np.int8)
        edges: dict[int, np.ndarray] = {}
        for j in ok_cols:
            x = F[:, j]
            fin = np.isfinite(x)
            ref = x[T & fin]
            binary = bool(np.isin(np.unique(ref), (0.0, 1.0)).all())
            qs = np.array([0.5]) if binary else np.unique(np.quantile(ref, QS))
            edges[j] = qs
            bins[fin, j] = np.searchsorted(qs, x[fin], side="right")
        min_eps = max(12, int(0.02 * bt["eps"]))
        lead = [r[1] for r in rows[:15]]
        md += self.rules(2, ok_cols, bins, edges, base, min_eps)
        self.log("step 4/5: best 3-condition rules (15 most different features)")
        md += self.rules(3, lead, bins, edges, base, min_eps)

        # 5. decision tree
        self.log("step 5/5: decision tree")
        md += self.tree(ok_cols, base, min_eps)

        (root / "PATTERNS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"report: {root / 'PATTERNS.md'}")
        return 0

    def cond(self, j: int, lo: float | None, hi: float | None) -> str:
        nm = self.names[j]
        parts = ([f"{nm} >= {lo:.4g}"] if lo is not None else []) + ([f"{nm} < {hi:.4g}"] if hi is not None else [])
        return " and ".join(parts) if parts else nm

    def groups(self, et: np.ndarray, pct: np.ndarray, cols: list[int], base: float) -> list[str]:
        out = ["## 2. Natural groups of moves (TUNE)", "",
               "Moves are grouped by how similar their indicator values were (k-means on percentiles, number of groups "
               "chosen by silhouette). Each group's rule = the 3 features that set it apart, inside the range its "
               "middle 60% had. Then the rule is applied to ALL bars: how often was it really followed by a move?", ""]
        if len(et) < 30:
            return out + [f"Too few TUNE moves ({len(et)}) to group.", ""]
        try:
            from sklearn.cluster import KMeans
            from sklearn.metrics import silhouette_score
        except ImportError:
            return out + ["(scikit-learn not installed - grouping skipped)", ""]
        X = pct[np.ix_(et, cols)]
        best = None
        for k in range(2, min(7, len(et) // 10 + 1)):
            km = KMeans(k, n_init=10, random_state=0).fit(X)
            sc = float(silhouette_score(X, km.labels_))
            if best is None or sc > best[0]:
                best = (sc, k, km.labels_)
        if best is None:
            return out + ["Too few moves to group.", ""]
        sc, k, lab = best
        out += [f"{k} groups (silhouette {sc:.2f}; near 0 = the moves do not form clear groups).", "",
                "| Group | Moves | What sets it apart (group percentile) | Rule | TUNE: followed by a move | CONFIRM |",
                "|---|---|---|---|---|---|"]
        F = self.feat
        for g in range(k):
            mem = et[lab == g]
            med = np.median(pct[np.ix_(mem, cols)], axis=0)
            order = np.argsort(-np.abs(med - 0.5))[:3]
            sel = np.ones(len(self.bar), bool)
            parts, desc = [], []
            for oi in order:
                j = cols[oi]
                vals = F[mem, j][np.isfinite(F[mem, j])]
                if not len(vals):
                    continue
                lo, hi = (float(x) for x in np.quantile(vals, [0.2, 0.8]))
                binary = bool(np.isin(np.unique(F[self.tune, j][np.isfinite(F[self.tune, j])]), (0.0, 1.0)).all())
                x = F[:, j]
                if binary:
                    v = 1.0 if med[oi] > 0.5 else 0.0
                    sel &= x == v
                    parts.append(self.names[j] if v else f"not {self.names[j]}")
                else:
                    sel &= (x >= lo) & (x <= hi)
                    parts.append(f"{self.names[j]} {lo:.4g}..{hi:.4g}")
                desc.append(f"{self.names[j]} {100 * med[oi]:.0f}")
            st, sc_ = self.stat(sel & self.tune), self.stat(sel & self.conf)
            out.append(f"| {g + 1} | {len(mem)} | {', '.join(desc)} | {' AND '.join(parts)} | {self.fmt(st, base)} | "
                       f"{self.fmt(sc_, base)} |")
        return out + [""]

    def rules(self, k: int, cols: list[int], bins: np.ndarray, edges: dict[int, np.ndarray], base: float,
              min_eps: int) -> list[str]:
        T, C = self.tune, self.conf
        found = []
        for combo in combinations(cols, k):
            ok = np.all(bins[:, list(combo)] >= 0, axis=1)
            key = np.zeros(len(self.bar), np.int64)
            for j in combo:
                key = key * 5 + np.maximum(bins[:, j], 0)
            for cell in np.unique(key[ok & T]):
                sel = ok & (key == cell)
                st = self.stat(sel & T)
                if st["eps"] < min_eps or st["net"] <= 0 or st["hr"] < base + 0.05:
                    continue
                found.append((st["lb"], combo, int(cell), st, sel))
        found.sort(key=lambda r: -r[0])
        out = [f"## {k + 1}. Best {k}-condition rules (TUNE, >= {min_eps} move-windows, ranked by cautious hit rate)",
               "", "| Rule | TUNE | CONFIRM |", "|---|---|---|"]
        seen = 0
        for lb, combo, cell, st, sel in found:
            qs_, rest = [], cell
            for _ in combo:
                rest, q = divmod(rest, 5)
                qs_.append(q)
            qs_ = qs_[::-1]
            parts = []
            for j, q in zip(combo, qs_):
                e = edges[j]
                if len(e) == 1 and e[0] == 0.5:
                    parts.append(self.names[j] if q == 1 else f"not {self.names[j]}")
                else:
                    parts.append(self.cond(j, float(e[q - 1]) if q > 0 else None,
                                           float(e[q]) if q < len(e) else None))
            out.append(f"| {' AND '.join(parts)} | {self.fmt(st, base)} | {self.fmt(self.stat(sel & C), base)} |")
            seen += 1
            if seen >= self.top:
                break
        if not seen:
            out.append(f"| (no {k}-condition rule beat the base rate by 5 points with positive net) | | |")
        return out + [""]

    def tree(self, cols: list[int], base: float, min_eps: int) -> list[str]:
        out = ["## 5. Decision tree (TUNE)", ""]
        try:
            from sklearn.tree import DecisionTreeClassifier
        except ImportError:
            return out + ["(scikit-learn not installed)", ""]
        T = self.tune
        X = np.nan_to_num(self.feat[:, cols], nan=0.0)
        leaf_n = max(min_eps * self.mv["h"] // 2, 30)
        clf = DecisionTreeClassifier(max_depth=4, min_samples_leaf=leaf_n, random_state=0).fit(X[T], self.hit[T])
        leaves = clf.apply(X)
        tr = clf.tree_
        # path text per leaf
        paths: dict[int, list[str]] = {}

        def walk(node: int, conds: list[str]) -> None:
            if tr.children_left[node] == -1:
                paths[node] = conds
                return
            nm, th = self.names[cols[tr.feature[node]]], tr.threshold[node]
            walk(tr.children_left[node], conds + [f"{nm} <= {th:.4g}"])
            walk(tr.children_right[node], conds + [f"{nm} > {th:.4g}"])
        walk(0, [])
        rows = []
        for leaf, conds in paths.items():
            sel = leaves == leaf
            st = self.stat(sel & T)
            if st["hr"] >= base * 1.2 and st["eps"] >= min_eps:
                rows.append((st["lb"], conds, st, self.stat(sel & self.conf)))
        rows.sort(key=lambda r: -r[0])
        out += ["Leaves whose hit rate is >= 1.2x the base rate:", "", "| Rule | TUNE | CONFIRM |", "|---|---|---|"]
        out += [f"| {' AND '.join(c) or '(all)'} | {self.fmt(s, base)} | {self.fmt(sc, base)} |"
                for _, c, s, sc in rows[:self.top]]
        if not rows:
            out.append("| (no leaf clearly above the base rate) | | |")
        return out + [""]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--move", help="H,T,S e.g. 20,4,2 (default: first move in tuning.json for the timeframe)")
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    t = Table(a)
    try:
        return t.run()
    finally:
        t.du.close()


if __name__ == "__main__":
    sys.exit(main())
