"""Pure diagnostic functions for M3.8. None of them fits a predictive model or edits data.

Conventions: `frame` is a per-row development frame with `symbol`, `as_of_date`,
`gross_return_at_max_offset`, `benchmark_return` (same-date equal-weight mean) and
`target` (above the same-date median). Non-overlapping statistics step through the
session axis by the horizon's maximum offset so their observations are independent.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

# Unadjusted price ratios produced by common splits, bonuses and consolidations.
CORPORATE_ACTION_FACTORS = {
    "split_1:2": 1 / 2, "split_1:4": 1 / 4, "split_1:5": 1 / 5, "split_1:10": 1 / 10, "split_2:5": 2 / 5,
    "bonus_1:1": 1 / 2, "bonus_2:1": 1 / 3, "bonus_1:2": 2 / 3, "bonus_1:3": 3 / 4, "bonus_3:1": 1 / 4,
    "bonus_4:1": 1 / 5, "consolidation_2:1": 2.0, "consolidation_5:1": 5.0, "consolidation_10:1": 10.0,
}


def _round(value: Any, digits: int = 6) -> Any:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    return round(float(value), digits)


MIN_INDEPENDENT_DATES = 20


def t_p(t: float, n: int) -> float | None:
    """Two-sided Student-t p-value with n-1 degrees of freedom; small samples are not normal."""
    from scipy.stats import t as student

    return None if n < 3 else float(2 * student.sf(abs(t), n - 1))


def stride_dates(dates: Iterable[str], positions: Mapping[str, int], step: int) -> list[str]:
    """Dates whose axis position is a multiple of `step` from the first: independent holding periods."""
    ordered = sorted(set(dates), key=positions.__getitem__)
    if not ordered:
        return []
    first = positions[ordered[0]]
    return [d for d in ordered if (positions[d] - first) % step == 0]


# --------------------------------------------------------------------------- return decomposition

def decile_returns(frame: pd.DataFrame, score: np.ndarray, deciles: int = 10) -> pd.DataFrame:
    """Mean per-date gross return, hit rate and rows by score decile (decile 10 is the top)."""
    work = frame[["as_of_date", "gross_return_at_max_offset", "benchmark_return", "target"]].copy()
    work["score"] = score
    pct = work.groupby("as_of_date")["score"].rank(method="first", pct=True)
    work["decile"] = np.minimum(np.ceil(pct * deciles), deciles).astype(int)
    per_date = work.groupby(["as_of_date", "decile"]).agg(
        gross=("gross_return_at_max_offset", "mean"), bench=("benchmark_return", "first"),
        hit=("target", "mean"), rows=("target", "size")).reset_index()
    per_date["excess_gross"] = per_date["gross"] - per_date["bench"]
    out = per_date.groupby("decile").agg(gross_mean=("gross", "mean"), excess_gross_mean=("excess_gross", "mean"),
                                         hit_rate=("hit", "mean"), rows=("rows", "sum")).reset_index()
    return out


def top_portfolio(frame: pd.DataFrame, score: np.ndarray, *, top_fraction: float, step: int,
                  positions: Mapping[str, int], round_trip: float) -> tuple[dict[str, Any], pd.DataFrame]:
    """Decompose non-overlapping top-fraction returns into benchmark, selection, cost and turnover.

    `net_excess_full = gross_excess - round_trip` charges a full round trip each period, as M3.5
    and M3.7 do. `net_excess_turnover` charges it only on the fraction of names replaced, which is
    what a holder that keeps repeat picks would pay. Both are diagnostics, not new strategies.
    """
    work = frame[["symbol", "as_of_date", "gross_return_at_max_offset", "benchmark_return"]].copy()
    work["score"] = score
    ranks = work.groupby("as_of_date")["score"].rank(ascending=False, method="first")
    sizes = work.groupby("as_of_date")["score"].transform("size")
    chosen = work[ranks <= np.maximum(1, np.floor(sizes * top_fraction))]
    bottom = work[ranks > sizes - np.maximum(1, np.floor(sizes * top_fraction))]
    dates = stride_dates(chosen["as_of_date"], positions, step)
    rows, previous = [], None
    for day in dates:
        picks = chosen[chosen["as_of_date"] == day]
        names = set(picks["symbol"])
        turnover = 1.0 if previous is None else 1 - len(names & previous) / max(len(names), 1)
        low = bottom[bottom["as_of_date"] == day]["gross_return_at_max_offset"].mean()
        rows.append({"as_of_date": day, "names": len(names), "top_gross": picks["gross_return_at_max_offset"].mean(),
                     "benchmark": picks["benchmark_return"].iloc[0], "bottom_gross": low, "turnover": turnover})
        previous = names
    periods = pd.DataFrame(rows)
    if periods.empty:
        return {"periods": 0}, periods
    periods["gross_excess"] = periods["top_gross"] - periods["benchmark"]
    periods["net_excess_full"] = periods["gross_excess"] - round_trip
    periods["cost_turnover"] = round_trip * periods["turnover"]
    periods["net_excess_turnover"] = periods["gross_excess"] - periods["cost_turnover"]
    periods["benchmark_minus_bottom"] = periods["benchmark"] - periods["bottom_gross"]
    beta, alpha = np.nan, np.nan
    if len(periods) > 2 and periods["benchmark"].var() > 0:
        beta, alpha = np.polyfit(periods["benchmark"], periods["top_gross"], 1)
    n = len(periods)
    std = periods["gross_excess"].std(ddof=1) if n > 1 else np.nan
    summary = {
        "periods": n, "holding_sessions": step, "names_mean": _round(periods["names"].mean(), 2),
        "benchmark_mean": _round(periods["benchmark"].mean()),
        "top_gross_mean": _round(periods["top_gross"].mean()),
        "gross_excess_mean": _round(periods["gross_excess"].mean()),
        "gross_excess_t": _round(periods["gross_excess"].mean() / (std / sqrt(n)) if n > 1 and std > 0 else None, 3),
        "round_trip_cost": round_trip,
        "turnover_mean": _round(periods["turnover"].iloc[1:].mean() if n > 1 else 1.0, 4),
        "cost_turnover_mean": _round(periods["cost_turnover"].mean()),
        "net_excess_full_cost_mean": _round(periods["net_excess_full"].mean()),
        "net_excess_turnover_cost_mean": _round(periods["net_excess_turnover"].mean()),
        "breakeven_round_trip": _round(periods["gross_excess"].mean() / max(periods["turnover"].mean(), 1e-9)),
        "top_minus_benchmark_mean": _round(periods["gross_excess"].mean()),
        "benchmark_minus_bottom_mean": _round(periods["benchmark_minus_bottom"].mean()),
        "beta_to_benchmark": _round(beta, 4), "alpha_per_period": _round(alpha),
    }
    return summary, periods


def exposures(frame: pd.DataFrame, score: np.ndarray, columns: Iterable[str], top_fraction: float) -> dict[str, Any]:
    """Mean same-date percentile of each characteristic in the top fraction; 0.5 means no tilt."""
    work = frame[["as_of_date", *columns]].copy()
    ranks = pd.Series(score, index=work.index).groupby(work["as_of_date"]).rank(ascending=False, method="first")
    sizes = work.groupby("as_of_date")["as_of_date"].transform("size")
    top = ranks <= np.maximum(1, np.floor(sizes * top_fraction))
    out = {}
    for column in columns:
        pct = work.groupby("as_of_date")[column].rank(pct=True)
        out[column] = _round(pct[top].mean(), 4)
    return out


# --------------------------------------------------------------------------- label audit

def label_audit(frame: pd.DataFrame, round_trip: float) -> dict[str, Any]:
    """Do above-median labels correspond to returns that beat the benchmark after costs?"""
    excess = frame["gross_return_at_max_offset"] - frame["benchmark_return"]
    per_date = frame.groupby("as_of_date").agg(mean=("gross_return_at_max_offset", "mean"),
                                               median=("gross_return_at_max_offset", "median"))
    positive = frame["target"] == 1
    return {
        "rows": int(len(frame)), "positive_rate": _round(positive.mean(), 4),
        "share_beating_mean": _round((excess > 0).mean(), 4),
        "share_beating_mean_after_cost": _round((excess > round_trip).mean(), 4),
        "positives_not_beating_mean_after_cost": _round((excess[positive] <= round_trip).mean(), 4),
        "mean_minus_median_per_date": _round((per_date["mean"] - per_date["median"]).mean()),
        "dates_mean_above_median": _round((per_date["mean"] > per_date["median"]).mean(), 4),
        "positive_excess_mean": _round(excess[positive].mean()),
        "negative_excess_mean": _round(excess[~positive].mean()),
        "excess_skew": _round(excess.skew(), 3),
    }


# --------------------------------------------------------------------------- OHLCV audit

@dataclass(frozen=True)
class AuditThresholds:
    large_move: float = 0.2
    factor_tolerance: float = 0.03
    persistence_tolerance: float = 0.15
    persistence_window: int = 20
    stale_run: int = 5
    volume_spike: float = 50.0


def classify_large_moves(prices: pd.DataFrame, thresholds: AuditThresholds = AuditThresholds()) -> pd.DataFrame:
    """Classify every close-to-close move of at least the threshold for one or many symbols.

    PROBABLE_UNADJUSTED_CORPORATE_ACTION: the ratio matches a split/bonus/consolidation factor
    and the price level persists for the following window (an unadjusted series step).
    POSSIBLE_UNADJUSTED_CORPORATE_ACTION: the ratio matches but the level does not persist.
    UNEXPLAINED_LARGE_MOVE: no factor matches. Nothing is corrected; these are quarantine flags.
    """
    events = []
    w = thresholds.persistence_window
    for symbol, group in prices.sort_values(["symbol", "trading_date"]).groupby("symbol", sort=False):
        close = group["close"].to_numpy(dtype=float)
        opening = group["open"].to_numpy(dtype=float)
        volume = group["volume"].to_numpy(dtype=float)
        dates = group["trading_date"].to_numpy()
        ratio = close[1:] / close[:-1]
        for i in np.flatnonzero(np.abs(ratio - 1) >= thresholds.large_move) + 1:
            r = close[i] / close[i - 1]
            name, factor = min(CORPORATE_ACTION_FACTORS.items(), key=lambda kv: abs(np.log(r / kv[1])))
            matched = abs(r / factor - 1) <= thresholds.factor_tolerance
            before, after = close[max(0, i - w):i], close[i:i + w]
            level = np.median(after) / np.median(before) if len(before) and len(after) else np.nan
            persists = bool(np.isfinite(level) and abs(level / factor - 1) <= thresholds.persistence_tolerance)
            vol_before, vol_after = np.median(volume[max(0, i - w):i]), np.median(volume[i:i + w])
            volume_shift = vol_after / vol_before if vol_before > 0 else np.nan
            if matched and persists:
                label = "PROBABLE_UNADJUSTED_CORPORATE_ACTION"
            elif matched:
                label = "POSSIBLE_UNADJUSTED_CORPORATE_ACTION"
            else:
                label = "UNEXPLAINED_LARGE_MOVE"
            events.append({"symbol": symbol, "trading_date": dates[i], "classification": label,
                           "close_ratio": round(float(r), 5), "nearest_factor": name if matched else "",
                           "open_gap_ratio": round(float(opening[i] / close[i - 1]), 5),
                           "level_ratio_after_before": _round(level, 5), "volume_ratio_after_before": _round(volume_shift, 3)})
    return pd.DataFrame(events, columns=["symbol", "trading_date", "classification", "close_ratio", "nearest_factor",
                                         "open_gap_ratio", "level_ratio_after_before", "volume_ratio_after_before"])


def stale_and_volume(prices: pd.DataFrame, thresholds: AuditThresholds = AuditThresholds()) -> pd.DataFrame:
    """Per-symbol counts: stale-close runs, zero-volume days, volume spikes, OHLC inconsistencies."""
    out = []
    for symbol, group in prices.sort_values(["symbol", "trading_date"]).groupby("symbol", sort=False):
        close = group["close"].to_numpy(dtype=float)
        volume = group["volume"].to_numpy(dtype=float)
        same = np.concatenate([[False], close[1:] == close[:-1]])
        run_id = np.cumsum(~same)
        runs = pd.Series(same).groupby(run_id).sum().to_numpy() + 1
        trailing = pd.Series(volume).rolling(60, min_periods=20).median().shift(1).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            spikes = np.nansum((trailing > 0) & (volume / trailing >= thresholds.volume_spike))
        bad = ((group["high"] < group[["open", "close"]].max(axis=1)) |
               (group["low"] > group[["open", "close"]].min(axis=1))).sum()
        out.append({"symbol": symbol, "candles": len(group),
                    "stale_runs": int((runs >= thresholds.stale_run).sum()),
                    "stale_days": int(runs[runs >= thresholds.stale_run].sum()),
                    "zero_volume_days": int((volume == 0).sum()), "volume_spikes": int(spikes),
                    "ohlc_inconsistent": int(bad)})
    return pd.DataFrame(out)


def rows_with_event_in_window(frame: pd.DataFrame, events: pd.DataFrame, positions: Mapping[str, int],
                              max_offset: int) -> pd.Series:
    """True for rows whose label window (as_of, as_of + max_offset] contains a quarantined event."""
    flagged = pd.Series(False, index=frame.index)
    if events.empty:
        return flagged
    by_symbol = {s: np.sort([positions[d] for d in g["trading_date"] if d in positions])
                 for s, g in events.groupby("symbol")}
    pos = frame["as_of_date"].map(positions).to_numpy()
    for symbol, idx in frame.groupby("symbol").indices.items():
        marks = by_symbol.get(symbol)
        if marks is None or not len(marks):
            continue
        start = pos[idx]
        nxt = np.searchsorted(marks, start, side="right")
        hit = nxt < len(marks)
        hit[hit] = marks[nxt[hit]] <= start[hit] + max_offset
        flagged.iloc[idx] = hit
    return flagged


# --------------------------------------------------------------------------- signals and features

def daily_ic(frame: pd.DataFrame, column: str, target: str = "gross_return_at_max_offset") -> pd.Series:
    """Per-date Spearman rank correlation of one column with the forward return."""
    work = frame[["as_of_date", column, target]].dropna()
    ranks = work.groupby("as_of_date")[[column, target]].rank()
    ranks["as_of_date"] = work["as_of_date"]
    grouped = ranks.groupby("as_of_date")
    return grouped[column].corr(ranks[target]).dropna() if len(ranks) else pd.Series(dtype=float)


def ic_summary(ic: pd.Series, positions: Mapping[str, int], step: int,
               groups: Mapping[str, pd.Index] | None = None) -> dict[str, Any]:
    """Mean IC with a t statistic on non-overlapping dates, and its sign consistency across groups."""
    if ic.empty:
        return {"dates": 0}
    independent = ic.loc[stride_dates(ic.index, positions, step)]
    n, std = len(independent), independent.std(ddof=1)
    t = independent.mean() / (std / sqrt(n)) if n > 1 and std > 0 else 0.0
    out = {"dates": int(len(ic)), "mean_ic": _round(ic.mean()), "independent_dates": n,
           "independent_mean_ic": _round(independent.mean()), "t": _round(t, 3), "p_student": _round(t_p(t, n), 6),
           "enough_independent_dates": n >= MIN_INDEPENDENT_DATES,
           "positive_share": _round((ic > 0).mean(), 4)}
    if groups:
        means = {name: _round(ic.reindex(index).dropna().mean()) for name, index in groups.items()}
        out["by_group"] = means
        valid = [v for v in means.values() if v is not None]
        sign = np.sign(ic.mean())
        out["groups_same_sign"] = f"{sum(np.sign(v) == sign for v in valid)}/{len(valid)}"
    return out


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float | None:
    """Population stability index of `current` against decile bins of `reference`."""
    reference, current = reference[np.isfinite(reference)], current[np.isfinite(current)]
    if len(reference) < bins or len(current) < bins:
        return None
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    expected = np.histogram(reference, edges)[0] / len(reference)
    actual = np.histogram(current, edges)[0] / len(current)
    expected, actual = np.clip(expected, 1e-6, None), np.clip(actual, 1e-6, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def high_correlations(frame: pd.DataFrame, columns: list[str], threshold: float, sample: int, seed: int
                      ) -> list[dict[str, Any]]:
    data = frame[columns]
    if len(data) > sample:
        data = data.sample(sample, random_state=seed)
    corr = data.rank().corr()
    pairs = []
    for i, a in enumerate(columns):
        for b in columns[i + 1:]:
            value = corr.loc[a, b]
            if np.isfinite(value) and abs(value) >= threshold:
                pairs.append({"a": a, "b": b, "spearman": round(float(value), 4)})
    return sorted(pairs, key=lambda p: -abs(p["spearman"]))
