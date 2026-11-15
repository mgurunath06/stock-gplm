"""Machine-generated candidate rules: batched (GPU when available) screening plus exact confirmation.

A search runs on the rows whose forward outcome had **completed before the cutoff**:

1. thresholds are training-window quantiles of every feature (``gt`` above upper quantiles,
   ``lt`` below lower ones; binary features use ``== 1``);
2. every single condition and every AND-pair of the strongest singles (different indicator
   families) is scored at every rule horizon in one batched matrix product
   (:func:`stock_gplm.app.compute.score_masks`): count, mean, deviation and win rate of the net
   forward return (round-trip cost already subtracted);
3. the overlap-adjusted t statistic uses ``n_eff = n / h`` because h-bar labels overlap; one-sided
   p-values are corrected by Benjamini-Hochberg across **every** (candidate, horizon) test of the
   search, and the number of tests is recorded;
4. the strongest survivors are confirmed with the exact, non-overlapping M5.4 trade simulator
   (stop, target and time exit, costs) on the same training rows.

Survivors with ``q <= fdr_q`` are ``FDR_SELECTED``. When none survive, the best few are kept as
``EXPLORATORY_UNCORRECTED`` so their *out-of-sample* behaviour is still measured, but they are never
actionable. In-sample statistics are reported as model-selection performance only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as sps

from ..m54.backtest import ExitPolicy, bh_adjust, simulate
from .compute import Backend, forward_matrix, pair_masks, score_masks, threshold_masks
from .db import sha
from .lanes import Lane


@dataclass
class Rule:
    conditions: list[dict[str, Any]]
    horizon: int
    exit: dict[str, Any]
    tier: str  # FDR_SELECTED | EXPLORATORY_UNCORRECTED
    in_sample: dict[str, Any] = field(default_factory=dict)

    @property
    def rule_id(self) -> str:
        return "rule-" + sha({"c": self.conditions, "h": self.horizon, "x": self.exit})[:16]

    def text(self) -> str:
        sym = {"gt": ">", "lt": "<"}
        return " AND ".join(f"{c['feature']} == 1" if c["op"] == "is" else f"{c['feature']} {sym[c['op']]} {c['threshold']:.6g}"
                            for c in self.conditions)

    def mask(self, lane: Lane) -> np.ndarray:
        m = np.ones(len(lane.market), dtype=bool)
        for c in self.conditions:
            if c["feature"] not in lane.names:
                return np.zeros(len(lane.market), dtype=bool)
            x = lane.X[:, lane.names.index(c["feature"])]
            with np.errstate(invalid="ignore"):
                if c["op"] == "is":
                    m &= x > 0.5
                else:
                    m &= (x > c["threshold"]) if c["op"] == "gt" else (x < c["threshold"])
            m &= np.isfinite(x)
        return m & ~lane.market.no_signal

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "text": self.text(), "conditions": self.conditions, "horizon": self.horizon,
                "exit": self.exit, "tier": self.tier, "in_sample": self.in_sample}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "Rule":
        return Rule(list(d["conditions"]), int(d["horizon"]), dict(d["exit"]), str(d["tier"]), dict(d.get("in_sample", {})))


def _singles(lane: Lane, train: slice, quantiles: Sequence[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    feats, thr, gt, labels = [], [], [], []
    for j, name in enumerate(lane.names):
        x = lane.X[train, j]
        x = x[np.isfinite(x)]
        if x.size < 50 or np.std(x) == 0:
            continue
        kind = lane.defs[name].kind if name in lane.defs else "signed"
        if kind == "binary" or set(np.unique(x)).issubset({0.0, 1.0}):
            feats.append(j)
            thr.append(0.5)
            gt.append(True)
            labels.append("is")
            continue
        for q in quantiles:
            feats.append(j)
            thr.append(float(np.quantile(x, q)))
            gt.append(q >= 0.5)
            labels.append(f"q{q:g}")
    return np.array(feats, dtype=np.int64), np.array(thr), np.array(gt, dtype=bool), labels


def search(backend: Backend, lane: Lane, last_label_idx: int, horizons: Sequence[int], cfg: Mapping[str, Any],
           start_idx: int = 0) -> dict[str, Any]:
    """Screen candidate rules on rows whose outcome completed by ``last_label_idx`` (inclusive)."""
    m = lane.market
    T = len(m)
    hs = [h for h in horizons if h < T]
    cost = lane.costs.buy + lane.costs.sell
    R, V = forward_matrix(m.open, m.close, hs, m.session_id, m.intraday, cost)
    for j, h in enumerate(hs):  # only labels that had completed by the cutoff
        V[max(0, last_label_idx - h + 1):, j] = False
        V[:start_idx, j] = False
    V &= ~m.no_signal[:, None]
    train_end = last_label_idx + 1
    singles_f, singles_t, singles_gt, labels = _singles(lane, slice(start_idx, train_end), cfg["quantiles"])
    if singles_f.size == 0:
        return {"tests": 0, "rules": [], "note": "no usable features"}
    masks_s = threshold_masks(backend, lane.X, singles_f, singles_t, singles_gt)
    sc = score_masks(backend, masks_s, R, V)
    n_eff = sc["n"] / np.array(hs)[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        tstat = sc["mean"] / (sc["std"] / np.sqrt(np.maximum(n_eff, 1)))
    tstat = np.where(n_eff >= cfg["min_trades"], tstat, np.nan)
    # strongest singles -> pairs across different families
    best = np.nanmax(np.where(np.isfinite(tstat), tstat, -np.inf), axis=1)
    order = np.argsort(-best)
    fam = [lane.defs[lane.names[f]].family if lane.names[f] in lane.defs else "?" for f in singles_f]
    top = [int(i) for i in order[: int(cfg["top_singles_for_pairs"])] if np.isfinite(best[i])]
    pairs = [(a, b) for k, a in enumerate(top) for b in top[k + 1:]
             if singles_f[a] != singles_f[b] and fam[a] != fam[b]][: int(cfg["max_pairs"])]
    if pairs:
        P = np.array(pairs, dtype=np.int64)
        masks_p = pair_masks(backend, masks_s, P)
        sp = score_masks(backend, masks_p, R, V)
        n_eff_p = sp["n"] / np.array(hs)[None, :]
        with np.errstate(invalid="ignore", divide="ignore"):
            tp = sp["mean"] / (sp["std"] / np.sqrt(np.maximum(n_eff_p, 1)))
        tp = np.where(n_eff_p >= cfg["min_trades"], tp, np.nan)
    else:
        P, tp, sp, n_eff_p = np.zeros((0, 2), dtype=np.int64), np.zeros((0, len(hs))), None, np.zeros((0, len(hs)))
    all_t = np.vstack([tstat, tp]) if len(tp) else tstat
    all_neff = np.vstack([n_eff, n_eff_p]) if len(tp) else n_eff
    flat_t = all_t.ravel()
    flat_df = np.maximum(all_neff.ravel() - 1, 1)
    pv = np.where(np.isfinite(flat_t), sps.t.sf(np.nan_to_num(flat_t, nan=0.0), flat_df), np.nan)
    tests = int(np.isfinite(pv).sum())
    q = np.array([np.nan if v is None else v for v in bh_adjust([None if not np.isfinite(x) else float(x) for x in pv],
                                                                 max(tests, 1))], dtype=float)
    q = q.reshape(all_t.shape)
    pv = pv.reshape(all_t.shape)

    def conds(ci: int) -> list[dict[str, Any]]:
        idx = [ci] if ci < len(singles_f) else list(P[ci - len(singles_f)])
        out = []
        for s in idx:
            name = lane.names[int(singles_f[s])]
            op = "is" if labels[s] == "is" else ("gt" if singles_gt[s] else "lt")
            out.append({"feature": name, "op": op, "threshold": None if op == "is" else float(singles_t[s]),
                        "label": labels[s]})
        return out

    finite = np.argwhere(np.isfinite(all_t))
    ranked = sorted(((float(all_t[i, j]), int(i), int(j)) for i, j in finite), reverse=True)
    ex = cfg["exit"]
    chosen: list[Rule] = []
    seen: set[str] = set()
    for tval, ci, hj in ranked:
        if len(chosen) >= int(cfg["max_selected"]):
            break
        h = hs[hj]
        rule = Rule(conds(ci), h, {"max_hold_bars": h, "stop_atr": ex.get("stop_atr"), "target_atr": ex.get("target_atr"),
                                   "trail_atr": ex.get("trail_atr")},
                    "FDR_SELECTED" if q[ci, hj] <= float(cfg["fdr_q"]) else "EXPLORATORY_UNCORRECTED")
        if rule.rule_id in seen:
            continue
        mask = rule.mask(lane)
        trades = simulate(m, mask, (start_idx, train_end), ExitPolicy.from_dict(rule.exit), lane.costs)
        nets = [t["net_return"] for t in trades]
        if len(trades) < max(3, int(cfg["min_trades"]) // 2) or np.mean(nets) <= 0:
            continue
        rule.in_sample = {"t_stat": tval, "p_value": float(pv[ci, hj]), "q_bh": float(q[ci, hj]),
                          "screen_n": float(all_neff[ci, hj] * h), "screen_mean_net": float(
                              (sc["mean"] if ci < len(singles_f) else sp["mean"])[ci if ci < len(singles_f)
                                                                                  else ci - len(singles_f), hj]),
                          "exact_trades": len(trades), "exact_mean_net": float(np.mean(nets)),
                          "exact_win_rate": float(np.mean([x > 0 for x in nets])),
                          "exact_total_net": float(np.prod([1 + x for x in nets]) - 1),
                          "train_rows": int(train_end - start_idx)}
        seen.add(rule.rule_id)
        chosen.append(rule)
    fdr = [r for r in chosen if r.tier == "FDR_SELECTED"]
    return {"tests": tests, "singles": int(len(singles_f)), "pairs": int(len(P)), "horizons": hs,
            "fdr_survivors": int(np.nansum(q <= float(cfg["fdr_q"]))), "rules": [r.to_dict() for r in chosen],
            "selected_fdr": len(fdr), "device": backend.name,
            "min_q": float(np.nanmin(q)) if np.isfinite(q).any() else None}

