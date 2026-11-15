"""Declarative hypothesis language and budgeted candidate generation.

A hypothesis is an AND of one or more conditions evaluated at the close of
session ``t`` on causal features. Condition operators:

* ``gt`` / ``lt``           feature above / below a threshold (a state)
* ``cross_above`` / ``cross_below``  feature crosses a threshold between t-1 and t (an event)
* ``is``                     a binary feature equals 1

Thresholds come from (a) quantiles of the feature measured on the *discovery*
segment only, so validation and test data never shape them, or (b) configured
absolute levels for bounded oscillators. No indicator period or level is
preferred in code. Hypotheses supplied from files (user or AI proposals) use the
same language and are validated by the same engine.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .indicators import FeatureDef

OPERATORS = ("gt", "lt", "cross_above", "cross_below", "is")


@dataclass(frozen=True)
class Condition:
    feature: str
    op: str
    threshold: float | None = None
    label: str = ""  # e.g. "q0.90" for provenance; never used in evaluation

    def __post_init__(self) -> None:
        if self.op not in OPERATORS:
            raise ValueError(f"unknown operator {self.op}")
        if (self.op == "is") != (self.threshold is None):
            raise ValueError("'is' takes no threshold; every other operator needs one")

    def to_dict(self) -> dict[str, Any]:
        return {"feature": self.feature, "op": self.op, "threshold": self.threshold, "label": self.label}

    def text(self) -> str:
        if self.op == "is":
            return f"{self.feature} == 1"
        sym = {"gt": ">", "lt": "<", "cross_above": "crosses above", "cross_below": "crosses below"}[self.op]
        tag = f" [{self.label}]" if self.label else ""
        return f"{self.feature} {sym} {self.threshold:.6g}{tag}"


@dataclass(frozen=True)
class Hypothesis:
    conditions: tuple[Condition, ...]
    origin: str = "generated"  # generated | refined | interaction | file:<name>

    @property
    def hypothesis_id(self) -> str:
        payload = json.dumps([c.to_dict() for c in sorted(self.conditions, key=lambda c: (c.feature, c.op, str(c.threshold)))],
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def text(self) -> str:
        return " AND ".join(c.text() for c in self.conditions)

    def to_dict(self) -> dict[str, Any]:
        return {"hypothesis_id": self.hypothesis_id, "origin": self.origin, "text": self.text(),
                "conditions": [c.to_dict() for c in self.conditions]}

    @staticmethod
    def from_dict(raw: Mapping[str, Any], origin: str | None = None) -> "Hypothesis":
        conds = tuple(Condition(str(c["feature"]), str(c["op"]),
                                None if c.get("threshold") is None else float(c["threshold"]), str(c.get("label", "")))
                      for c in raw["conditions"])
        if not conds:
            raise ValueError("a hypothesis needs at least one condition")
        return Hypothesis(conds, origin or str(raw.get("origin", "file")))


def condition_mask(cond: Condition, features: Mapping[str, np.ndarray]) -> np.ndarray:
    """Boolean signal per session; undefined (NaN) values never fire."""
    if cond.feature not in features:
        raise KeyError(f"feature {cond.feature} is not computed in this run")
    x = features[cond.feature]
    with np.errstate(invalid="ignore"):
        if cond.op == "is":
            return x == 1.0
        t = float(cond.threshold)  # type: ignore[arg-type]
        if cond.op == "gt":
            return x > t
        if cond.op == "lt":
            return x < t
        prev = np.concatenate(([np.nan], x[:-1]))
        if cond.op == "cross_above":
            return (prev <= t) & (x > t)
        return (prev >= t) & (x < t)


def signal_mask(h: Hypothesis, features: Mapping[str, np.ndarray]) -> np.ndarray:
    mask = np.ones(len(next(iter(features.values()))), dtype=bool)
    for c in h.conditions:
        mask &= condition_mask(c, features)
    return mask


def _quantile_thresholds(values: np.ndarray, quantiles: Sequence[float]) -> list[tuple[float, str]]:
    finite = values[np.isfinite(values)]
    if finite.size < 30:
        return []
    out: list[tuple[float, str]] = []
    seen: set[float] = set()
    for q in quantiles:
        v = float(np.quantile(finite, q))
        r = float(f"{v:.6g}")
        if r not in seen:
            seen.add(r)
            out.append((r, f"q{q:.2f}"))
    return out


def single_conditions(defs: Mapping[str, FeatureDef], features: Mapping[str, np.ndarray], discovery: slice,
                      quantiles: Sequence[float]) -> list[Hypothesis]:
    """Enumerate single-condition hypotheses in deterministic order."""
    out: list[Hypothesis] = []
    lo_q = [q for q in quantiles if q < 0.5]
    hi_q = [q for q in quantiles if q > 0.5]
    for key in sorted(defs):
        d = defs[key]
        x = features[key][discovery]
        if d.kind == "binary":
            if np.nansum(x) > 0:
                out.append(Hypothesis((Condition(key, "is"),)))
            continue
        if d.kind == "bounded" and d.levels:
            for lv in d.levels:
                lbl = f"level{lv:g}"
                out += [Hypothesis((Condition(key, "gt", lv, lbl),)), Hypothesis((Condition(key, "lt", lv, lbl),)),
                        Hypothesis((Condition(key, "cross_above", lv, lbl),)), Hypothesis((Condition(key, "cross_below", lv, lbl),))]
            continue
        for v, lbl in _quantile_thresholds(x, hi_q):
            out.append(Hypothesis((Condition(key, "gt", v, lbl),)))
        for v, lbl in _quantile_thresholds(x, lo_q):
            out.append(Hypothesis((Condition(key, "lt", v, lbl),)))
        if d.kind == "signed":
            out += [Hypothesis((Condition(key, "cross_above", 0.0, "zero"),)),
                    Hypothesis((Condition(key, "cross_below", 0.0, "zero"),))]
    return out


def budget_sample(candidates: Sequence[Hypothesis], defs: Mapping[str, FeatureDef], limit: int, seed: int
                  ) -> list[Hypothesis]:
    """Deterministic stratified sample: round-robin across indicators so every family is covered."""
    if len(candidates) <= limit:
        return list(candidates)
    by_ind: dict[str, list[Hypothesis]] = {}
    for h in candidates:
        by_ind.setdefault(defs[h.conditions[0].feature].indicator, []).append(h)
    rng = random.Random(seed)
    queues = []
    for name in sorted(by_ind):
        items = list(by_ind[name])
        rng.shuffle(items)
        queues.append(items)
    picked: list[Hypothesis] = []
    while len(picked) < limit:
        progressed = False
        for q in queues:
            if q and len(picked) < limit:
                picked.append(q.pop())
                progressed = True
        if not progressed:
            break
    return sorted(picked, key=lambda h: h.hypothesis_id)


def _neighbour_keys(d: FeatureDef, defs: Mapping[str, FeatureDef], grids: Mapping[str, Mapping[str, list[Any]]]) -> list[str]:
    """Features of the same indicator/output whose parameters differ by one grid step in one dimension."""
    grid = grids.get(d.indicator, {}).get("params", {})
    params = dict(d.params)
    out = []
    for other in defs.values():
        if other.indicator != d.indicator or other.output != d.output or other.key == d.key:
            continue
        op = dict(other.params)
        diffs = [k for k in params if op.get(k) != params[k]]
        if len(diffs) != 1:
            continue
        k = diffs[0]
        values = list(grid.get(k, []))
        if params[k] in values and op[k] in values and abs(values.index(params[k]) - values.index(op[k])) == 1:
            out.append(other.key)
    return sorted(out)


def neighbours(h: Hypothesis, defs: Mapping[str, FeatureDef], features: Mapping[str, np.ndarray], discovery: slice,
               grids: Mapping[str, Mapping[str, Any]], quantiles: Sequence[float]) -> list[Hypothesis]:
    """Parameter neighbours of a single-condition hypothesis (adjacent periods and adjacent thresholds)."""
    if len(h.conditions) != 1:
        return []
    c = h.conditions[0]
    d = defs[c.feature]
    out: list[Hypothesis] = []
    # Same threshold rule applied to neighbouring periods.
    for key in _neighbour_keys(d, defs, grids):
        if c.op == "is" or c.label in {"zero"} or c.label.startswith("level"):
            out.append(Hypothesis((Condition(key, c.op, c.threshold, c.label),), "refined"))
        elif c.label.startswith("q"):
            q = float(c.label[1:])
            th = _quantile_thresholds(features[key][discovery], [q])
            if th:
                out.append(Hypothesis((Condition(key, c.op, th[0][0], th[0][1]),), "refined"))
    # Adjacent thresholds on the same feature.
    if c.label.startswith("q"):
        q = float(c.label[1:])
        qs = sorted(quantiles)
        if q in qs:
            i = qs.index(q)
            for j in (i - 1, i + 1):
                if 0 <= j < len(qs) and (qs[j] > 0.5) == (q > 0.5):
                    th = _quantile_thresholds(features[c.feature][discovery], [qs[j]])
                    if th:
                        out.append(Hypothesis((Condition(c.feature, c.op, th[0][0], th[0][1]),), "refined"))
    elif c.label.startswith("level") and d.levels:
        lv = sorted(d.levels)
        if c.threshold in lv:
            i = lv.index(c.threshold)  # type: ignore[arg-type]
            for j in (i - 1, i + 1):
                if 0 <= j < len(lv):
                    out.append(Hypothesis((Condition(c.feature, c.op, lv[j], f"level{lv[j]:g}"),), "refined"))
    uniq = {n.hypothesis_id: n for n in out if n.hypothesis_id != h.hypothesis_id}
    return [uniq[k] for k in sorted(uniq)]


def interactions(survivors: Sequence[Hypothesis], defs: Mapping[str, FeatureDef], limit: int) -> list[Hypothesis]:
    """AND-pairs of surviving single conditions from different indicator families."""
    out: list[Hypothesis] = []
    for i, a in enumerate(survivors):
        for b in survivors[i + 1:]:
            fa, fb = defs[a.conditions[0].feature], defs[b.conditions[0].feature]
            if fa.family == fb.family:
                continue
            out.append(Hypothesis(a.conditions + b.conditions, "interaction"))
            if len(out) >= limit:
                return out
    return out


def load_hypothesis_file(path_text: str, payload: Mapping[str, Any]) -> list[Hypothesis]:
    if payload.get("contract") != "stock-gplm.m54-hypotheses.v1":
        raise ValueError("hypothesis file must use contract stock-gplm.m54-hypotheses.v1")
    origin = f"file:{payload.get('source', path_text)}"
    return [Hypothesis.from_dict(item, origin) for item in payload["hypotheses"]]


def unique(hyps: Iterable[Hypothesis]) -> list[Hypothesis]:
    seen: dict[str, Hypothesis] = {}
    for h in hyps:
        seen.setdefault(h.hypothesis_id, h)
    return list(seen.values())
