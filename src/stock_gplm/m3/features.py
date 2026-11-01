"""Deterministic, causal daily features computed from unadjusted validated candles.

A value at session t reads only candles at or before t. Series restart after an
unobserved session or a corporate-action effective date, so no window bridges a
gap and warm-up applies again. Undefined values stay None; nothing is imputed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterable, Sequence

from .evidence import SessionAxis
from .readiness import DatasetRow, _decimal, _ema, _macd, _rsi

FEATURE_CONTRACT = "stock-gplm.m32-features.v1"
ARITHMETIC = Context(prec=34, rounding=ROUND_HALF_EVEN)
SMA_PERIODS = (5, 10, 20, 50, 100, 200)
EMA_PERIODS = (9, 20, 50, 100, 200)
VOLUME_RATIO_PERIODS = (5, 20, 60)
VOLATILITY_PERIODS = (20, 60, 120)
MOMENTUM_PERIODS = (1, 5, 21, 63, 126, 252)
RANGE_PERIODS = (20, 252)
SMA_DISTANCE_PERIODS = (20, 50, 200)
WILDER_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_WIDTH = Decimal(2)

Series = list[Decimal | None]


@dataclass(frozen=True)
class FeatureSpec:
    """One column: its warm-up, and its memory where that is bounded."""

    name: str
    family: str
    min_observations: int
    lookback_sessions: int | None
    definition: str


def _specs() -> tuple[FeatureSpec, ...]:
    specs: list[FeatureSpec] = []

    def add(name: str, family: str, warm_up: int, lookback: int | None, definition: str) -> None:
        specs.append(FeatureSpec(name, family, warm_up, lookback, definition))

    for period in SMA_PERIODS:
        add(f"sma_{period}", "trend", period, period, f"mean close over {period} sessions")
    for period in EMA_PERIODS:
        add(f"ema_{period}", "trend", period, None, f"close EMA seeded by SMA({period})")
    add("rsi_14", "momentum", 15, None, "Wilder RSI of close changes")
    add("macd_line", "momentum", 26, None, "EMA(12) minus EMA(26) of close")
    add("macd_signal", "momentum", 34, None, "EMA(9) of macd_line")
    add("macd_histogram", "momentum", 34, None, "macd_line minus macd_signal")
    add("atr_14", "volatility", 15, None, "Wilder mean of true range")
    add("atr_14_pct", "volatility", 15, None, "atr_14 divided by close")
    add("bb_mid_20", "volatility", 20, 20, "mean close over 20 sessions")
    add("bb_upper_20", "volatility", 20, 20, "bb_mid_20 plus 2 population deviations")
    add("bb_lower_20", "volatility", 20, 20, "bb_mid_20 minus 2 population deviations")
    add("bb_percent_b_20", "volatility", 20, 20, "close position between the bands; None when equal")
    add("bb_bandwidth_20", "volatility", 20, 20, "band width divided by bb_mid_20")
    add("plus_di_14", "trend", 15, None, "100 * Wilder +DM / atr_14; 0 when atr_14 is 0")
    add("minus_di_14", "trend", 15, None, "100 * Wilder -DM / atr_14; 0 when atr_14 is 0")
    add("adx_14", "trend", 28, None, "Wilder mean of DX; DX is 0 when both DIs are 0")
    for period in VOLUME_RATIO_PERIODS:
        add(
            f"volume_ratio_{period}", "volume", period + 1, period + 1,
            f"volume over mean of the prior {period} sessions; None when that mean is 0",
        )
    for period in VOLATILITY_PERIODS:
        add(
            f"volatility_{period}", "volatility", period + 1, period + 1,
            f"sample deviation of {period} simple close returns, not annualized",
        )
    for period in MOMENTUM_PERIODS:
        add(f"momentum_{period}", "momentum", period + 1, period + 1, f"close over close {period} sessions ago, minus 1")
    add("close_location", "structure", 1, 1, "(close - low) / (high - low); None when high equals low")
    add("gap_open", "structure", 2, 2, "open over prior close, minus 1")
    add("range_pct", "structure", 1, 1, "(high - low) / close")
    add("body_pct", "structure", 1, 1, "(close - open) / open")
    add("upper_wick_pct", "structure", 1, 1, "(high - max(open, close)) / close")
    add("lower_wick_pct", "structure", 1, 1, "(min(open, close) - low) / close")
    for period in RANGE_PERIODS:
        add(f"pct_from_high_{period}", "structure", period, period, f"close over {period}-session high, minus 1")
        add(f"pct_from_low_{period}", "structure", period, period, f"close over {period}-session low, minus 1")
    for period in SMA_DISTANCE_PERIODS:
        add(f"close_to_sma_{period}", "structure", period, period, f"close over sma_{period}, minus 1")
    return tuple(specs)


FEATURE_SPECS = _specs()
FEATURE_NAMES = tuple(item.name for item in FEATURE_SPECS)


@dataclass(frozen=True)
class FeatureRow:
    """Features knowable after the close of ``as_of_date``."""

    symbol: str
    as_of_date: str
    run_index: int
    run_observation: int
    source_path: str
    source_sha256: str
    values: dict[str, str | None]


def _rolling_mean(values: Sequence[Decimal], window: int) -> Series:
    result: Series = [None] * len(values)
    total = Decimal(0)
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index >= window - 1:
            result[index] = total / window
    return result


def _rolling_deviation(values: Sequence[Decimal], window: int, *, sample: bool) -> Series:
    result: Series = [None] * len(values)
    total = squares = Decimal(0)
    for index, value in enumerate(values):
        total += value
        squares += value * value
        if index >= window:
            total -= values[index - window]
            squares -= values[index - window] * values[index - window]
        if index >= window - 1:
            variance = (squares - total * total / window) / (window - 1 if sample else window)
            result[index] = max(variance, Decimal(0)).sqrt()
    return result


def _rolling_extreme(values: Sequence[Decimal], window: int, *, largest: bool) -> Series:
    result: Series = [None] * len(values)
    candidates: deque[int] = deque()
    for index, value in enumerate(values):
        while candidates and (
            values[candidates[-1]] <= value if largest else values[candidates[-1]] >= value
        ):
            candidates.pop()
        candidates.append(index)
        if candidates[0] <= index - window:
            candidates.popleft()
        if index >= window - 1:
            result[index] = values[candidates[0]]
    return result


def _wilder(values: Sequence[Decimal], period: int, start: int) -> Series:
    """Wilder smoothing of ``values[start:]``, seeded by their first ``period`` mean."""
    result: Series = [None] * len(values)
    seed_end = start + period
    if len(values) < seed_end:
        return result
    current = sum(values[start:seed_end], Decimal(0)) / period
    result[seed_end - 1] = current
    for index in range(seed_end, len(values)):
        current = (current * (period - 1) + values[index]) / period
        result[index] = current
    return result


def _ratio_minus_one(numerators: Sequence[Decimal], denominators: Series) -> Series:
    return [
        value / base - 1 if base is not None and base != 0 else None
        for value, base in zip(numerators, denominators)
    ]


def _run_features(rows: Sequence[DatasetRow]) -> dict[str, Series]:
    opens = [Decimal(item.open) for item in rows]
    highs = [Decimal(item.high) for item in rows]
    lows = [Decimal(item.low) for item in rows]
    closes = [Decimal(item.close) for item in rows]
    volumes = [Decimal(item.volume) for item in rows]
    count = len(rows)
    output: dict[str, Series] = {}

    means = {period: _rolling_mean(closes, period) for period in {*SMA_PERIODS, *SMA_DISTANCE_PERIODS}}
    for period in SMA_PERIODS:
        output[f"sma_{period}"] = means[period]
    for period in EMA_PERIODS:
        output[f"ema_{period}"] = _ema(closes, period)
    output["rsi_14"] = _rsi(closes, WILDER_PERIOD)
    output["macd_line"], output["macd_signal"], output["macd_histogram"] = _macd(closes)

    true_range = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, count)
    ]
    atr = _wilder(true_range, WILDER_PERIOD, 1)
    output["atr_14"] = atr
    output["atr_14_pct"] = [
        value / close if value is not None else None for value, close in zip(atr, closes)
    ]

    deviation = _rolling_deviation(closes, BOLLINGER_PERIOD, sample=False)
    middle = _rolling_mean(closes, BOLLINGER_PERIOD)
    upper = [m + BOLLINGER_WIDTH * d if m is not None and d is not None else None for m, d in zip(middle, deviation)]
    lower = [m - BOLLINGER_WIDTH * d if m is not None and d is not None else None for m, d in zip(middle, deviation)]
    output["bb_mid_20"], output["bb_upper_20"], output["bb_lower_20"] = middle, upper, lower
    output["bb_percent_b_20"] = [
        (close - low) / (high - low) if high is not None and low is not None and high != low else None
        for close, high, low in zip(closes, upper, lower)
    ]
    output["bb_bandwidth_20"] = [
        (high - low) / mid if mid is not None else None for high, low, mid in zip(upper, lower, middle)
    ]

    plus_move = [Decimal(0)] * count
    minus_move = [Decimal(0)] * count
    for i in range(1, count):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_move[i] = up if up > down and up > 0 else Decimal(0)
        minus_move[i] = down if down > up and down > 0 else Decimal(0)
    plus_smooth = _wilder(plus_move, WILDER_PERIOD, 1)
    minus_smooth = _wilder(minus_move, WILDER_PERIOD, 1)

    def directional(smooth: Series) -> Series:
        return [
            (Decimal(100) * value / range_ if range_ != 0 else Decimal(0))
            if value is not None and range_ is not None else None
            for value, range_ in zip(smooth, atr)
        ]

    plus_di, minus_di = directional(plus_smooth), directional(minus_smooth)
    output["plus_di_14"], output["minus_di_14"] = plus_di, minus_di
    dx = [
        Decimal(100) * abs(p - m) / (p + m) if p + m != 0 else Decimal(0)
        for p, m in zip(plus_di, minus_di) if p is not None and m is not None
    ]
    adx: Series = [None] * count
    for offset, value in enumerate(_wilder(dx, WILDER_PERIOD, 0)):
        adx[WILDER_PERIOD + offset] = value
    output["adx_14"] = adx

    for period in VOLUME_RATIO_PERIODS:
        prior = _rolling_mean(volumes, period)
        output[f"volume_ratio_{period}"] = [None] * count
        for i in range(period, count):
            base = prior[i - 1]
            if base is not None and base != 0:
                output[f"volume_ratio_{period}"][i] = volumes[i] / base

    returns = [right / left - 1 for left, right in zip(closes, closes[1:])]
    for period in VOLATILITY_PERIODS:
        output[f"volatility_{period}"] = [None] + _rolling_deviation(returns, period, sample=True)
    for period in MOMENTUM_PERIODS:
        output[f"momentum_{period}"] = [
            closes[i] / closes[i - period] - 1 if i >= period else None for i in range(count)
        ]

    output["close_location"] = [
        (c - l) / (h - l) if h != l else None for c, h, l in zip(closes, highs, lows)
    ]
    output["gap_open"] = [None] + [opens[i] / closes[i - 1] - 1 for i in range(1, count)]
    output["range_pct"] = [(h - l) / c for h, l, c in zip(highs, lows, closes)]
    output["body_pct"] = [(c - o) / o for c, o in zip(closes, opens)]
    output["upper_wick_pct"] = [(h - max(o, c)) / c for h, o, c in zip(highs, opens, closes)]
    output["lower_wick_pct"] = [(min(o, c) - l) / c for l, o, c in zip(lows, opens, closes)]
    for period in RANGE_PERIODS:
        output[f"pct_from_high_{period}"] = _ratio_minus_one(closes, _rolling_extreme(highs, period, largest=True))
        output[f"pct_from_low_{period}"] = _ratio_minus_one(closes, _rolling_extreme(lows, period, largest=False))
    for period in SMA_DISTANCE_PERIODS:
        output[f"close_to_sma_{period}"] = _ratio_minus_one(closes, means[period])
    return output


def _check_rows(rows: Sequence[DatasetRow]) -> None:
    if len({item.symbol for item in rows}) > 1:
        raise ValueError("feature input must contain one symbol")
    days = [item.trading_date for item in rows]
    if any(left >= right for left, right in zip(days, days[1:])):
        raise ValueError("feature input must be strictly ascending by trading date")


def split_runs(
    rows: Sequence[DatasetRow], axis: SessionAxis, discontinuities: Iterable[str] = (),
) -> list[list[DatasetRow]]:
    """Split where a session is unobserved or a corporate action takes effect."""
    breaks = frozenset(discontinuities)
    runs: list[list[DatasetRow]] = []
    previous: int | None = None
    for row in rows:
        position = axis.position(row.trading_date)
        if previous is None or position != previous + 1 or row.trading_date in breaks:
            runs.append([])
        runs[-1].append(row)
        previous = position
    return runs


def compute_symbol_features(
    rows: Sequence[DatasetRow], axis: SessionAxis, discontinuities: Iterable[str] = (),
) -> list[FeatureRow]:
    """Compute every registered feature for one symbol's ascending candles."""
    _check_rows(rows)
    result: list[FeatureRow] = []
    with localcontext(ARITHMETIC):
        for run_index, run in enumerate(split_runs(rows, axis, discontinuities)):
            columns = _run_features(run)
            for position, row in enumerate(run):
                values: dict[str, str | None] = {}
                for name in FEATURE_NAMES:
                    value = columns[name][position]
                    values[name] = _decimal(value) if value is not None else None
                result.append(FeatureRow(
                    row.symbol, row.trading_date, run_index, position + 1,
                    row.source_path, row.source_sha256, values,
                ))
    return result
