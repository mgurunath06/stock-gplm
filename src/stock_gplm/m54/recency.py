"""Recency weighting schemes for ranking strategies, selected on the validation segment.

Weights are a function of a trade's age measured in **sessions** before the end of the
segment, never in bars, so a 1-minute lane cannot gain weight merely by having more bars.
Weights only change the *ranking* score (``weighted_excess_mean``); the hypothesis test that
feeds multiple-testing control is always unweighted.

Schemes (configured, none preferred in code):

* ``uniform``                   every trade weight 1
* ``exp_hl<N>``                 0.5 ** (age_sessions / N)
* ``regime_match``              1 when the trade's regime label equals the regime at the end
                                of the training segment, otherwise ``mismatch_weight``
* ``exp_hl<N>_regime``          product of the two
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class Scheme:
    name: str
    half_life_sessions: float | None = None
    regime: bool = False
    mismatch_weight: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "half_life_sessions": self.half_life_sessions, "regime_conditional": self.regime,
                "mismatch_weight": self.mismatch_weight if self.regime else None}


def schemes(cfg: Mapping[str, Any]) -> list[Scheme]:
    out = [Scheme("uniform")]
    for hl in cfg.get("half_lives_sessions", []):
        out.append(Scheme(f"exp_hl{hl:g}", float(hl)))
    if cfg.get("regime_conditional", False):
        mw = float(cfg.get("mismatch_weight", 0.5))
        out.append(Scheme("regime_match", None, True, mw))
        for hl in cfg.get("half_lives_sessions", [])[:1]:
            out.append(Scheme(f"exp_hl{hl:g}_regime", float(hl), True, mw))
    return out


def weights(scheme: Scheme, trade_sessions: Sequence[int], end_session: int, trade_regimes: Sequence[str] | None = None,
            current_regime: str | None = None) -> np.ndarray:
    n = len(trade_sessions)
    w = np.ones(n)
    if scheme.half_life_sessions:
        age = end_session - np.asarray(trade_sessions, dtype=float)
        w *= 0.5 ** (np.clip(age, 0, None) / scheme.half_life_sessions)
    if scheme.regime and trade_regimes is not None and current_regime is not None:
        w *= np.where(np.asarray(trade_regimes) == current_regime, 1.0, scheme.mismatch_weight)
    return w


def select(scheme_rows: Mapping[str, Sequence[float | None]]) -> tuple[str, dict[str, Any]]:
    """Pick the scheme whose discovery-ranked top strategies earned the highest mean validation return.

    ``scheme_rows[name]`` holds the validation mean net returns of that scheme's top-K strategies.
    Ties (and the case where no scheme has any validation evidence) resolve to ``uniform``.
    """
    table = {}
    for name, vals in scheme_rows.items():
        v = [x for x in vals if x is not None]
        table[name] = {"top_k": len(vals), "with_validation_trades": len(v),
                       "mean_validation_net": float(np.mean(v)) if v else None}
    scored = [(t["mean_validation_net"], name) for name, t in table.items() if t["mean_validation_net"] is not None]
    if not scored:
        return "uniform", table
    best = max(scored, key=lambda x: (x[0], x[1] == "uniform"))
    uniform = table.get("uniform", {}).get("mean_validation_net")
    if uniform is not None and abs(best[0] - uniform) < 1e-12:
        return "uniform", table
    return best[1], table
