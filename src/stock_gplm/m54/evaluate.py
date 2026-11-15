"""Causal event study, economic evaluation and statistics for one hypothesis.

Timing (matches the V1 simulator convention):

* the signal is computed from bars up to and including the close of session ``t``;
* entry fills at the **open of t+1** (adverse slippage + buy-side costs);
* exit fills at the **open of t+1+h** for horizon ``h`` sessions (sell-side costs);
* path statistics (MFE, MAE, time to target/failure) use sessions ``t+1 .. t+h``.

Segment containment: an event counts in a segment only if its signal session and
its exit session both lie inside the segment, so no label straddles a segment
boundary (purging). Overlap control: *independent* events are chosen greedily so
a new event starts only after the previous one exits. Statistics use independent
events; raw (overlapping) counts are reported but never tested.

Sessions flagged as suspected corporate-action discontinuities exclude any event
whose holding path touches them (the provider price basis is unverified).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as sps

from ..v1.portfolio import CostModel


@dataclass(frozen=True)
class Market:
    dates: tuple[str, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    natr: np.ndarray  # ATR(14)/close known at t, used for target/stop scaling
    blocked: np.ndarray  # True on sessions with a suspected price discontinuity


def cost_rates(costs: CostModel) -> tuple[float, float]:
    """Per-side cost fractions (buy, sell) including slippage."""
    buy = (costs.commission_bps + costs.slippage_bps + costs.buy_statutory_bps) / 10_000
    sell = (costs.commission_bps + costs.slippage_bps + costs.sell_statutory_bps) / 10_000
    return buy, sell


def eligible_starts(m: Market, seg: tuple[int, int], h: int) -> np.ndarray:
    """Signal sessions whose full trade (entry t+1, exit open t+1+h) lies inside the segment."""
    a, b = seg
    last = b - 1 - (1 + h)
    if last < a:
        return np.zeros(0, dtype=int)
    idx = np.arange(a, last + 1)
    if m.blocked.any():
        cum = np.concatenate(([0], np.cumsum(m.blocked.astype(int))))
        # path sessions t+1 .. t+1+h must not contain a blocked session
        touched = cum[idx + 2 + h] - cum[idx + 1]
        idx = idx[touched == 0]
    return idx


def _trade(m: Market, t: int, h: int, buy: float, sell: float, target_atr: float) -> dict[str, Any]:
    entry_ref = m.open[t + 1]
    exit_ref = m.open[t + 1 + h]
    entry = entry_ref * (1 + buy)
    net = exit_ref * (1 - sell) / entry - 1
    gross = exit_ref / entry_ref - 1
    hi = m.high[t + 1:t + 1 + h]
    lo = m.low[t + 1:t + 1 + h]
    cl = m.close[t + 1:t + 1 + h]
    band = target_atr * m.natr[t] if math.isfinite(m.natr[t]) else float("nan")
    hit_t = np.nonzero(hi >= entry_ref * (1 + band))[0] if math.isfinite(band) else np.zeros(0, int)
    hit_s = np.nonzero(lo <= entry_ref * (1 - band))[0] if math.isfinite(band) else np.zeros(0, int)
    path = np.concatenate(([entry_ref], cl))
    peak = np.maximum.accumulate(path)
    return {"signal_date": m.dates[t], "entry_date": m.dates[t + 1], "exit_date": m.dates[t + 1 + h],
            "entry_price": float(entry_ref), "exit_price": float(exit_ref), "gross_return": float(gross),
            "net_return": float(net), "mfe": float(hi.max() / entry_ref - 1), "mae": float(lo.min() / entry_ref - 1),
            "max_drawdown": float((path / peak - 1).min()),
            "time_to_target": int(hit_t[0]) + 1 if hit_t.size else None,
            "time_to_failure": int(hit_s[0]) + 1 if hit_s.size else None}


def independent(starts: np.ndarray, h: int) -> list[int]:
    chosen: list[int] = []
    nxt = -1
    for t in starts.tolist():
        if t >= nxt:
            chosen.append(t)
            nxt = t + 1 + h  # exit session; the next entry may be decided that day
    return chosen


def baseline_returns(m: Market, seg: tuple[int, int], h: int, buy: float, sell: float) -> np.ndarray:
    idx = eligible_starts(m, seg, h)
    if idx.size == 0:
        return np.zeros(0)
    return m.open[idx + 1 + h] * (1 - sell) / (m.open[idx + 1] * (1 + buy)) - 1


def bootstrap_ci(values: np.ndarray, iterations: int, seed: int, level: float = 0.95) -> tuple[float | None, float | None]:
    if values.size < 5 or iterations <= 0:
        return None, None
    rng = np.random.default_rng(seed)
    means = rng.choice(values, size=(iterations, values.size), replace=True).mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def random_entry_percentile(base: np.ndarray, n: int, observed: float, draws: int, seed: int) -> float | None:
    """Share of random same-size entry sets (same segment and horizon) whose mean is below the observed mean."""
    if n < 3 or base.size < n or draws <= 0:
        return None
    rng = np.random.default_rng(seed)
    sims = np.array([rng.choice(base, size=n, replace=False).mean() for _ in range(draws)])
    return float((sims < observed).mean())


def equity_stats(net: Sequence[float]) -> dict[str, float | None]:
    if not net:
        return {"compounded_return": None, "equity_max_drawdown": None}
    eq = np.cumprod(1 + np.asarray(net))
    peak = np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:]
    return {"compounded_return": float(eq[-1] - 1), "equity_max_drawdown": float((eq / peak - 1).min())}


def evaluate(m: Market, mask: np.ndarray, seg: tuple[int, int], h: int, costs: CostModel, *, target_atr: float,
             bootstrap: int, random_draws: int, seed: int, regimes: Mapping[str, np.ndarray] | None = None,
             folds: int = 0, keep_events: bool = False) -> dict[str, Any]:
    buy, sell = cost_rates(costs)
    starts = eligible_starts(m, seg, h)
    fired = starts[mask[starts]] if starts.size else starts
    chosen = independent(fired, h)
    trades = [_trade(m, t, h, buy, sell, target_atr) for t in chosen]
    base = baseline_returns(m, seg, h, buy, sell)
    base_mean = float(base.mean()) if base.size else None
    net = np.array([x["net_return"] for x in trades])
    n = int(net.size)
    out: dict[str, Any] = {"horizon": h, "segment": [m.dates[seg[0]], m.dates[seg[1] - 1]],
                           "raw_events": int(fired.size), "independent_events": n,
                           "eligible_sessions": int(starts.size), "baseline_mean_net": base_mean}
    if n == 0:
        out.update({"mean_net": None, "median_net": None, "hit_rate": None, "excess_mean": None, "t_stat": None,
                    "p_value": None})
        if keep_events:
            out["events"] = []
        return out
    excess = net - (base_mean or 0.0)
    if n >= 3 and float(np.std(excess, ddof=1)) > 0:
        tstat = float(np.mean(excess) / (np.std(excess, ddof=1) / math.sqrt(n)))
        p = float(sps.t.sf(tstat, df=n - 1))  # one-sided: pattern beats unconditional entry
    else:
        tstat, p = None, None
    ci = bootstrap_ci(net, bootstrap, seed)
    gross = np.array([x["gross_return"] for x in trades])
    ttt = [x["time_to_target"] for x in trades if x["time_to_target"] is not None]
    ttf = [x["time_to_failure"] for x in trades if x["time_to_failure"] is not None]
    out.update({
        "mean_net": float(net.mean()), "median_net": float(np.median(net)), "std_net": float(net.std(ddof=1)) if n > 1 else None,
        "mean_gross": float(gross.mean()), "hit_rate": float((net > 0).mean()),
        "success_definition": "net return after round-trip costs > 0 at the horizon exit",
        "excess_mean": float(excess.mean()), "t_stat": tstat, "p_value": p,
        "ci95_mean_net": list(ci), "return_quantiles": {str(q): float(np.quantile(net, q)) for q in (0.05, 0.25, 0.5, 0.75, 0.95)},
        "mean_mfe": float(np.mean([x["mfe"] for x in trades])), "mean_mae": float(np.mean([x["mae"] for x in trades])),
        "worst_trade_drawdown": float(min(x["max_drawdown"] for x in trades)),
        "target_hit_rate": len(ttt) / n, "mean_time_to_target": float(np.mean(ttt)) if ttt else None,
        "failure_hit_rate": len(ttf) / n, "mean_time_to_failure": float(np.mean(ttf)) if ttf else None,
        "random_entry_percentile": random_entry_percentile(base, n, float(net.mean()), random_draws, seed + 1),
        "exposure_fraction": n * h / max(1, seg[1] - seg[0]),
        **equity_stats(net.tolist()),
    })
    if regimes:
        reg: dict[str, Any] = {}
        for name, labels in regimes.items():
            groups: dict[str, list[float]] = {}
            for t, r in zip(chosen, net.tolist()):
                groups.setdefault(str(labels[t]), []).append(r)
            reg[name] = {k: {"n": len(v), "mean_net": float(np.mean(v))} for k, v in sorted(groups.items())}
        out["regimes"] = reg
    if folds > 1:
        edges = np.linspace(seg[0], seg[1], folds + 1).astype(int)
        fold_rows = []
        for i in range(folds):
            vals = [r - (base_mean or 0.0) for t, r in zip(chosen, net.tolist()) if edges[i] <= t < edges[i + 1]]
            fold_rows.append({"fold": i + 1, "start": m.dates[edges[i]], "n": len(vals),
                              "mean_excess": float(np.mean(vals)) if vals else None})
        informative = [f for f in fold_rows if f["n"] >= 3]
        out["walk_forward"] = {"folds": fold_rows, "informative_folds": len(informative),
                               "positive_fraction": (sum(1 for f in informative if f["mean_excess"] > 0) / len(informative))
                               if informative else None}
    if keep_events:
        out["events"] = trades
    return out


def bh_adjust(pvalues: Sequence[float | None], total_tests: int) -> list[float | None]:
    """Benjamini-Hochberg q-values; ``None`` p-values count as tests (p = 1) but stay ``None``."""
    m = max(total_tests, len(pvalues))
    filled = np.array([1.0 if p is None else p for p in pvalues])
    if filled.size == 0:
        return []
    order = np.argsort(filled)
    ranked = filled[order] * m / np.arange(1, filled.size + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    out = np.empty_like(q)
    out[order] = q
    return [None if p is None else float(v) for p, v in zip(pvalues, out)]


def bonferroni(p: float | None, total_tests: int) -> float | None:
    return None if p is None else min(1.0, p * max(1, total_tests))
