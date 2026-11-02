"""Out-of-sample metrics: discrimination, calibration and benchmark-relative net returns.

The benchmark is the equal-weight mean gross return of eligible rows on the same
as-of date, held without trading cost. The strategy is the top fraction by score,
charged the full round-trip cost. Consecutive as-of dates overlap for multi-session
horizons, so a non-overlapping subsample stepped by the horizon is reported as well.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score


def reliability(probability: np.ndarray, outcome: np.ndarray, bins: int) -> tuple[float, list[dict[str, Any]]]:
    """Expected calibration error over equal-width bins, with each bin's evidence."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(probability, edges[1:-1]), 0, bins - 1)
    table, error = [], 0.0
    for b in range(bins):
        mask = index == b
        count = int(mask.sum())
        if not count:
            continue
        mean_p, rate = float(probability[mask].mean()), float(outcome[mask].mean())
        error += count / len(outcome) * abs(mean_p - rate)
        table.append({"bin": b, "count": count, "mean_probability": round(mean_p, 6), "observed_rate": round(rate, 6)})
    return error, table


def evaluate(
    frame: pd.DataFrame, score: np.ndarray, probability: np.ndarray, *, base_rate: float, cost: float,
    top_fraction: float, bins: int, step_sessions: int, axis_positions: dict[str, int],
) -> dict[str, Any]:
    """Score one out-of-sample partition. `frame` holds targets and realised gross returns.

    Ordering metrics (AUC, rank IC, top fraction) use the raw `score`, because isotonic
    calibration maps ranges of scores to one value and would otherwise break ties by
    row order. Probability metrics (log loss, Brier, ECE) use the calibrated `probability`.
    """
    y = frame["target"].to_numpy()
    work = frame[["as_of_date", "gross_return_at_max_offset", "benchmark_return", "flag_in_label_window"]].copy()
    work["p"] = score
    ece, table = reliability(probability, y, bins)
    ranks = work.groupby("as_of_date")["p"].rank(ascending=False, method="first")
    sizes = work.groupby("as_of_date")["p"].transform("size")
    top = work[ranks <= np.maximum(1, np.floor(sizes * top_fraction))]
    per_date = top.groupby("as_of_date").agg(top=("gross_return_at_max_offset", "mean"),
                                             bench=("benchmark_return", "first"))
    per_date["excess_net"] = per_date["top"] - cost - per_date["bench"]

    def _ic(group: pd.DataFrame) -> float:
        # Undefined (NaN) when a date's scores or returns are constant; excluded from the mean.
        if group["p"].nunique() < 2 or group["gross_return_at_max_offset"].nunique() < 2:
            return float("nan")
        return float(group["p"].rank().corr(group["gross_return_at_max_offset"].rank()))

    ic = work.groupby("as_of_date")[["p", "gross_return_at_max_offset"]].apply(_ic).dropna()
    positions = np.array([axis_positions[d] for d in per_date.index])
    stride = per_date[(positions - positions.min()) % step_sessions == 0]
    single = len(np.unique(y)) < 2
    return {
        "rows": int(len(frame)), "dates": int(per_date.shape[0]),
        "positive_rate": round(float(y.mean()), 6),
        "rows_with_flag_in_label_window": int(work["flag_in_label_window"].sum()),
        "auc": None if single else round(float(roc_auc_score(y, score)), 6),
        "log_loss": None if single else round(float(log_loss(y, probability, labels=[0, 1])), 6),
        "log_loss_base_rate": None if single else round(float(log_loss(y, np.full(len(y), base_rate), labels=[0, 1])), 6),
        "brier": round(float(brier_score_loss(y, probability)), 6),
        "brier_base_rate": round(float(np.mean((base_rate - y) ** 2)), 6),
        "ece": round(float(ece), 6),
        "reliability": table,
        "rank_ic_mean": round(float(ic.mean()), 6),
        "rank_ic_positive_share": round(float((ic > 0).mean()), 6),
        "top_mean_gross": round(float(per_date["top"].mean()), 6),
        "benchmark_mean_gross": round(float(per_date["bench"].mean()), 6),
        "top_excess_net_mean": round(float(per_date["excess_net"].mean()), 6),
        "top_excess_net_positive_share": round(float((per_date["excess_net"] > 0).mean()), 6),
        "non_overlapping": {
            "dates": int(len(stride)),
            "top_excess_net_mean": round(float(stride["excess_net"].mean()), 6) if len(stride) else None,
        },
    }
