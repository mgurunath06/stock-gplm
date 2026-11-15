"""Deterministic synthetic market fixtures (seeded; no real candles are ever read)."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..app import strategy_dsl as dsl
from ..m54.backtest import LaneMarket

SEED = 20261115


def ohlcv(n: int = 1500, seed: int = SEED, drift: float = 0.0003, vol: float = 0.015,
          regime_flip: bool = True) -> pd.DataFrame:
    """Random-walk OHLCV with trending, ranging and volatile regimes so most rules fire somewhere."""
    rng = np.random.default_rng(seed)
    mu = np.full(n, drift)
    sig = np.full(n, vol)
    if regime_flip:
        k = n // 5
        mu[k:2 * k], mu[2 * k:3 * k], mu[3 * k:4 * k] = 0.0015, -0.0012, 0.0
        sig[3 * k:4 * k] = vol * 2.0
        sig[4 * k:] = vol * 0.6
    r = rng.normal(mu, sig)
    close = 100 * np.exp(np.cumsum(r))
    gap = rng.normal(0, sig * 0.4)
    open_ = np.concatenate(([100.0], close[:-1])) * np.exp(gap)
    spread = np.abs(rng.normal(0, sig * 0.8)) + sig * 0.2
    high = np.maximum(open_, close) * (1 + spread * rng.uniform(0.2, 1.0, n))
    low = np.minimum(open_, close) * (1 - spread * rng.uniform(0.2, 1.0, n))
    volume = np.round(rng.lognormal(12, 0.5, n) * (1 + 3 * (np.abs(r) > 2 * sig)))
    sessions = pd.bdate_range("2019-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"session": sessions, "open": open_, "high": high, "low": low, "close": close,
                         "volume": volume.astype(float),
                         "end_ns": (pd.to_datetime(sessions).astype("int64") + 10 * 3600 * 10**9).to_numpy()})


def frame_from(o: Any, h: Any, lo: Any, c: Any, v: Any | None = None) -> pd.DataFrame:
    n = len(c)
    sessions = pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"session": sessions, "open": np.asarray(o, float), "high": np.asarray(h, float),
                         "low": np.asarray(lo, float), "close": np.asarray(c, float),
                         "volume": np.asarray(v if v is not None else np.full(n, 1000.0), float),
                         "end_ns": (pd.to_datetime(sessions).astype("int64") + 10 * 3600 * 10**9).to_numpy()})


def edge_cases(n: int = 120) -> dict[str, pd.DataFrame]:
    """Named edge-case series for indicator checks."""
    i = np.arange(n, dtype=float)
    out: dict[str, pd.DataFrame] = {}
    c = np.full(n, 50.0)
    out["constant"] = frame_from(c, c, c, c)
    c = 100 + i
    out["monotonic_up"] = frame_from(c - 0.5, c + 0.5, c - 1, c)
    c = 300 - i
    out["monotonic_down"] = frame_from(c + 0.5, c + 1, c - 0.5, c)
    c = 100 + 5 * np.sin(i / 3)
    out["zero_volume"] = frame_from(c, c + 1, c - 1, c, np.zeros(n))
    c = 100 + np.where(i % 2 == 0, 1.0, -1.0)
    out["alternating"] = frame_from(c, c + 0.5, c - 0.5, c)
    c = np.full(n, 100.0)
    c[n // 2] = 150.0
    out["single_spike"] = frame_from(c, np.maximum(c, 100.5), np.minimum(c, 99.5), c)
    c = 100 + np.cumsum(np.where(i % 7 < 4, 0.8, -0.6))
    out["short_series"] = frame_from(c[:10], c[:10] + 1, c[:10] - 1, c[:10])
    return out


def ctx(frame: pd.DataFrame, timeframe: str = "1d") -> dsl.Context:
    return dsl.Context.from_frame(frame, timeframe, False)


def market(frame: pd.DataFrame, natr: np.ndarray | float = 0.02, blocked: np.ndarray | None = None,
           no_signal: np.ndarray | None = None) -> LaneMarket:
    n = len(frame)
    na = np.full(n, float(natr)) if np.isscalar(natr) else np.asarray(natr, float)
    return LaneMarket("1d", frame["session"].to_numpy(), np.arange(n, dtype=np.int64),
                      frame["end_ns"].to_numpy(dtype=np.int64), *(frame[k].to_numpy(float) for k in
                                                                  ("open", "high", "low", "close", "volume")),
                      na, np.zeros(n, bool) if blocked is None else blocked,
                      np.zeros(n, bool) if no_signal is None else no_signal, False, 252.0)


def flat_path(n: int = 60, price: float = 100.0) -> pd.DataFrame:
    """Every bar opens/closes at ``price`` with a 0.5 range: the engine scenarios edit single bars."""
    p = np.full(n, price)
    return frame_from(p.copy(), p + 0.5, p - 0.5, p.copy())


def planted_edge(n: int = 1200, seed: int = SEED + 7) -> pd.DataFrame:
    """Random walk plus a real, discoverable edge: after 3 consecutive down closes the next 3 bars drift up.

    Used to show genetic programming finds structure that exists and is not handed to it.
    """
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0, 0.012, n)
    down = 0
    boost = np.zeros(n)
    for t in range(n):
        if boost[t]:
            r[t] = abs(r[t]) * 0.5 + 0.006
        down = down + 1 if r[t] < 0 else 0
        if down >= 3 and t + 3 < n:
            boost[t + 1:t + 4] = 1
            down = 0
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.concatenate(([100.0], close[:-1]))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    vol = rng.lognormal(12, 0.4, n)
    return frame_from(open_, high, low, close, vol)


def intraday(sessions: int = 40, bars_per_session: int = 25, seed: int = SEED + 3, minutes: int = 15) -> pd.DataFrame:
    """``minutes``-minute bars (default 15), 09:15-15:30 IST, ``minute_of_session`` = minutes from 09:15 to the bar end."""
    rng = np.random.default_rng(seed)
    n = sessions * bars_per_session
    r = rng.normal(0, 0.003, n)
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.concatenate(([100.0], close[:-1])) * np.exp(rng.normal(0, 0.0008, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0015, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0015, n)))
    days = pd.bdate_range("2026-01-05", periods=sessions)
    k = np.tile(np.arange(bars_per_session), sessions)
    sess = np.repeat(days.strftime("%Y-%m-%d"), bars_per_session)
    start_ist = pd.DatetimeIndex(np.repeat(days, bars_per_session)) + pd.to_timedelta(9 * 60 + 15 + minutes * k, unit="m")
    end_ns = (start_ist + pd.Timedelta(minutes=minutes) - pd.Timedelta(hours=5, minutes=30)).asi8
    return pd.DataFrame({"session": sess, "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.lognormal(9, 0.5, n), "end_ns": end_ns,
                         "minute_of_session": (minutes * (k + 1)).astype(float)})


def natr_of(frame: pd.DataFrame, n: int = 14) -> np.ndarray:
    from ..m54.indicators import _atr

    f = frame.reset_index(drop=True)
    return (_atr(f, n) / f["close"]).to_numpy(dtype=float)


def finite_max_abs(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    """Max |a-b| over bars where both are finite, and the count of NaN-pattern mismatches."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    fa, fb = np.isfinite(a), np.isfinite(b)
    both = fa & fb
    err = float(np.max(np.abs(a[both] - b[both]))) if both.any() else 0.0
    return err, int((fa != fb).sum())


def rel_err(a: np.ndarray, b: np.ndarray) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    both = np.isfinite(a) & np.isfinite(b)
    if not both.any():
        return 0.0
    return float(np.max(np.abs(a[both] - b[both]) / np.maximum(1.0, np.abs(b[both]))))


def first_converged(a: np.ndarray, b: np.ndarray, tol: float = 1e-6) -> int | None:
    """First bar after which |a-b|/max(1,|b|) stays below tol on every finite bar."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    both = np.isfinite(a) & np.isfinite(b)
    bad = both & (np.abs(a - b) / np.maximum(1.0, np.abs(b)) > tol)
    idx = np.nonzero(bad)[0]
    if not both.any():
        return None
    return int(idx[-1] + 1) if idx.size else int(np.nonzero(both)[0][0])


def isclose_or_nan(a: float, b: float, tol: float = 1e-9) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tol * max(1.0, abs(b))
