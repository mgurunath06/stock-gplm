"""Genetic-programming strategy discovery over the strategy DSL (catalogue entries 336-338 as a method).

Genome = 1..4 boolean *atoms* joined by AND (optionally one OR pair), a direction and an exit gene.
Atoms are drawn from an open grammar, not from the catalogue:

* oscillator / statistic vs a threshold taken from that series' **discovery-segment quantiles**
  (rsi, roc, zscore, cci, mfi, williams %R, stochastic, %B, CMF, efficiency ratio, ADX,
  percent-rank, slope/ATR, volatility ratios, relative volume, close location, gap, ...);
* bounded symbolic terms: ``(ma_a - ma_b) / atr``, ``(close - highest/lowest) / atr``, ratios of
  volatilities or volumes (entry 337 "symbolic" component — bounded expressions, not a full
  symbolic-regression fit);
* event atoms: MA crosses, channel breakouts/breakdowns, candle relations, rising/falling runs.

Fitness uses **only the discovery segment** (first 60% of the development window), split in two
halves: t-statistic of cost-adjusted excess return over the whole segment, zeroed unless both
halves are positive, minus a complexity penalty, with a minimum trade count. The validation and
final segments are never touched here. The best distinct genomes (deduplicated by signal overlap)
are returned as complete ``stock-gplm.strategy-def.v1`` documents (origin ``ML_DISCOVERED``) for the
caller to validate once and persist into the strategy library.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from . import strategy_dsl as dsl
from .strategy_engine import Exit, LaneInputs, baseline, simulate

# v2 (2026-11-15): like-for-like baseline (signal exits included), selection also requires a positive
# absolute cost-adjusted mean on the discovery segment, and every evaluated genome is logged.
EVOLVE_CONTRACT = "stock-gplm.evolve.v2"
BUDGETS = {"quick": (40, 8, 5), "standard": (80, 15, 8), "deep": (160, 30, 12)}  # population, generations, keep

# (template, parameter choices). {n} {m} are lookbacks; thresholds come from data quantiles.
_SERIES: list[tuple[str, dict[str, list[int]]]] = [
    ("rsi(close, {n})", {"n": [2, 3, 5, 9, 14, 21]}),
    ("roc(close, {n})", {"n": [1, 3, 5, 10, 20, 40, 60]}),
    ("zscore(close, {n})", {"n": [5, 10, 20, 50]}),
    ("cci({n})", {"n": [10, 14, 20, 40]}),
    ("mfi({n})", {"n": [7, 14, 21]}),
    ("willr({n})", {"n": [7, 14, 28]}),
    ("stoch_k({n}, 3)", {"n": [5, 9, 14, 21]}),
    ("pctb(close, {n}, 2)", {"n": [10, 20, 40]}),
    ("cmf({n})", {"n": [10, 20, 40]}),
    ("er({n})", {"n": [5, 10, 20]}),
    ("adx({n})", {"n": [7, 14, 21]}),
    ("plus_di({n}) - minus_di({n})", {"n": [7, 14, 21]}),
    ("percentrank(close, {n})", {"n": [10, 20, 60, 120]}),
    ("slope(close, {n}) / atr(14)", {"n": [5, 10, 20, 50]}),
    ("hv({n}) / hv({m})", {"n": [5, 10], "m": [20, 60, 120]}),
    ("volume / sma(volume, {n})", {"n": [5, 10, 20, 50]}),
    ("(close - low) / (high - low)", {}),
    ("gap", {}),
    ("(close - open) / atr(14)", {}),
    ("(ema(close, {n}) - sma(close, {m})) / atr(14)", {"n": [5, 10, 20], "m": [50, 100, 200]}),
    ("(close - highest(high, {n})) / atr(14)", {"n": [10, 20, 55, 120]}),
    ("(close - lowest(low, {n})) / atr(14)", {"n": [10, 20, 55, 120]}),
    ("(close - sma(close, {n})) / atr(14)", {"n": [5, 10, 20, 50, 200]}),
    ("macd_hist(close, 12, 26, 9) / atr(14)", {}),
    ("bb_width(close, {n}, 2) / bb_width(close, {n}, 2)[{m}]", {"n": [20], "m": [5, 20]}),
    ("atr(14) / atr(14)[{n}]", {"n": [5, 20, 60]}),
    ("count(close > close[1], {n})", {"n": [5, 10, 20]}),
    ("obv - sma(obv, {n})", {"n": [10, 20, 50]}),
    ("autocorr(ret, {n}, 1)", {"n": [20, 60]}),
    ("skew(ret, {n})", {"n": [20, 60]}),
    ("day_of_week", {}),
    ("day_of_month", {}),
]
_EVENTS: list[tuple[str, dict[str, list[int]]]] = [
    ("cross_above(ema(close, {n}), ema(close, {m}))", {"n": [3, 5, 9, 13, 20], "m": [21, 34, 50, 100, 200]}),
    ("cross_below(ema(close, {n}), ema(close, {m}))", {"n": [3, 5, 9, 13, 20], "m": [21, 34, 50, 100, 200]}),
    ("close > highest(high, {n})[1]", {"n": [5, 10, 20, 55, 120, 252]}),
    ("close < lowest(low, {n})[1]", {"n": [5, 10, 20, 55]}),
    ("close > sma(close, {n})", {"n": [10, 20, 50, 100, 200]}),
    ("close < sma(close, {n})", {"n": [10, 20, 50, 100, 200]}),
    ("ema(close, {n}) > ema(close, {m})", {"n": [5, 10, 20, 50], "m": [50, 100, 200]}),
    ("rising(close, {n})", {"n": [2, 3, 4, 5]}),
    ("falling(close, {n})", {"n": [2, 3, 4, 5]}),
    ("close > high[1]", {}),
    ("close < low[1]", {}),
    ("close > open", {}),
    ("close < open", {}),
    ("high[1] < high[2] and low[1] > low[2]", {}),
    ("(high - low) == lowest(high - low, {n})", {"n": [4, 7, 10]}),
    ("supertrend_dir(10, 3) == 1", {}),
    ("cross_above(close, swing_high(3))", {}),
    ("low < lowest(low, {n})[1] and close > lowest(low, {n})[1]", {"n": [10, 20]}),
]
_QUANTS = [0.05, 0.1, 0.2, 0.3, 0.7, 0.8, 0.9, 0.95]
_EXITS = [
    {"max_hold_bars": 3, "stop_atr": 1.5, "target_atr": 2.0},
    {"max_hold_bars": 5, "stop_atr": 1.5, "target_atr": 2.5},
    {"max_hold_bars": 10, "stop_atr": 2.0, "target_atr": 3.0},
    {"max_hold_bars": 10, "stop_atr": 2.5, "when": "close > sma(close, 5)"},
    {"max_hold_bars": 20, "stop_atr": 2.0, "trail_atr": 3.0},
    {"max_hold_bars": 40, "stop_atr": 3.0, "trail_atr": 4.0},
]
_SHORT_WHEN = "close < sma(close, 5)"


def _fill(tpl: str, choices: Mapping[str, list[int]], rng: random.Random) -> str:
    vals = {k: rng.choice(v) for k, v in choices.items()}
    if "n" in vals and "m" in vals and vals["n"] >= vals["m"]:
        vals["n"], vals["m"] = min(vals.values()), max(vals.values())
        if vals["n"] == vals["m"]:
            vals["m"] = vals["n"] * 2
    return tpl.format(**vals)


@dataclass
class Genome:
    atoms: list[str]
    direction: str
    exit_idx: int
    or_pair: bool = False
    fitness: float = -math.inf
    stats: dict[str, Any] = field(default_factory=dict)

    def entry(self) -> str:
        if self.or_pair and len(self.atoms) >= 2:
            head = f"(({self.atoms[0]}) or ({self.atoms[1]}))"
            rest = self.atoms[2:]
            return " and ".join([head] + [f"({a})" for a in rest])
        return " and ".join(f"({a})" for a in self.atoms)

    def exit(self) -> dict[str, Any]:
        e = dict(_EXITS[self.exit_idx])
        if self.direction == "short" and e.get("when"):
            e["when"] = _SHORT_WHEN
        return e

    def key(self) -> str:
        return f"{self.direction}|{self.exit_idx}|{int(self.or_pair)}|" + "|".join(sorted(self.atoms[:2]) + self.atoms[2:]
                                                                             if self.or_pair else sorted(self.atoms))


class Evolver:
    def __init__(self, li: LaneInputs, seg: tuple[int, int], seed: int, budget: str = "quick",
                 allow_short: bool = True, min_trades: int = 12):
        self.li, self.seg, self.rng = li, seg, random.Random(seed)
        self.pop_n, self.gens, self.keep = BUDGETS.get(budget, BUDGETS["quick"])
        self.allow_short = allow_short
        self.min_trades = min_trades
        self.base_cache: dict[Any, Any] = {}
        self.mask_cache: dict[str, np.ndarray | None] = {}
        self.fit_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.thresholds: dict[str, list[float]] = {}
        self.evaluated = 0
        self.generation = 0
        self.log: list[dict[str, Any]] = []  # every distinct genome evaluated, in order (audit trail)
        a, b = seg
        self.mid = a + (b - a) // 2

    # ---- atoms
    def _series_quantiles(self, expr: str) -> list[float] | None:
        if expr in self.thresholds:
            return self.thresholds[expr]
        try:
            v = dsl.evaluate(dsl.parse(expr), self.li.ctx)
        except (dsl.DSLError, dsl.MissingData):
            self.thresholds[expr] = []
            return None
        a, b = self.seg
        arr = np.asarray(v, dtype=float)[a:b]
        arr = arr[np.isfinite(arr)]
        if arr.size < 50 or float(np.std(arr)) == 0:
            self.thresholds[expr] = []
            return None
        q = sorted({float(f"{x:.4g}") for x in np.quantile(arr, _QUANTS)})
        self.thresholds[expr] = q
        return q

    def random_atom(self) -> str:
        for _ in range(20):
            if self.rng.random() < 0.6:
                tpl, ch = self.rng.choice(_SERIES)
                s = _fill(tpl, ch, self.rng)
                qs = self._series_quantiles(s)
                if not qs:
                    continue
                t = self.rng.choice(qs)
                op = ">" if t >= np.median(qs) else "<"
                if self.rng.random() < 0.25:
                    op = "<" if op == ">" else ">"
                atom = f"{s} {op} {t:g}"
            else:
                tpl, ch = self.rng.choice(_EVENTS)
                atom = _fill(tpl, ch, self.rng)
            if self._mask(atom) is not None:
                return atom
        return "close > sma(close, 50)"

    def _mask(self, expr: str) -> np.ndarray | None:
        if expr not in self.mask_cache:
            try:
                self.mask_cache[expr] = dsl.signal(expr, self.li.ctx)
            except (dsl.DSLError, dsl.MissingData):
                self.mask_cache[expr] = None
        return self.mask_cache[expr]

    def mask(self, g: Genome) -> np.ndarray:
        ms = [self._mask(a) for a in g.atoms]
        if any(m is None for m in ms):
            return np.zeros(len(self.li.market), bool)
        if g.or_pair and len(ms) >= 2:
            out = ms[0] | ms[1]
            for m in ms[2:]:
                out = out & m
            return out
        out = ms[0].copy()
        for m in ms[1:]:
            out &= m
        return out

    def random_genome(self) -> Genome:
        k = self.rng.choice([1, 2, 2, 3, 3, 4])
        d = "short" if self.allow_short and self.rng.random() < 0.25 else "long"
        return Genome([self.random_atom() for _ in range(k)], d, self.rng.randrange(len(_EXITS)),
                      or_pair=k >= 3 and self.rng.random() < 0.2)

    # ---- fitness (discovery segment only)
    def fitness(self, g: Genome) -> float:
        key = g.key()
        if key in self.fit_cache:
            g.fitness, g.stats = self.fit_cache[key]
            return g.fitness
        self.evaluated += 1
        li = self.li
        m = self.mask(g)
        ex_d = g.exit()
        ex = Exit.of(ex_d)
        xm = None
        if ex_d.get("when"):
            xm = self._mask(ex_d["when"])
        short = g.direction == "short"
        tr = simulate(li.market, m, self.seg, ex, li.buy, li.sell, short, xm)
        base = baseline(li.market, self.seg, ex, li.buy, li.sell, short, self.base_cache, xmask=xm) or 0.0
        n = len(tr)
        if n < self.min_trades:
            res = (-math.inf, {"trades": n})
        else:
            x = np.array([t["net_return"] for t in tr]) - base
            h1 = x[[t["t"] < self.mid for t in tr]]
            h2 = x[[t["t"] >= self.mid for t in tr]]
            sd = float(x.std(ddof=1))
            tstat = float(x.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0
            stable = h1.size >= 3 and h2.size >= 3 and h1.mean() > 0 and h2.mean() > 0
            score = (tstat if stable else min(tstat, 0.0) - 1.0) - 0.15 * (len(g.atoms) - 1)
            res = (score, {"trades": n, "mean_net": float(np.mean([t["net_return"] for t in tr])),
                           "excess_mean": float(x.mean()), "t_stat": tstat, "halves_positive": bool(stable)})
        self.fit_cache[key] = res
        g.fitness, g.stats = res
        self.log.append({"seq": self.evaluated, "generation": self.generation, "direction": g.direction,
                         "entry": g.entry(), "exit": g.exit(), "atoms": len(g.atoms),
                         "fitness": round(g.fitness, 6) if math.isfinite(g.fitness) else None,
                         "baseline_mean_net": round(base, 8), **{k: (round(v, 8) if isinstance(v, float) else v)
                                                                 for k, v in g.stats.items()}})
        return g.fitness

    # ---- variation
    def mutate(self, g: Genome) -> Genome:
        c = Genome(list(g.atoms), g.direction, g.exit_idx, g.or_pair)
        r = self.rng.random()
        if r < 0.35:
            c.atoms[self.rng.randrange(len(c.atoms))] = self.random_atom()
        elif r < 0.5 and len(c.atoms) < 4:
            c.atoms.append(self.random_atom())
        elif r < 0.6 and len(c.atoms) > 1:
            c.atoms.pop(self.rng.randrange(len(c.atoms)))
        elif r < 0.8:
            c.exit_idx = self.rng.randrange(len(_EXITS))
        elif r < 0.9:
            i = self.rng.randrange(len(c.atoms))
            c.atoms[i] = self._tweak(c.atoms[i])
        elif self.allow_short:
            c.direction = "short" if c.direction == "long" else "long"
        c.or_pair = c.or_pair and len(c.atoms) >= 3
        return c

    def _tweak(self, atom: str) -> str:
        for op in (" > ", " < "):
            if op in atom and atom.count(op) == 1:
                s, t = atom.split(op)
                qs = self.thresholds.get(s)
                if qs:
                    return f"{s}{op}{self.rng.choice(qs):g}"
        return self.random_atom()

    def crossover(self, a: Genome, b: Genome) -> Genome:
        pool = list(dict.fromkeys(a.atoms + b.atoms))
        k = max(1, min(4, (len(a.atoms) + len(b.atoms)) // 2))
        atoms = self.rng.sample(pool, min(k, len(pool)))
        return Genome(atoms, self.rng.choice([a.direction, b.direction]), self.rng.choice([a.exit_idx, b.exit_idx]))

    def run(self) -> dict[str, Any]:
        pop = [self.random_genome() for _ in range(self.pop_n)]
        for g in pop:
            self.fitness(g)
        history = []
        for gen in range(self.gens):
            self.generation = gen + 1
            pop.sort(key=lambda g: g.fitness, reverse=True)
            history.append(round(pop[0].fitness, 3) if math.isfinite(pop[0].fitness) else None)
            elite = pop[: max(2, self.pop_n // 8)]
            children = list(elite)
            while len(children) < self.pop_n:
                p1 = max(self.rng.sample(pop, 3), key=lambda g: g.fitness)
                p2 = max(self.rng.sample(pop, 3), key=lambda g: g.fitness)
                child = self.crossover(p1, p2) if self.rng.random() < 0.5 else self.mutate(p1)
                if self.rng.random() < 0.3:
                    child = self.mutate(child)
                self.fitness(child)
                children.append(child)
            pop = children
        pop.sort(key=lambda g: g.fitness, reverse=True)
        chosen: list[Genome] = []
        fired: list[np.ndarray] = []
        a, b = self.seg
        for g in pop:
            if not math.isfinite(g.fitness) or g.fitness <= 0 or not g.stats.get("halves_positive"):
                continue
            if (g.stats.get("mean_net") or 0.0) <= 0:  # beating the baseline while losing money is no discovery
                continue
            m = self.mask(g)[a:b]
            if any(_jaccard(m, f) > 0.7 for f in fired) or any(g.key() == c.key() for c in chosen):
                continue
            chosen.append(g)
            fired.append(m)
            if len(chosen) >= self.keep:
                break
        return {"contract": EVOLVE_CONTRACT, "population": self.pop_n, "generations": self.gens,
                "evaluated": self.evaluated, "best_by_generation": history, "selected": chosen,
                "atoms_cached": len(self.mask_cache), "log": self.log}


def _jaccard(a: np.ndarray, b: np.ndarray) -> float:
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return inter / union if union else 1.0


def genome_doc(g: Genome, symbol: str, timeframe: str, provenance: Mapping[str, Any]) -> dict[str, Any]:
    from .strategy_library import sha

    entry = g.entry()
    exit_d = g.exit()
    bare = symbol[:-3] if symbol.upper().endswith("-EQ") else symbol
    fid = sha({"e": dsl.unparse(dsl.parse(entry)), "x": exit_d, "d": g.direction, "s": bare, "tf": timeframe})[:10]
    return {"contract": "stock-gplm.strategy-def.v1", "id": f"ML-{bare}-{timeframe}-{fid}", "version": 1,
            "origin": "ML_DISCOVERED", "name": f"GP {g.direction} rule for {bare} {timeframe} ({len(g.atoms)} atoms)",
            "category": "ML: genetic programming", "style": "DISCOVERED", "direction": g.direction, "entry": entry,
            "exit": exit_d, "params": {}, "applicability": {"symbols": [bare], "timeframes": [timeframe],
                                                            "regimes": ["ALL"]},
            "fidelity": "EXACT", "assumptions": [], "requires": ["OHLCV"], "enabled": True,
            "provenance": {"method": "evolve", "contract": EVOLVE_CONTRACT, **dict(provenance),
                           "discovery_fitness": round(g.fitness, 4), "discovery_stats": g.stats}}


__all__ = ["BUDGETS", "EVOLVE_CONTRACT", "Evolver", "Genome", "genome_doc"]
