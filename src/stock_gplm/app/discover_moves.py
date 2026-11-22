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
2b. Per stock, then combined (owner 2026-11-22: "find what works for each stock, then combine if the same thing
   works on multiple stocks"): for every stock separately, condition pairs followed by the move far more often than
   that stock's own base rate; then how many stocks share each rule vs how many pure chance would give. Recurring
   rules (TUNE only) go to the LLM; STOCK_RULES.md / stock_rules.json add each rule's result on those stocks' unseen
   CONFIRM bars (owner report only, never shown to the LLM).
3. The LLM turns the best rules into strategies whose exit IS the move (target T ATR, stop S ATR, H bars); the
   usual rounds, exit sweep and CONFIRM gate follow (discover.py). Ids MOVE-<tf>-<Name>.
"""
from __future__ import annotations

import json
import os
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


# ------------------------------------------------------------------ parallel helpers (owner 2026-11-22: use all cores)
_G: dict[str, Any] = {}


def _init_worker(shared: Mapping[str, Any]) -> None:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    _G.clear()
    _G.update(shared)


def _run_parallel(fn: Any, shared: Mapping[str, Any], tasks: list) -> list:
    """fn(task) for every task in a process pool; the big arrays in `shared` go to each worker once. Falls back to
    one core if the pool fails, so a run never dies because of the parallel step."""
    from concurrent.futures import ProcessPoolExecutor
    nw = max(1, min(20, (os.cpu_count() or 4) - 4, len(tasks)))
    if nw > 1:
        try:
            with ProcessPoolExecutor(nw, initializer=_init_worker, initargs=(shared,)) as ex:
                return list(ex.map(fn, tasks))
        except Exception as exc:  # noqa: BLE001 - fall back to the serial path
            print(f"parallel step failed ({type(exc).__name__}: {str(exc)[:160]}); running on one core", flush=True)
    _init_worker(shared)
    try:
        return [fn(t) for t in tasks]
    finally:
        _G.clear()


def _chunks(items: list, n: int) -> list[list]:
    n = max(1, min(n, len(items)))
    return [items[i::n] for i in range(n)]


def _combo_chunk(task: tuple) -> list[tuple]:
    """Condition combinations: (hit rate, avg net, share, bars, cols, bins) for cells with >= min_n bars."""
    combos, k, min_n = task
    bins, hit, net, w = _G["bins"], _G["hit"], _G["net"], _G["w"]
    N = len(hit)
    out = []
    for combo in combos:
        cols = list(combo)
        ok = np.all(bins[:, cols] >= 0, axis=1)
        if not ok.any():
            continue
        idx = np.zeros(int(ok.sum()), np.int64)
        for c in cols:
            idx = idx * 5 + bins[ok, c].astype(np.int64)
        size = 5 ** k
        cnt = np.bincount(idx, minlength=size)
        sw_ = np.bincount(idx, weights=w[ok], minlength=size)
        sh = np.bincount(idx, weights=w[ok] * hit[ok], minlength=size)
        sn = np.bincount(idx, weights=w[ok] * net[ok], minlength=size)
        for cell in np.flatnonzero(cnt >= min_n):
            if sw_[cell] <= 0:
                continue
            qs, rest = [], int(cell)
            for _ in cols:
                rest, q = divmod(rest, 5)
                qs.append(q)
            out.append((float(sh[cell] / sw_[cell]), float(sn[cell] / sw_[cell]), float(cnt[cell] / N), int(cnt[cell]),
                        tuple(cols), tuple(qs[::-1])))
    return out


def _stock_chunk(task: tuple) -> dict[str, Any]:
    """Per-stock condition-pair rules for a chunk of feature pairs (see MoveDiscoverer._stock_rules)."""
    pairs, mb, mh, lift, min_stocks = task
    bins, hit, net, st = _G["bins"], _G["hit"], _G["net"], _G["st"]
    tune, conf, per, S, P, base_t = _G["tune"], _G["conf"], _G["per"], _G["S"], _G["P"], _G["base_t"]
    res: dict[str, Any] = {"n_pass": 0, "n_elig": 0, "elig_s": np.zeros(S, np.int64), "per_stock": [], "found": []}
    for a, b in pairs:
        ok = (bins[:, a] >= 0) & (bins[:, b] >= 0)
        key = st * 25 + bins[:, a].astype(np.int64) * 5 + bins[:, b]

        def agg(sel: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
            k = key[sel]
            return (np.bincount(k, minlength=S * 25).reshape(S, 25),
                    np.bincount(k, weights=hit[sel], minlength=S * 25).reshape(S, 25),
                    np.bincount(k, weights=net[sel], minlength=S * 25).reshape(S, 25))
        n_t, h_t, s_t = agg(ok & tune)
        hr_t = h_t / np.maximum(n_t, 1)
        elig = n_t >= mb
        ok_pass = (elig & (h_t >= mh) & (hr_t >= np.maximum(lift * base_t[:, None], base_t[:, None] + 0.10))
                   & (s_t > 0))
        res["n_pass"] += int(ok_pass.sum())
        res["n_elig"] += int(elig.sum())
        res["elig_s"] += elig.sum(axis=1)
        if not ok_pass.any():
            continue
        cnt = ok_pass.sum(axis=0)
        n_c, h_c, s_c = agg(ok & conf)
        # consistency = periods (years daily / months intraday, >= 3 bars) in which the rule beat the stock's base
        selp = ok & tune
        kp = key[selp] * P + per[selp]
        pn = np.bincount(kp, minlength=S * 25 * P).reshape(S, 25, P)
        ph = np.bincount(kp, weights=hit[selp], minlength=S * 25 * P).reshape(S, 25, P)
        for s_, c_ in zip(*np.nonzero(ok_pass)):
            okp = pn[s_, c_] >= 3
            good = int((ph[s_, c_][okp] / pn[s_, c_][okp] > base_t[s_]).sum())
            res["per_stock"].append((int(s_), (a, b, int(c_), int(n_t[s_, c_]), float(h_t[s_, c_]), float(s_t[s_, c_]),
                                               int(okp.sum()), good, int(n_c[s_, c_]), float(h_c[s_, c_]),
                                               float(s_c[s_, c_]), int(cnt[c_]))))
        for c in np.flatnonzero(cnt >= min_stocks):
            stocks = np.flatnonzero(ok_pass[:, c])
            res["found"].append({"a": a, "b": b, "qa": int(c) // 5, "qb": int(c) % 5, "stocks": stocks,
                                 "elig": int(elig[:, c].sum()), "n_t": int(n_t[stocks, c].sum()),
                                 "h_t": float(h_t[stocks, c].sum()), "base_t": float(base_t[stocks].mean()),
                                 "n_c": n_c[stocks, c], "h_c": h_c[stocks, c], "s_c": s_c[stocks, c]})
    return res


class MoveDiscoverer(Discoverer):
    def __init__(self, tf: str, name: str, cfg: Mapping[str, Any], stocks_arg: str | None, workers: int):
        super().__init__(tf, name, cfg, stocks_arg, workers)
        self.prefix = "MOVE"
        self.moves = ((cfg.get("discover") or {}).get("moves") or MOVES_DEFAULT).get(tf) or MOVES_DEFAULT[tf]
        self.sr = {"min_bars": 40, "min_hits": 8, "lift": 1.5, "min_stocks": 5,
                   **((cfg.get("discover") or {}).get("stock_rules") or {})}
        self._edges: dict[int, np.ndarray] = {}

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
                                         True, {"h": int(mv["h"]), "t": float(mv["t"]), "s": float(mv["s"])}))
        self.log(f"moves: labelling every liquid bar for {len(probes)} move definitions {self.moves} {dirs}")
        res = self.evaluate(probes)
        names = te.feature_names(self.intraday)
        exprs = te.feature_exprs(self.intraday)
        L = [f"MOVE FACTS {self.tf} - {len(self.stocks)} liquid NSE stocks, tuning window {self.t['tune_start']} .. "
             f"{self.t['confirm_start']} (recent bars weigh more). A MOVE = price reaches +T x ATR(14) before -S x "
             "ATR(14) within H bars (short: mirrored). Every liquid bar is labelled hit / miss; 'hit rate' = how often a "
             "condition was FOLLOWED by a move (precision); lift = hit rate / base rate; net = average result of that "
             "trade after costs. Event features: yes / no.",
             "Feature DSL: " + "; ".join(f"{k} = {exprs[k]}" for k in names),
             "Build entries from the HIGHEST hit-rate rules (tree rules, combinations); the hit rate must be clearly "
             "above the break-even hit rate. Use the move's target / stop / H as the exit.", ""]
        report, rules_json = [f"# Per-stock move rules {self.tf} ({self.name})", "",
                              "Each rule was found SEPARATELY on each stock (TUNE window: hit rate >= "
                              f"{self.sr['lift']}x and >= +10 points over that stock's own base rate, >= "
                              f"{self.sr['min_bars']} bars, >= {self.sr['min_hits']} hits, positive net). 'Chance' = "
                              "how many stocks would show it by luck at the overall pass rate. CONFIRM = the same rule "
                              "on the same stocks' unseen bars (never shown to the LLM). Holds = share of those stocks "
                              "whose CONFIRM hit rate beat their own TUNE base rate (>= 5 confirm bars).", ""], {}
        per_stock_md = [f"# Per-stock move rules {self.tf} ({self.name}) - every stock's own rules", ""]
        for v in probes:
            raw = res[v.vid].get("raw")
            if raw is None or "feat" not in raw:
                continue
            day = raw["day"].astype(np.int64)
            m = (day >= self.tune_day) & (day < self.confirm_day)
            if m.sum() < 1000:
                continue
            hit = (raw["reason"][m] == 1).astype(float)  # target reached before stop
            net, feat = raw["net"][m].astype(float), raw["feat"][m].astype(float)
            w = te.weights(day[m], self.confirm_day - 1, float(self.t["decay"]))
            label = v.vid.split("~")[1]
            L += self._move_facts(label, hit, net, feat, w, names)
            facts_lines, rep_lines, js, ps_lines = self._stock_rules(label, raw, m, day >= self.confirm_day, names)
            L += facts_lines + [""]
            report += rep_lines
            per_stock_md += ps_lines
            rules_json[label] = js
        (self.root / "STOCK_RULES.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        (self.root / "stock_rules.json").write_text(json.dumps(rules_json, indent=1), encoding="utf-8")
        (self.root / "PER_STOCK_RULES.md").write_text("\n".join(per_stock_md) + "\n", encoding="utf-8")
        self.log(f"per-stock rules: {self.root / 'STOCK_RULES.md'}")
        L = self.used_dsl_line(L, names, exprs)
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
        self._edges = edges
        return out

    def _cond(self, names: list[str], j: int, q: int) -> str:
        """DSL-ready condition for quintile / yes-no bin q of feature j (bins: qs[q-1] <= x < qs[q])."""
        qs, nm = self._edges[j], names[j]
        if len(qs) == 1 and qs[0] == 0.5:
            return nm if q == 1 else f"not {nm}"
        parts = ([f"{nm} >= {_q(qs[q - 1])}"] if q > 0 else []) + ([f"{nm} < {_q(qs[q])}"] if q < len(qs) else [])
        return " and ".join(parts)

    def _stock_rules(self, label: str, raw: Mapping[str, np.ndarray], tune: np.ndarray, conf: np.ndarray,
                     names: list[str]) -> tuple[list[str], list[str], dict]:
        """Per-stock condition-pair rules, then how many stocks share each rule (vs chance) and how they did on
        CONFIRM. Bins = the pooled TUNE quintiles, so a rule means the same thing on every stock."""
        edges = self._edges
        feat = raw["feat"].astype(float)
        hit = (raw["reason"] == 1).astype(float)
        net = raw["net"].astype(float)
        st = raw["stock"].astype(np.int64)
        S = int(st.max()) + 1 if len(st) else 0
        bins = np.full(feat.shape, -1, dtype=np.int8)
        for j, qs in edges.items():
            ok = np.isfinite(feat[:, j])
            bins[ok, j] = np.searchsorted(qs, feat[ok, j], side="right")
        pid = te.period_of(raw["day"].astype(np.int64), "month" if self.intraday else "year")
        per = (pid - pid.min()).astype(np.int64)
        P = int(per.max()) + 1 if len(per) else 1
        per_stock: dict[int, list[tuple]] = {}
        elig_s = np.zeros(S, np.int64)
        nb_t = np.bincount(st[tune], minlength=S)
        base_t = np.bincount(st[tune], weights=hit[tune], minlength=S) / np.maximum(nb_t, 1)
        mb, mh, lift = int(self.sr["min_bars"]), int(self.sr["min_hits"]), float(self.sr["lift"])
        J = sorted(edges)
        pairs = [(a, b) for ia, a in enumerate(J) for b in J[ia + 1:]]
        shared = {"bins": bins, "hit": hit, "net": net, "st": st, "tune": tune, "conf": conf, "per": per, "S": S, "P": P,
                  "base_t": base_t}
        tasks = [(ch, mb, mh, lift, int(self.sr["min_stocks"]))
                 for ch in _chunks(pairs, 4 * max(1, min(20, (os.cpu_count() or 4) - 4)))]
        found, n_pass, n_elig = [], 0, 0
        for part in _run_parallel(_stock_chunk, shared, tasks):
            n_pass += part["n_pass"]
            n_elig += part["n_elig"]
            elig_s += part["elig_s"]
            for si, item in part["per_stock"]:
                per_stock.setdefault(si, []).append(item)
            found += part["found"]
        p_pass = n_pass / n_elig if n_elig else 0.0
        for f in found:
            f["chance"] = p_pass * f["elig"]
            f["ratio"] = len(f["stocks"]) / max(f["chance"], 0.5)
        keep = [f for f in found if len(f["stocks"]) >= max(int(self.sr["min_stocks"]), 3 * f["chance"])]
        keep.sort(key=lambda f: (-f["ratio"], -len(f["stocks"])))
        side = label.split("-")[0]
        facts = [f"Rules that RECUR across stocks ({side} move {label}; found separately on each stock; overall per-stock "
                 f"pass rate {p_pass:.2%}):"]
        rep_lines = [f"## {label}: {len(keep)} recurring rules (of {len(found)} shared by >= {self.sr['min_stocks']} "
                     f"stocks; per-stock pass rate {p_pass:.2%})", "",
                     "| Rule (DSL) | Stocks | Chance | TUNE hit / their base | CONFIRM bars / hit / avg net | Holds |",
                     "|---|---|---|---|---|---|"]
        js = []
        for f in keep[:25]:
            rule = f"{self._cond(names, f['a'], f['qa'])} and {self._cond(names, f['b'], f['qb'])}"
            ns, ht = len(f["stocks"]), f["h_t"] / max(f["n_t"], 1)
            nc, hc, sc = int(f["n_c"].sum()), float(f["h_c"].sum()), float(f["s_c"].sum())
            okc = f["n_c"] >= 5
            holds = (f["h_c"][okc] / f["n_c"][okc] > base_t[f["stocks"][okc]]).mean() if okc.any() else float("nan")
            syms = [self.stocks[i] for i in f["stocks"] if i < len(self.stocks)]
            if len(facts) <= 8:
                facts.append(f"+ {rule}: {ns} stocks (chance ~{f['chance']:.1f}), hit rate {ht:.0%} on them vs their "
                             f"base {f['base_t']:.0%}")
            rep_lines.append(f"| {rule} | {ns} | {f['chance']:.1f} | {ht:.0%} / {f['base_t']:.0%} | "
                             f"{nc} / {(hc / nc if nc else float('nan')):.0%} / {(100 * sc / nc if nc else float('nan')):+.2f}% | "
                             f"{holds:.0%} |")
            js.append({"rule": rule, "stocks": syms, "chance": round(f["chance"], 2), "tune_hit": round(ht, 4),
                       "tune_base": round(f["base_t"], 4), "confirm_bars": nc,
                       "confirm_hit": round(hc / nc, 4) if nc else None,
                       "confirm_avg_net": round(sc / nc, 5) if nc else None,
                       "holds": None if np.isnan(holds) else round(float(holds), 3)})
        if len(facts) == 1:
            facts.append("- none beyond chance")
        rep_lines.append("")
        ps_lines, ps_json = self._per_stock(label, per_stock, base_t, elig_s, p_pass, names)
        return facts, rep_lines, {"pass_rate": p_pass, "rules": js, "per_stock": ps_json}, ps_lines

    def _per_stock(self, label: str, per_stock: Mapping[int, list[tuple]], base_t: np.ndarray, elig_s: np.ndarray,
                   p_pass: float, names: list[str]) -> tuple[list[str], dict]:
        """Per stock: its rules ranked by cautious extra hit rate (lower 20% estimate minus the stock's base rate),
        with lift, consistency, how many other stocks share the rule (a bonus, not a requirement) and CONFIRM."""
        from scipy.stats import beta
        ranked = []
        for si, items in per_stock.items():
            base = float(base_t[si])
            rows = []
            for a, b, c, n, h, sn, np_, good, nc, hc, sc, also in items:
                low = float(beta.ppf(0.2, h + 1, n - h + 1))
                rows.append({"rule": f"{self._cond(names, a, c // 5)} and {self._cond(names, b, c % 5)}", "bars": n,
                             "hit": h / n, "lift": (h / n) / base if base > 0 else 0.0, "hit_low": low,
                             "extra_low": low - base, "avg_net": sn / n, "periods": np_, "periods_good": good,
                             "also_on": also - 1, "confirm_bars": nc, "confirm_hit": hc / nc if nc else None,
                             "confirm_avg_net": sc / nc if nc else None})
            rows.sort(key=lambda r: (-r["extra_low"], -r["periods_good"]))
            sym = self.stocks[si] if si < len(self.stocks) else str(si)
            ranked.append((rows[0]["extra_low"], sym, base, len(items), p_pass * float(elig_s[si]), rows[:10]))
        ranked.sort(key=lambda r: -r[0])
        per = "year" if not self.intraday else "month"
        L = [f"## {label}", "", f"Per stock, best first. Rules ranked by CAUTIOUS extra hit rate (lower 20% estimate "
             f"minus the stock's own base rate). Consistent = {per}s (with >= 3 bars) in which the rule beat the base. "
             "Also on = other stocks where the same rule passed (a bonus, not required). Luck = rules this stock would "
             "pass by chance at the overall pass rate - if 'rules passed' is not well above it, treat its list as noise. "
             "CONFIRM = the stock's unseen bars.", ""]
        js = {}
        for best, sym, base, npass, luck, rows in ranked:
            L += [f"### {sym}: base hit rate {base:.0%}, {npass} rules passed (luck ~{luck:.0f})", "",
                  f"| Rule | Bars | Hit | Lift | Cautious hit | Consistent | Also on | CONFIRM bars / hit / avg net |",
                  "|---|---|---|---|---|---|---|---|"]
            for r in rows[:5]:
                cf = (f"{r['confirm_bars']} / {r['confirm_hit']:.0%} / {100 * r['confirm_avg_net']:+.2f}%"
                      if r["confirm_bars"] else "0")
                L.append(f"| {r['rule']} | {r['bars']} | {r['hit']:.0%} | x{r['lift']:.1f} | {r['hit_low']:.0%} | "
                         f"{r['periods_good']} of {r['periods']} | {r['also_on']} | {cf} |")
            L.append("")
            js[sym] = {"base_hit": round(base, 4), "rules_passed": npass, "luck": round(luck, 1),
                       "rules": [{k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]}
        return L, js

    def _combos(self, k: int, cols: list[int], bins: np.ndarray, edges: Mapping[int, np.ndarray], hit: np.ndarray,
                net: np.ndarray, w: np.ndarray, names: list[str], min_n: int) -> list[tuple]:
        from itertools import combinations
        combos = list(combinations(cols, k))
        if not combos:
            return []
        tasks = [(ch, k, min_n) for ch in _chunks(combos, 4 * max(1, min(20, (os.cpu_count() or 4) - 4)))]
        parts = _run_parallel(_combo_chunk, {"bins": bins, "hit": hit, "net": net, "w": w}, tasks)
        cells = []
        for part in parts:
            for hr, nt, share, n, cc, qs in part:
                desc = " AND ".join(f"{names[c]} {self._rng(edges[c], q)}" for c, q in zip(cc, qs))
                cells.append((hr, nt, share, n, desc))
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
