"""Move-first strategy discovery (owner 2026-11-22: "rather than create a strategy and see if it fits, see a move and
create a strategy").

    python -m stock_gplm.app.discover --tf 1d --mode moves        # -> <reports>/tuning/moves-1d/

1. Define a MOVE per timeframe (config/tuning/tuning.json discover.moves): price reaches +T x ATR(14) BEFORE it
   falls -S x ATR(14), within H bars (intraday also the mirror for shorts; 15:15 square-off). Every liquid bar of
   every universe stock in the TUNE window is labelled hit / miss by the real engine (costs included).
2. Fingerprint: for every feature / event (trend distance, RSI, ATR%, volume, gaps, EMA touches, MACD/RSI crosses,
   breakouts, time of day ...) the HIT RATE that followed - i.e. precision, how often the condition was FOLLOWED by
   a move, not how often moves had it - and the lift over the base rate; best 2- and 3-condition combinations; and
   readable rules from a small decision tree (weighted by recency).
3. The LLM turns the best rules into strategies whose exit IS the move (target T ATR, stop S ATR, H bars); the
   usual rounds, exit sweep and CONFIRM gate follow (discover.py). Ids MOVE-<tf>-<Name>.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

import numpy as np

from . import tune_eval as te
from . import tune_llm as tl
from .discover import Discoverer, _q

MOVES_DEFAULT = {
    "1d": [{"h": 10, "t": 2.5, "s": 1.5}, {"h": 20, "t": 4.0, "s": 2.0}],
    "15m": [{"h": 8, "t": 3.0, "s": 1.5}, {"h": 16, "t": 5.0, "s": 2.0}],
    "5m": [{"h": 24, "t": 4.0, "s": 2.0}, {"h": 36, "t": 6.0, "s": 2.5}],
    "60m": [{"h": 6, "t": 2.5, "s": 1.25}, {"h": 12, "t": 4.0, "s": 2.0}],
}
ATR_RANGES = {"stop_atr": (0.5, 5.0), "target_atr": (0.5, 10.0)}
SCHEMA = ('{"strategies": [{"name": "2-4 words, readable", "idea": "one sentence: which rule / facts it uses", '
          '"from": "<id of a tried strategy it improves>" or null, "direction": "long" or "short", '
          '"entry": "<DSL condition WITHOUT the liquidity floor>", '
          '"exit": {"target_atr": number, "stop_atr": number, "max_hold_bars": int, "trail_atr": number|null, '
          '"when": "<optional DSL exit condition>" or null}}]}')


class MoveDiscoverer(Discoverer):
    def __init__(self, tf: str, name: str, cfg: Mapping[str, Any], stocks_arg: str | None, workers: int):
        super().__init__(tf, name, cfg, stocks_arg, workers)
        self.prefix = "MOVE"
        self.moves = ((cfg.get("discover") or {}).get("moves") or MOVES_DEFAULT).get(tf) or MOVES_DEFAULT[tf]

    # -------------------------------------------------------------- facts: start from the moves
    def facts(self) -> str:
        if self.state.get("facts"):
            return self.state["facts"]
        dirs = ["long", "short"] if self.intraday else ["long"]
        probes = []
        for mv in self.moves:
            for dr in dirs:
                ex = {"max_hold_bars": int(mv["h"]), "target_atr": float(mv["t"]), "stop_atr": float(mv["s"])}
                probes.append(te.Variant(f"MOVE~{dr}-{mv['h']}-{mv['t']:g}-{mv['s']:g}", self.tf, self.floor, ex, dr,
                                         True))
        self.log(f"moves: labelling every liquid bar for {len(probes)} move definitions {self.moves} {dirs}")
        res = self.evaluate(probes)
        names = te.feature_names(self.intraday)
        exprs = {**te.FEATURES, **(te.INTRADAY_FEATURES if self.intraday else {})}
        L = [f"MOVE FACTS {self.tf} - {len(self.stocks)} liquid NSE stocks, tuning window {self.t['tune_start']} .. "
             f"{self.t['confirm_start']} (recent bars weigh more). A MOVE = price reaches +T x ATR(14) before -S x "
             "ATR(14) within H bars (short: mirrored). Every liquid bar is labelled hit / miss; 'hit rate' = how often a "
             "condition was FOLLOWED by a move (precision); lift = hit rate / base rate; net = average result of that "
             "trade after costs. Event features: yes / no.",
             "Feature DSL: " + "; ".join(f"{k} = {exprs[k]}" for k in names),
             "Build entries from the HIGHEST hit-rate rules (tree rules, combinations); the hit rate must be clearly "
             "above the break-even hit rate. Use the move's target / stop / H as the exit.", ""]
        for v in probes:
            raw = res[v.vid].get("raw")
            if raw is None or "feat" not in raw:
                continue
            day = raw["day"].astype(np.int64)
            m = (day >= self.tune_day) & (day < self.confirm_day)
            if m.sum() < 1000:
                continue
            hit = (raw["reason"][m] == 1).astype(float)  # TARGET / TARGET_GAP = target reached before stop
            net, feat = raw["net"][m].astype(float), raw["feat"][m].astype(float)
            w = te.weights(day[m], self.confirm_day - 1, float(self.t["decay"]))
            L += self._move_facts(v.vid.split("~")[1], hit, net, feat, w, names)
        txt = "\n".join(L)
        self.state["facts"] = txt
        self.save()
        (self.root / "FACTS.md").write_text("# " + txt.replace("\n", "\n\n", 3) + "\n", encoding="utf-8")
        self.log(f"facts written: {self.root / 'FACTS.md'} ({len(txt):,} chars)")
        return txt

    def _move_facts(self, label: str, hit: np.ndarray, net: np.ndarray, feat: np.ndarray, w: np.ndarray,
                    names: list[str]) -> list[str]:
        side, h, t, s = label.split("-")
        N, sw = len(hit), w.sum()
        p0, n0 = float((w * hit).sum() / sw), float((w * net).sum() / sw)
        j_atr = names.index("atr_pct") if "atr_pct" in names else None
        atr_med = float(np.nanmedian(feat[:, j_atr])) if j_atr is not None else float("nan")
        be = float(s) / (float(t) + float(s))
        out = [f"## MOVE {side.upper()}: +{t} ATR before -{s} ATR within {h} bars (median ATR {100 * atr_med:.2f}% of "
               f"price -> target ~{100 * atr_med * float(t):.1f}%, stop ~{100 * atr_med * float(s):.1f}%). {N:,} bars, "
               f"base hit rate {p0:.1%}, avg net {100 * n0:+.3f}%; break-even hit rate ~{be:.0%} before costs."]
        # quintiles / yes-no per feature
        bins = np.full(feat.shape, -1, dtype=np.int8)
        edges: dict[int, np.ndarray] = {}
        rows = []
        for j, name in enumerate(names):
            x = feat[:, j]
            ok = np.isfinite(x)
            if ok.sum() < 1000:
                continue
            binary = bool(np.isin(np.unique(x[ok]), (0.0, 1.0)).all())
            qs = np.array([0.5]) if binary else np.unique(np.quantile(x[ok], [0.2, 0.4, 0.6, 0.8]))
            edges[j] = qs
            b = np.searchsorted(qs, x[ok], side="right").astype(np.int8)
            bins[ok, j] = b
            cells, lifts = [], []
            for q in range(len(qs) + 1):
                sel = np.zeros(N, bool)
                sel[ok] = b == q
                ws = w[sel].sum()
                if ws <= 0:
                    continue
                hr = float((w[sel] * hit[sel]).sum() / ws)
                lifts.append(hr / p0 if p0 > 0 else 0)
                cells.append(f"{self._rng(qs, q)} {hr:.0%}")
            rows.append((max(lifts) if lifts else 0, f"- {name}: " + " | ".join(cells)))
        rows.sort(key=lambda r: -r[0])
        out.append("Hit rate by feature range (largest lift first):")
        out += [r[1] for r in rows[:8]]
        min_n = max(int(self.d["min_cell_trades"]), int(self.d["min_cell_share"] * N))
        J = sorted(edges)
        top = [j for j in J if names[j] in {r[1].split(":")[0][2:] for r in rows[:12]}]
        for k, cols, lim in ((2, J, 8), (3, top, 6)):
            cells = self._combos(k, cols, bins, edges, hit, net, w, names, min_n)
            if cells:
                out.append(f"Best {k}-condition combinations (>= {min_n:,} bars): hit rate / lift / net / share")
                out += [f"+ {c[4]}: {c[0]:.0%} / x{c[0] / p0:.1f} / {100 * c[1]:+.2f}% / {c[2]:.1%}" for c in cells[:lim]]
        out += self._tree_rules(feat, hit, net, w, names, edges, p0, min_n)
        out.append("")
        return out

    def _combos(self, k: int, cols: list[int], bins: np.ndarray, edges: Mapping[int, np.ndarray], hit: np.ndarray,
                net: np.ndarray, w: np.ndarray, names: list[str], min_n: int) -> list[tuple]:
        from itertools import combinations
        N = len(hit)
        cells = []
        for combo in combinations(cols, k):
            ok = np.all(bins[:, list(combo)] >= 0, axis=1)
            if not ok.any():
                continue
            idx = np.zeros(int(ok.sum()), np.int32)
            for c in combo:
                idx = idx * 5 + bins[ok, c].astype(np.int32)
            size = 5 ** k
            cnt = np.bincount(idx, minlength=size)
            sw_ = np.bincount(idx, weights=w[ok], minlength=size)
            sh = np.bincount(idx, weights=w[ok] * hit[ok], minlength=size)
            sn = np.bincount(idx, weights=w[ok] * net[ok], minlength=size)
            for cell in np.flatnonzero(cnt >= min_n):
                if sw_[cell] <= 0:
                    continue
                qs, rest = [], int(cell)
                for _ in combo:
                    rest, q = divmod(rest, 5)
                    qs.append(q)
                qs = qs[::-1]
                desc = " AND ".join(f"{names[c]} {self._rng(edges[c], q)}" for c, q in zip(combo, qs))
                cells.append((sh[cell] / sw_[cell], sn[cell] / sw_[cell], cnt[cell] / N, cnt[cell], desc))
        cells.sort(key=lambda r: -r[0])
        return cells

    def _tree_rules(self, feat: np.ndarray, hit: np.ndarray, net: np.ndarray, w: np.ndarray, names: list[str],
                    edges: Mapping[int, np.ndarray], p0: float, min_n: int) -> list[str]:
        """Readable 'if A and B and C' rules from a small recency-weighted decision tree (leaves above base rate)."""
        try:
            from sklearn.tree import DecisionTreeClassifier
        except ImportError:
            return []
        X = feat.copy()
        med = np.nanmedian(X, axis=0)
        bad = ~np.isfinite(X)
        X[bad] = np.take(np.nan_to_num(med), np.nonzero(bad)[1])
        clf = DecisionTreeClassifier(max_depth=4, min_samples_leaf=min_n, random_state=0)
        clf.fit(X, hit.astype(int), sample_weight=w)
        tr = clf.tree_
        leaf_of = clf.apply(X)
        binary = {j for j, e in edges.items() if len(e) == 1 and e[0] == 0.5}
        rules = []

        def walk(node: int, bounds: dict[int, list[float]]) -> None:
            if tr.children_left[node] == -1:
                sel = leaf_of == node
                ws = w[sel].sum()
                if ws <= 0:
                    return
                hr = float((w[sel] * hit[sel]).sum() / ws)
                if hr >= 1.3 * p0:
                    rules.append((hr, float((w[sel] * net[sel]).sum() / ws), int(sel.sum()), dict(bounds)))
                return
            j, thr = int(tr.feature[node]), float(tr.threshold[node])
            lo, hi = bounds.get(j, [-np.inf, np.inf])
            walk(int(tr.children_left[node]), {**bounds, j: [lo, min(hi, thr)]})
            walk(int(tr.children_right[node]), {**bounds, j: [max(lo, thr), hi]})

        walk(0, {})
        rules.sort(key=lambda r: -r[0])
        out = []
        for hr, nt, n, b in rules[:6]:
            conds = []
            for j, (lo, hi) in b.items():
                nm = names[j]
                if j in binary:
                    conds.append(nm if lo >= 0.5 else f"not {nm}")
                elif np.isfinite(lo) and np.isfinite(hi):
                    conds.append(f"{nm} > {_q(lo)} and {nm} <= {_q(hi)}")
                elif np.isfinite(lo):
                    conds.append(f"{nm} > {_q(lo)}")
                else:
                    conds.append(f"{nm} <= {_q(hi)}")
            out.append(f"* IF {' and '.join(conds)}: hit rate {hr:.0%} (x{hr / p0:.1f}), net {100 * nt:+.2f}%, {n:,} bars")
        return (["Decision-tree rules (DSL-ready; feature names are replaced by their formulas):"] + out) if out else []

    # -------------------------------------------------------------- exits = the move
    def exit_text(self) -> str:
        defs = "; ".join(f"target_atr {m['t']:g}, stop_atr {m['s']:g}, max_hold_bars {m['h']}" for m in self.moves)
        return (f"Exit = the MOVE: target target_atr x ATR(14) above entry, stop stop_atr x ATR(14) below (short: "
                f"mirrored), time exit after max_hold_bars{' (and 15:15 square-off, no entries after 14:45)' if self.intraday else ''}. "
                f"Use one of the move definitions ({defs}) or adjust within stop_atr 0.5-5, target_atr 0.5-10, "
                "max_hold_bars 2-120; optional trail_atr 1-6 (trailing stop below the highest price since entry); "
                'optional "when" exit rule.')

    def schema(self) -> str:
        return SCHEMA

    def exit_desc(self, ex: Mapping[str, Any]) -> str:
        return (f"exit target {ex.get('target_atr')} ATR, stop {ex.get('stop_atr')} ATR, hold {ex.get('max_hold_bars')}"
                + (f", trail {ex['trail_atr']} ATR" if ex.get("trail_atr") else "")
                + (f", when {ex['when']}" if ex.get("when") else ""))

    def build_exit(self, e: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        m0 = self.moves[0]
        ex: dict[str, Any] = {}
        for key, dflt in (("target_atr", m0["t"]), ("stop_atr", m0["s"])):
            lo, hi = ATR_RANGES[key]
            try:
                ex[key] = round(min(max(float(e.get(key) or dflt), lo), hi), 2)
            except (TypeError, ValueError):
                ex[key] = float(dflt)
        lo, hi = tl.EXIT_RANGES["max_hold_bars"]
        try:
            ex["max_hold_bars"] = int(min(max(int(e.get("max_hold_bars") or m0["h"]), lo), hi))
        except (TypeError, ValueError):
            ex["max_hold_bars"] = int(m0["h"])
        if e.get("trail_atr") not in (None, "", "null"):
            lo, hi = tl.EXIT_RANGES["trail_atr"]
            try:
                ex["trail_atr"] = round(min(max(float(e["trail_atr"]), lo), hi), 2)
            except (TypeError, ValueError):
                pass
        return ex, ""

    def sweep_exits(self, i: Mapping[str, Any], es: Mapping[str, Any]):
        for tm in (0.6, 1.0, 1.5):
            for sm in (0.75, 1.0, 1.33):
                for trv in es.get("trail_atr", [None]):
                    ex = json.loads(json.dumps(i["exit"]))
                    ex["target_atr"] = round(min(max(float(i["exit"]["target_atr"]) * tm, 0.5), 10.0), 2)
                    ex["stop_atr"] = round(min(max(float(i["exit"]["stop_atr"]) * sm, 0.5), 5.0), 2)
                    if trv is None:
                        ex.pop("trail_atr", None)
                    else:
                        ex["trail_atr"] = trv
                    yield (ex, f"X{ex['target_atr']:g}-{ex['stop_atr']:g}" + (f"-T{trv:g}" if trv else ""),
                           f"target {ex['target_atr']:g} ATR, stop {ex['stop_atr']:g} ATR"
                           + (f", trail {trv:g} ATR" if trv else ""))
