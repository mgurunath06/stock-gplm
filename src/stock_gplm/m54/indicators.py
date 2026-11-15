"""Extensible, causal indicator and feature registry for stock-behaviour discovery.

Every feature value at session ``t`` uses only bars ``<= t`` (closes, highs, lows,
opens and volumes of completed sessions). Features are expressed scale-free
(ratios, oscillators, z-scores or binary events) so thresholds are comparable
across stocks and price levels. Undefined warm-up values are ``NaN`` and never
satisfy a condition.

Indicators register themselves with :func:`register`. A search-space document
(``config/m54/search-space.json``) supplies parameter grids by indicator name; no
period or threshold is hard-coded as preferred. Indicators that need data this
repository does not hold (index/sector series, delivery volume) are listed in
:data:`UNSUPPORTED` with the input they require.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

import numpy as np
import pandas as pd

# Feature kinds decide how thresholds are generated:
#   bounded -> configured absolute levels (e.g. 0..100 oscillators) plus crossings
#   signed  -> discovery-quantile thresholds plus zero crossings
#   ratio   -> discovery-quantile thresholds only
#   binary  -> the event itself (value 1.0)
KINDS = ("bounded", "signed", "ratio", "binary")


@dataclass(frozen=True)
class FeatureDef:
    key: str
    family: str
    indicator: str
    params: tuple[tuple[str, int | float], ...]
    output: str
    kind: str
    levels: tuple[float, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "family": self.family, "indicator": self.indicator,
                "params": dict(self.params), "output": self.output, "kind": self.kind,
                "levels": list(self.levels)}


IndicatorFn = Callable[..., Mapping[str, pd.Series]]


@dataclass(frozen=True)
class Indicator:
    name: str
    family: str
    fn: IndicatorFn
    outputs: Mapping[str, str]  # output name -> kind
    levels: Mapping[str, tuple[float, ...]]
    constraint: Callable[[Mapping[str, Any]], bool] | None = None
    description: str = ""


REGISTRY: dict[str, Indicator] = {}

UNSUPPORTED: dict[str, str] = {
    "relative_strength_vs_index": "verified NIFTY/sector index daily series (none registered locally)",
    "beta_and_correlation": "verified benchmark index daily series",
    "sector_relative_momentum": "point-in-time sector classification and sector index series",
    "broad_market_regime": "verified broad-market index series and breadth data",
    "delivery_volume": "verified NSE delivery-quantity data (not in Shoonya daily V1 records)",
    "intraday_vwap": "minute data (separate authorization required)",
    "divergence_swing_based": "implemented only in causal rolling form (price vs oscillator slope); "
                              "pivot-based divergence needs confirmation lag rules not yet specified",
}


def register(name: str, family: str, outputs: Mapping[str, str], *, levels: Mapping[str, tuple[float, ...]] | None = None,
             constraint: Callable[[Mapping[str, Any]], bool] | None = None, description: str = "") -> Callable[[IndicatorFn], IndicatorFn]:
    def wrap(fn: IndicatorFn) -> IndicatorFn:
        if name in REGISTRY:
            raise ValueError(f"indicator {name} registered twice")
        if any(kind not in KINDS for kind in outputs.values()):
            raise ValueError(f"{name}: unknown output kind")
        REGISTRY[name] = Indicator(name, family, fn, dict(outputs), dict(levels or {}), constraint, description)
        return fn
    return wrap


# --------------------------------------------------------------------------- primitives

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _seeded_recursive(s: pd.Series, n: int, alpha: float) -> pd.Series:
    """Exponential smoothing seeded by the SMA of the first ``n`` defined values (V1 convention).

    Leading NaNs (warm-up of an input indicator) are skipped. A NaN after the seed yields NaN
    for that session and leaves the state unchanged; it is never filled.
    """
    x = s.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)
    finite = np.nonzero(np.isfinite(x))[0]
    if finite.size < n:
        return pd.Series(out, index=s.index)
    first = int(finite[0])
    window = x[first:first + n]
    if not np.isfinite(window).all():
        return pd.Series(out, index=s.index)
    prior = float(window.mean())
    out[first + n - 1] = prior
    for i in range(first + n, len(x)):
        v = x[i]
        if np.isfinite(v):
            prior = alpha * v + (1 - alpha) * prior
            out[i] = prior
    return pd.Series(out, index=s.index)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return _seeded_recursive(s, n, 2.0 / (n + 1.0))


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return _seeded_recursive(s, n, 1.0 / n)


def _wma(s: pd.Series, n: int) -> pd.Series:
    weights = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, weights) / weights.sum()), raw=True)


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1, skipna=False)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    return _wilder(_true_range(df), n)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.where(b != 0)


def _ma(kind: str, df: pd.DataFrame, n: int) -> pd.Series:
    close = df["close"]
    if kind == "SMA":
        return _sma(close, n)
    if kind == "EMA":
        return _ema(close, n)
    if kind == "WMA":
        return _wma(close, n)
    if kind == "HMA":
        half = max(1, n // 2)
        return _wma(2 * _wma(close, half) - _wma(close, n), max(1, int(round(np.sqrt(n)))))
    if kind == "DEMA":
        e1 = _ema(close, n)
        return 2 * e1 - _ema(e1, n)
    if kind == "TEMA":
        e1 = _ema(close, n)
        e2 = _ema(e1, n)
        return 3 * e1 - 3 * e2 + _ema(e2, n)
    if kind == "VWMA":
        return _safe_div((close * df["volume"]).rolling(n, min_periods=n).sum(), df["volume"].rolling(n, min_periods=n).sum())
    if kind == "KAMA":
        change = (close - close.shift(n)).abs()
        vol = close.diff().abs().rolling(n, min_periods=n).sum()
        er = _safe_div(change, vol).fillna(0.0)
        fast, slow = 2 / 3, 2 / 31
        sc = (er * (fast - slow) + slow) ** 2
        values = close.to_numpy(dtype=float)
        out = np.full(len(values), np.nan)
        scv = sc.to_numpy()
        if len(values) > n:
            out[n] = values[n]
            for i in range(n + 1, len(values)):
                out[i] = out[i - 1] + scv[i] * (values[i] - out[i - 1])
        return pd.Series(out, index=close.index)
    raise ValueError(kind)


# --------------------------------------------------------------------------- A. moving averages

_MA_OUT = {"distance": "signed", "slope": "signed", "acceleration": "signed", "touch_bounce": "binary",
           "atr_distance": "signed"}


def _ma_features(kind: str) -> IndicatorFn:
    def fn(df: pd.DataFrame, period: int, slope_lag: int = 5) -> dict[str, pd.Series]:
        ma = _ma(kind, df, period)
        slope = ma / ma.shift(slope_lag) - 1
        atr = _atr(df, 14)
        # Pullback-and-bounce: today's low reached the average from above, close held above it.
        bounce = ((df["low"] <= ma) & (df["close"] > ma) & (df["close"].shift(1) > ma.shift(1))).astype(float)
        bounce[ma.isna()] = np.nan
        return {"distance": df["close"] / ma - 1, "slope": slope, "acceleration": slope - slope.shift(slope_lag),
                "touch_bounce": bounce, "atr_distance": _safe_div(df["close"] - ma, atr)}
    return fn


for _kind in ("SMA", "EMA", "WMA", "HMA", "DEMA", "TEMA", "VWMA", "KAMA"):
    register(_kind, "moving_average", _MA_OUT, description=f"{_kind}: distance, slope, acceleration, bounce")(_ma_features(_kind))


@register("MA_CROSS", "moving_average", {"spread": "signed", "compression": "ratio"},
          constraint=lambda p: p["fast"] < p["slow"], description="fast/slow average spread; crossings are zero crossings")
def _ma_cross(df: pd.DataFrame, fast: int, slow: int, ma_type: str = "EMA") -> dict[str, pd.Series]:
    f, s = _ma(ma_type, df, fast), _ma(ma_type, df, slow)
    spread = f / s - 1
    return {"spread": spread, "compression": spread.abs()}


@register("MA_ALIGNMENT", "moving_average", {"score": "signed"},
          constraint=lambda p: p["short"] < p["medium"] < p["long"], description="+1 bullish stack, -1 bearish stack")
def _ma_alignment(df: pd.DataFrame, short: int, medium: int, long: int) -> dict[str, pd.Series]:
    a, b, c = _ema(df["close"], short), _ema(df["close"], medium), _ema(df["close"], long)
    score = ((a > b) & (b > c)).astype(float) - ((a < b) & (b < c)).astype(float)
    score[c.isna()] = np.nan
    return {"score": score}


# --------------------------------------------------------------------------- B. momentum

@register("RSI", "momentum", {"value": "bounded", "slope": "signed"}, levels={"value": ()})
def _rsi(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    delta = df["close"].diff()
    gain, loss = _wilder(delta.clip(lower=0), period), _wilder((-delta).clip(lower=0), period)
    rsi = 100 - 100 / (1 + _safe_div(gain, loss))
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100.0).where(~((loss == 0) & (gain == 0)), 50.0)
    return {"value": rsi, "slope": rsi - rsi.shift(3)}


@register("MACD", "momentum", {"line_norm": "signed", "hist_norm": "signed", "hist_slope": "signed"},
          constraint=lambda p: p["fast"] < p["slow"])
def _macd(df: pd.DataFrame, fast: int, slow: int, signal: int) -> dict[str, pd.Series]:
    line = _ema(df["close"], fast) - _ema(df["close"], slow)
    hist = line - _ema(line, signal)
    norm = df["close"]
    return {"line_norm": line / norm, "hist_norm": hist / norm, "hist_slope": (hist - hist.shift(1)) / norm}


@register("PPO", "momentum", {"value": "signed"}, constraint=lambda p: p["fast"] < p["slow"])
def _ppo(df: pd.DataFrame, fast: int, slow: int) -> dict[str, pd.Series]:
    s = _ema(df["close"], slow)
    return {"value": (_ema(df["close"], fast) - s) / s * 100}


@register("ROC", "momentum", {"value": "signed"})
def _roc(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    return {"value": df["close"] / df["close"].shift(period) - 1}


@register("CCI", "momentum", {"value": "signed"})
def _cci(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mean = _sma(tp, period)
    mad = tp.rolling(period, min_periods=period).apply(lambda x: float(np.mean(np.abs(x - x.mean()))), raw=True)
    return {"value": _safe_div(tp - mean, 0.015 * mad)}


@register("STOCH", "momentum", {"k": "bounded", "d": "bounded", "k_minus_d": "signed"}, levels={"k": (), "d": ()})
def _stoch(df: pd.DataFrame, period: int, smooth: int = 3) -> dict[str, pd.Series]:
    lo, hi = df["low"].rolling(period, min_periods=period).min(), df["high"].rolling(period, min_periods=period).max()
    k = _sma(100 * _safe_div(df["close"] - lo, hi - lo), smooth)
    d = _sma(k, 3)
    return {"k": k, "d": d, "k_minus_d": k - d}


@register("STOCH_RSI", "momentum", {"value": "bounded"}, levels={"value": ()})
def _stoch_rsi(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    rsi = _rsi(df, period)["value"]
    lo, hi = rsi.rolling(period, min_periods=period).min(), rsi.rolling(period, min_periods=period).max()
    return {"value": 100 * _safe_div(rsi - lo, hi - lo)}


@register("WILLIAMS_R", "momentum", {"value": "bounded"}, levels={"value": ()})
def _willr(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    lo, hi = df["low"].rolling(period, min_periods=period).min(), df["high"].rolling(period, min_periods=period).max()
    return {"value": 100 + (-100 * _safe_div(hi - df["close"], hi - lo))}  # rescaled to 0..100


@register("TRIX", "momentum", {"value": "signed"})
def _trix(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    e3 = _ema(_ema(_ema(df["close"], period), period), period)
    return {"value": e3 / e3.shift(1) - 1}


@register("TSI", "momentum", {"value": "signed"}, constraint=lambda p: p["short"] < p["long"])
def _tsi(df: pd.DataFrame, long: int, short: int) -> dict[str, pd.Series]:
    m = df["close"].diff()
    return {"value": 100 * _safe_div(_ema(_ema(m, long), short), _ema(_ema(m.abs(), long), short))}


@register("MOMENTUM_DIVERGENCE", "momentum", {"bearish": "signed"},
          description="causal rolling divergence: price slope sign minus RSI slope sign over the window")
def _divergence(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    rsi = _rsi(df, 14)["value"]
    price_up = np.sign(df["close"] - df["close"].shift(period))
    rsi_up = np.sign(rsi - rsi.shift(period))
    return {"bearish": (price_up - rsi_up) / 2}


# --------------------------------------------------------------------------- C. volume

@register("RVOL", "volume", {"value": "ratio", "spike": "binary"})
def _rvol(df: pd.DataFrame, period: int, spike: float = 2.0) -> dict[str, pd.Series]:
    base = _sma(df["volume"], period).shift(1)  # baseline excludes today
    value = _safe_div(df["volume"], base)
    return {"value": value, "spike": (value >= spike).astype(float).where(value.notna())}


@register("OBV", "volume", {"zslope": "signed"})
def _obv(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    obv = (np.sign(df["close"].diff()).fillna(0) * df["volume"]).cumsum()
    change = obv - obv.shift(period)
    return {"zslope": _safe_div(change, df["volume"].rolling(period, min_periods=period).sum())}


@register("AD_LINE", "volume", {"zslope": "signed"})
def _ad(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    mfm = _safe_div((df["close"] - df["low"]) - (df["high"] - df["close"]), df["high"] - df["low"]).fillna(0)
    ad = (mfm * df["volume"]).cumsum()
    return {"zslope": _safe_div(ad - ad.shift(period), df["volume"].rolling(period, min_periods=period).sum())}


@register("CMF", "volume", {"value": "signed"})
def _cmf(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    mfm = _safe_div((df["close"] - df["low"]) - (df["high"] - df["close"]), df["high"] - df["low"]).fillna(0)
    return {"value": _safe_div((mfm * df["volume"]).rolling(period, min_periods=period).sum(),
                               df["volume"].rolling(period, min_periods=period).sum())}


@register("MFI", "volume", {"value": "bounded"}, levels={"value": ()})
def _mfi(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    flow = tp * df["volume"]
    up = flow.where(tp > tp.shift(1), 0.0).rolling(period, min_periods=period).sum()
    down = flow.where(tp < tp.shift(1), 0.0).rolling(period, min_periods=period).sum()
    return {"value": 100 - 100 / (1 + _safe_div(up, down))}


@register("PVO", "volume", {"value": "signed"}, constraint=lambda p: p["fast"] < p["slow"])
def _pvo(df: pd.DataFrame, fast: int, slow: int) -> dict[str, pd.Series]:
    s = _ema(df["volume"], slow)
    return {"value": _safe_div(_ema(df["volume"], fast) - s, s) * 100}


@register("PRICE_VOLUME", "volume", {"up_on_volume": "binary", "down_on_volume": "binary", "breakout_on_volume": "binary"})
def _price_volume(df: pd.DataFrame, period: int, multiple: float = 1.5) -> dict[str, pd.Series]:
    rvol = _safe_div(df["volume"], _sma(df["volume"], period).shift(1))
    ret = df["close"].pct_change()
    prior_high = df["high"].rolling(period, min_periods=period).max().shift(1)
    valid = rvol.notna()
    return {"up_on_volume": ((ret > 0) & (rvol >= multiple)).astype(float).where(valid),
            "down_on_volume": ((ret < 0) & (rvol >= multiple)).astype(float).where(valid),
            "breakout_on_volume": ((df["close"] > prior_high) & (rvol >= multiple)).astype(float).where(valid & prior_high.notna())}


# --------------------------------------------------------------------------- D. trend strength

def _dmi(df: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    up, down = df["high"].diff(), -df["low"].diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    atr = _atr(df, period)
    pdi, mdi = 100 * _safe_div(_wilder(plus, period), atr), 100 * _safe_div(_wilder(minus, period), atr)
    dx = 100 * _safe_div((pdi - mdi).abs(), pdi + mdi)
    return pdi, mdi, _wilder(dx, period)


@register("ADX", "trend", {"adx": "bounded", "di_spread": "signed", "adx_slope": "signed"}, levels={"adx": ()})
def _adx(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    pdi, mdi, adx = _dmi(df, period)
    return {"adx": adx, "di_spread": pdi - mdi, "adx_slope": adx - adx.shift(5)}


@register("AROON", "trend", {"oscillator": "signed"})
def _aroon(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    up = df["high"].rolling(period + 1, min_periods=period + 1).apply(lambda x: float(np.argmax(x)), raw=True) / period * 100
    down = df["low"].rolling(period + 1, min_periods=period + 1).apply(lambda x: float(np.argmin(x)), raw=True) / period * 100
    return {"oscillator": up - down}


@register("SUPERTREND", "trend", {"direction": "signed", "flip_up": "binary"})
def _supertrend(df: pd.DataFrame, period: int, multiplier: float) -> dict[str, pd.Series]:
    atr = _atr(df, period).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    n = len(close)
    upper, lower, direction = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        bu, bl = hl2[i] + multiplier * atr[i], hl2[i] - multiplier * atr[i]
        if i == 0 or np.isnan(upper[i - 1]):
            upper[i], lower[i], direction[i] = bu, bl, 1.0
            continue
        upper[i] = bu if (bu < upper[i - 1] or close[i - 1] > upper[i - 1]) else upper[i - 1]
        lower[i] = bl if (bl > lower[i - 1] or close[i - 1] < lower[i - 1]) else lower[i - 1]
        if direction[i - 1] < 0 and close[i] > upper[i - 1]:
            direction[i] = 1.0
        elif direction[i - 1] > 0 and close[i] < lower[i - 1]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]
    d = pd.Series(direction, index=df.index)
    flip = ((d > 0) & (d.shift(1) < 0)).astype(float).where(d.shift(1).notna())
    return {"direction": d, "flip_up": flip}


@register("ICHIMOKU", "trend", {"tenkan_kijun": "signed", "above_cloud": "signed"},
          constraint=lambda p: p["tenkan"] < p["kijun"] < p["senkou"])
def _ichimoku(df: pd.DataFrame, tenkan: int, kijun: int, senkou: int) -> dict[str, pd.Series]:
    def mid(n: int) -> pd.Series:
        return (df["high"].rolling(n, min_periods=n).max() + df["low"].rolling(n, min_periods=n).min()) / 2
    t, k = mid(tenkan), mid(kijun)
    # The cloud plotted at t was computed `kijun` sessions earlier, so shifting forward is causal.
    span_a, span_b = ((t + k) / 2).shift(kijun), mid(senkou).shift(kijun)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1, skipna=False)
    bottom = pd.concat([span_a, span_b], axis=1).min(axis=1, skipna=False)
    above = (df["close"] > top).astype(float) - (df["close"] < bottom).astype(float)
    above[top.isna()] = np.nan
    return {"tenkan_kijun": t / k - 1, "above_cloud": above}


@register("PSAR", "trend", {"direction": "signed"})
def _psar(df: pd.DataFrame, step: float, maximum: float) -> dict[str, pd.Series]:
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(high)
    direction = np.full(n, np.nan)
    if n < 2:
        return {"direction": pd.Series(direction, index=df.index)}
    bull, af, ep, sar = True, step, high[0], low[0]
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < sar:
                bull, sar, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, maximum)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > sar:
                bull, sar, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, maximum)
        direction[i] = 1.0 if bull else -1.0
    direction[:10] = np.nan  # warm-up
    return {"direction": pd.Series(direction, index=df.index)}


@register("VORTEX", "trend", {"spread": "signed"})
def _vortex(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    vp = (df["high"] - df["low"].shift(1)).abs().rolling(period, min_periods=period).sum()
    vm = (df["low"] - df["high"].shift(1)).abs().rolling(period, min_periods=period).sum()
    tr = _true_range(df).rolling(period, min_periods=period).sum()
    return {"spread": _safe_div(vp - vm, tr)}


# --------------------------------------------------------------------------- E. volatility

@register("ATR", "volatility", {"natr": "ratio", "natr_change": "signed"})
def _atr_ind(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    natr = _atr(df, period) / df["close"]
    return {"natr": natr, "natr_change": natr / natr.shift(period) - 1}


@register("BOLLINGER", "volatility", {"percent_b": "signed", "bandwidth": "ratio", "squeeze_release": "binary"})
def _bollinger(df: pd.DataFrame, period: int, width: float) -> dict[str, pd.Series]:
    mid, sd = _sma(df["close"], period), df["close"].rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + width * sd, mid - width * sd
    bw = _safe_div(upper - lower, mid)
    low_bw = bw.rolling(period * 3, min_periods=period * 3).quantile(0.2).shift(1)
    release = ((bw.shift(1) <= low_bw) & (df["close"] > upper)).astype(float).where(low_bw.notna())
    return {"percent_b": _safe_div(df["close"] - lower, upper - lower), "bandwidth": bw, "squeeze_release": release}


@register("KELTNER", "volatility", {"position": "signed"})
def _keltner(df: pd.DataFrame, period: int, multiplier: float) -> dict[str, pd.Series]:
    mid = _ema(df["close"], period)
    return {"position": _safe_div(df["close"] - mid, multiplier * _atr(df, period))}


@register("DONCHIAN", "volatility", {"position": "bounded", "breakout_up": "binary", "breakout_down": "binary"},
          levels={"position": ()})
def _donchian(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    hi = df["high"].rolling(period, min_periods=period).max().shift(1)
    lo = df["low"].rolling(period, min_periods=period).min().shift(1)
    valid = hi.notna()
    return {"position": 100 * _safe_div(df["close"] - lo, hi - lo),
            "breakout_up": (df["close"] > hi).astype(float).where(valid),
            "breakout_down": (df["close"] < lo).astype(float).where(valid)}


@register("HIST_VOL", "volatility", {"value": "ratio", "regime_ratio": "ratio"}, constraint=lambda p: p["short"] < p["long"])
def _hist_vol(df: pd.DataFrame, short: int, long: int) -> dict[str, pd.Series]:
    r = np.log(df["close"]).diff()
    s, lng = r.rolling(short, min_periods=short).std(), r.rolling(long, min_periods=long).std()
    return {"value": s * np.sqrt(252), "regime_ratio": _safe_div(s, lng)}


# --------------------------------------------------------------------------- F. price action

@register("BREAKOUT", "price_action", {"up": "binary", "failed_up": "binary", "retest": "binary", "distance_high": "signed"})
def _breakout(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    prior_high = df["high"].rolling(period, min_periods=period).max().shift(1)
    up = df["close"] > prior_high
    valid = prior_high.notna()
    # Failed breakout: yesterday broke out, today closes back below that breakout level.
    failed = up.shift(1, fill_value=False) & (df["close"] < prior_high.shift(1))
    # Retest: a breakout within the last 10 sessions, today's low revisits the old high, close holds above.
    recent = up.rolling(10, min_periods=1).max().shift(1).fillna(0).astype(bool)
    level = prior_high.where(up).ffill().shift(1)
    retest = recent & (df["low"] <= level * 1.005) & (df["close"] > level)
    return {"up": up.astype(float).where(valid), "failed_up": failed.astype(float).where(valid),
            "retest": retest.astype(float).where(valid), "distance_high": df["close"] / prior_high - 1}


@register("GAP", "price_action", {"gap": "signed"})
def _gap(df: pd.DataFrame, period: int = 1) -> dict[str, pd.Series]:
    # Known at the close of the gap day (the open is in the past by then).
    return {"gap": df["open"] / df["close"].shift(1) - 1}


@register("RANGE", "price_action", {"compression": "ratio", "nr": "binary", "inside_bar": "binary", "expansion": "ratio"})
def _range(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    rng = df["high"] - df["low"]
    avg = _sma(rng, period).shift(1)
    nr = (rng <= rng.rolling(period, min_periods=period).min()).astype(float).where(avg.notna())
    inside = ((df["high"] <= df["high"].shift(1)) & (df["low"] >= df["low"].shift(1))).astype(float)
    return {"compression": _safe_div(df["high"].rolling(period, min_periods=period).max() - df["low"].rolling(period, min_periods=period).min(), df["close"]),
            "nr": nr, "inside_bar": inside, "expansion": _safe_div(rng, avg)}


@register("STRUCTURE", "price_action", {"hh_hl_score": "signed"})
def _structure(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    hh = (df["high"] > df["high"].shift(1)).astype(float)
    hl = (df["low"] > df["low"].shift(1)).astype(float)
    ll = (df["low"] < df["low"].shift(1)).astype(float)
    lh = (df["high"] < df["high"].shift(1)).astype(float)
    score = ((hh + hl) - (ll + lh)).rolling(period, min_periods=period).sum() / (2 * period)
    return {"hh_hl_score": score}


@register("CANDLE", "price_action", {"body": "signed", "upper_wick": "ratio", "lower_wick": "ratio",
                                      "bullish_engulfing": "binary", "hammer": "binary", "close_location": "bounded"},
          levels={"close_location": ()})
def _candle(df: pd.DataFrame, period: int = 1) -> dict[str, pd.Series]:
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - lo).where(h > lo)
    body = c - o
    engulf = (body > 0) & (body.shift(1) < 0) & (c >= o.shift(1)) & (o <= c.shift(1))
    lower = (pd.concat([o, c], axis=1).min(axis=1) - lo)
    upper = (h - pd.concat([o, c], axis=1).max(axis=1))
    hammer = (lower >= 2 * body.abs()) & (upper <= body.abs()) & rng.notna()
    return {"body": body / rng, "upper_wick": upper / rng, "lower_wick": lower / rng,
            "bullish_engulfing": engulf.astype(float), "hammer": hammer.astype(float),
            "close_location": 100 * (c - lo) / rng}


@register("SUPPORT_RESISTANCE", "price_action", {"support_distance_atr": "ratio", "resistance_distance_atr": "ratio"})
def _sr(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    atr = _atr(df, 14)
    support = df["low"].rolling(period, min_periods=period).min().shift(1)
    resistance = df["high"].rolling(period, min_periods=period).max().shift(1)
    return {"support_distance_atr": _safe_div(df["close"] - support, atr),
            "resistance_distance_atr": _safe_div(resistance - df["close"], atr)}


# --------------------------------------------------------------------------- H. statistical

@register("STATS", "statistical", {"autocorr": "signed", "return_z": "signed", "streak": "signed",
                                    "skew": "signed", "vol_cluster": "ratio"})
def _stats(df: pd.DataFrame, period: int) -> dict[str, pd.Series]:
    r = df["close"].pct_change()
    sign = np.sign(r).fillna(0)
    groups = (sign != sign.shift(1)).cumsum()
    streak = sign * (sign.groupby(groups).cumcount() + 1)
    absr = r.abs()
    return {"autocorr": r.rolling(period, min_periods=period).corr(r.shift(1)),
            "return_z": _safe_div(r - r.rolling(period, min_periods=period).mean(), r.rolling(period, min_periods=period).std()),
            "streak": streak.where(r.notna()),
            "skew": r.rolling(period, min_periods=period).skew(),
            "vol_cluster": absr.rolling(period, min_periods=period).corr(absr.shift(1))}


# --------------------------------------------------------------------------- enumeration

def _param_grid(grid: Mapping[str, list[Any]]) -> Iterator[dict[str, Any]]:
    keys = sorted(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, combo))


def feature_key(indicator: str, params: Mapping[str, Any], output: str) -> str:
    inner = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{indicator}({inner}).{output}"


def build_features(df: pd.DataFrame, search_space: Mapping[str, Any], *, max_period: int | None = None
                   ) -> tuple[dict[str, np.ndarray], dict[str, FeatureDef], list[dict[str, Any]]]:
    """Compute every feature in the search space. Returns arrays, definitions and skip notes."""
    arrays: dict[str, np.ndarray] = {}
    defs: dict[str, FeatureDef] = {}
    skipped: list[dict[str, Any]] = []
    levels_cfg = search_space.get("levels", {})
    for name in sorted(search_space["indicators"]):
        spec = search_space["indicators"][name]
        if not spec.get("enabled", True):
            skipped.append({"indicator": name, "reason": "disabled in search space"})
            continue
        indicator = REGISTRY.get(name)
        if indicator is None:
            raise ValueError(f"search space names unknown indicator {name}")
        for params in _param_grid(spec.get("params", {})):
            if indicator.constraint is not None and not indicator.constraint(params):
                continue
            periods = [v for k, v in params.items() if isinstance(v, int) and k not in {"ma_type"}]
            if max_period is not None and periods and max(periods) > max_period:
                skipped.append({"indicator": name, "params": params, "reason": f"period exceeds budget max_period {max_period}"})
                continue
            outputs = indicator.fn(df, **params)
            for output, series in outputs.items():
                kind = indicator.outputs[output]
                key = feature_key(name, params, output)
                values = np.asarray(series.to_numpy(dtype=float), dtype=float)
                values[~np.isfinite(values)] = np.nan
                levels = tuple(float(x) for x in levels_cfg.get(f"{name}.{output}", levels_cfg.get(output, []))) \
                    if kind == "bounded" else ()
                arrays[key] = values
                defs[key] = FeatureDef(key, indicator.family, name, tuple(sorted(params.items())), output, kind, levels)
    return arrays, defs, skipped


def registry_summary() -> list[dict[str, Any]]:
    return [{"indicator": i.name, "family": i.family, "outputs": dict(i.outputs), "description": i.description}
            for i in sorted(REGISTRY.values(), key=lambda x: (x.family, x.name))]
