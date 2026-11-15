"""Research interfaces for recency weighting, rolling windows and multi-timeframe features.

These complement :mod:`stock_gplm.m3.splits` (walk-forward folds, purge/embargo,
leakage checks), which remains the only split implementation. Nothing here fits a
model, reserves a trial or reads a holdout: callers pass development-partition
rows only, and comparisons of weighting schemes must use chronological validation
folds, never the locked holdout.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

WEIGHTING_SCHEMES = frozenset({"equal", "exponential"})


@dataclass(frozen=True)
class TrainingWindow:
    """How much history a fold trains on and how it is weighted.

    ``window_sessions=None`` means expanding (all available training sessions).
    ``half_life_sessions`` applies only to ``exponential``: a sample that many
    sessions older than the newest training session gets half its weight.
    """

    scheme: str = "equal"
    window_sessions: int | None = None
    half_life_sessions: float | None = None

    def __post_init__(self) -> None:
        if self.scheme not in WEIGHTING_SCHEMES:
            raise ValueError(f"scheme must be one of {sorted(WEIGHTING_SCHEMES)}")
        if self.window_sessions is not None and (type(self.window_sessions) is not int or self.window_sessions < 1):
            raise ValueError("window_sessions must be a positive integer or None")
        if self.scheme == "exponential":
            if self.half_life_sessions is None or not self.half_life_sessions > 0:
                raise ValueError("exponential weighting requires a positive half_life_sessions")
        elif self.half_life_sessions is not None:
            raise ValueError("half_life_sessions applies only to exponential weighting")

    @property
    def label(self) -> str:
        window = "expanding" if self.window_sessions is None else f"roll{self.window_sessions}"
        weight = "eq" if self.scheme == "equal" else f"hl{self.half_life_sessions:g}"
        return f"{window}-{weight}"


def apply_window(train_sessions: Sequence[str], window: TrainingWindow) -> list[str]:
    """Return the training sessions kept by the window (the most recent ``window_sessions``)."""
    ordered = list(train_sessions)
    if ordered != sorted(set(ordered)):
        raise ValueError("training sessions must be unique and ascending")
    return ordered if window.window_sessions is None else ordered[-window.window_sessions:]


def recency_weights(sample_sessions: Sequence[str], train_sessions: Sequence[str],
                    window: TrainingWindow) -> list[float]:
    """Per-sample weights, normalized to mean 1, for samples dated on kept training sessions.

    Age is measured in sessions on the training axis (not calendar days) from the
    newest kept session, so holidays and weekends do not distort decay.
    """
    kept = apply_window(train_sessions, window)
    position = {day: i for i, day in enumerate(kept)}
    missing = sorted({d for d in sample_sessions if d not in position})
    if missing:
        raise ValueError(f"samples outside the kept training window: {missing[:3]}")
    newest = len(kept) - 1
    if window.scheme == "equal":
        raw = [1.0] * len(sample_sessions)
    else:
        decay = math.log(2.0) / float(window.half_life_sessions)  # type: ignore[arg-type]
        raw = [math.exp(-decay * (newest - position[d])) for d in sample_sessions]
    mean = sum(raw) / len(raw) if raw else 1.0
    return [w / mean for w in raw]


def comparison_grid(window_sizes: Sequence[int | None], half_lives: Sequence[float]) -> list[TrainingWindow]:
    """Equal-weight baselines plus exponential variants for each window size (pre-register before use)."""
    grid = []
    for size in window_sizes:
        grid.append(TrainingWindow("equal", size))
        grid.extend(TrainingWindow("exponential", size, h) for h in half_lives)
    return grid


class IntradayFeatureSource(Protocol):
    """Session-level summaries of minute data, available only after that session closes.

    ``features(symbol, session)`` returns numeric features computed from minute bars of
    ``session`` only, or ``None`` when no accepted minute data exists. ``available_at``
    is the ISO UTC instant after which the features may be used.
    """

    def features(self, symbol: str, session: str) -> Mapping[str, float] | None: ...

    def available_at(self, symbol: str, session: str) -> str: ...


class NoIntradayData:
    """Daily-only placeholder until minute access is authorized: every lookup is absent."""

    def features(self, symbol: str, session: str) -> Mapping[str, float] | None:
        return None

    def available_at(self, symbol: str, session: str) -> str:
        return session + "T10:00:00Z"  # 15:30 IST close; unused when features are absent


def join_timeframes(symbol: str, daily_rows: Sequence[Mapping[str, float | str]], intraday: IntradayFeatureSource,
                    decision_time_utc: str = "T10:00:00Z", prefix: str = "intraday_") -> list[dict[str, object]]:
    """Attach intraday features to each daily row without look-ahead.

    The row for session ``d`` is a decision made at ``d``'s close (executed at the next
    session's open), so it may use intraday features of ``d`` only if they were available
    by ``d + decision_time_utc``. Missing intraday data stays missing (``None``), never filled.
    """
    joined = []
    for row in daily_rows:
        session = str(row["date"])
        out: dict[str, object] = dict(row)
        values = intraday.features(symbol, session)
        if values is not None and intraday.available_at(symbol, session) > session + decision_time_utc:
            raise ValueError(f"{symbol} {session}: intraday features become available after the decision time")
        out[prefix + "present"] = values is not None
        for key, value in sorted((values or {}).items()):
            out[prefix + key] = value
        joined.append(out)
    return joined
