"""ML research readiness: purged, segment-aware datasets from discovery features.

This module builds matrices only. It fits nothing, reserves no trial and never
reads the M3.2 locked holdout. Model fitting stays under the M3.5/M3.7 policies
(manifest-verified inputs, shared 20-trial-per-horizon ledger, single holdout
scoring). The intended consumers are those pipelines and future stock-specific,
sector or pooled models. Compare any model with buy-and-hold, unconditional entry,
rule-based findings and simple statistical baselines before claiming value.

Labels use the same timing as the event study: entry at the open of t+1, exit at
the open of t+1+h, after round-trip costs. Rows whose label window leaves their
segment are dropped (purging), so no label straddles discovery/validation/test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np

from .evaluate import cost_rates, eligible_starts

if TYPE_CHECKING:
    from .engine import Run


@dataclass(frozen=True)
class SegmentMatrix:
    segment: str
    dates: tuple[str, ...]
    X: np.ndarray
    y_net_return: np.ndarray
    y_positive: np.ndarray


def build_dataset(run: "Run", horizon: int, feature_keys: Sequence[str] | None = None,
                  segments: Sequence[str] = ("discovery", "validation", "test")) -> dict[str, SegmentMatrix]:
    if "holdout" in segments:
        raise ValueError("the locked holdout is not available to this builder")
    keys = sorted(feature_keys) if feature_keys is not None else sorted(run.features)
    buy, sell = cost_rates(run.costs)
    m = run.market
    out: dict[str, SegmentMatrix] = {}
    for seg in segments:
        idx = eligible_starts(m, run.segs[seg], horizon)
        y = m.open[idx + 1 + horizon] * (1 - sell) / (m.open[idx + 1] * (1 + buy)) - 1
        X = np.column_stack([run.features[k][idx] for k in keys]) if keys else np.zeros((idx.size, 0))
        out[seg] = SegmentMatrix(seg, tuple(m.dates[i] for i in idx), X, y, (y > 0).astype(int))
    return out


def feature_coverage(matrices: Mapping[str, SegmentMatrix]) -> dict[str, float]:
    """Share of finite feature values per segment (warm-up NaNs are left for the model policy)."""
    return {k: float(np.isfinite(v.X).mean()) if v.X.size else 0.0 for k, v in matrices.items()}
