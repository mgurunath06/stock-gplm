"""Causal evaluation of validated strategy expression trees."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .schema import ValidatedStrategy, validate_strategy


@dataclass(frozen=True)
class EvaluationResult:
    dates: tuple[str, ...]
    entry_signals: tuple[bool, ...]
    exit_signals: tuple[bool, ...]
    indicators: Mapping[str, tuple[float | None, ...]]
    strategy_hash: str


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def normalize_candles(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Validate a chronological, completed-session OHLCV sequence."""
    normalized: list[dict[str, Any]] = []
    previous: date | None = None
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"candles[{index}] must be an object")
        actual = set(row)
        required = {"date", "open", "high", "low", "close", "volume"}
        if actual != required:
            raise ValueError(f"candles[{index}] fields must equal {sorted(required)}")
        try:
            session = date.fromisoformat(str(row["date"]))
        except ValueError as exc:
            raise ValueError(f"candles[{index}].date must be ISO YYYY-MM-DD") from exc
        if previous is not None and session <= previous:
            raise ValueError("candles must be unique and strictly chronological")
        previous = session
        opening = _number(row["open"], f"candles[{index}].open")
        high = _number(row["high"], f"candles[{index}].high")
        low = _number(row["low"], f"candles[{index}].low")
        close = _number(row["close"], f"candles[{index}].close")
        volume = _number(row["volume"], f"candles[{index}].volume")
        if min(opening, high, low, close) <= 0 or volume < 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError(f"candles[{index}] has invalid OHLCV bounds")
        normalized.append(
            {"date": session.isoformat(), "open": opening, "high": high, "low": low, "close": close, "volume": volume}
        )
    return tuple(normalized)


def _sma(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index + 1 >= period:
            output[index] = running / period
    return output


def _ema(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return output
    seed = sum(values[:period]) / period
    output[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prior = seed
    for index in range(period, len(values)):
        prior = values[index] * alpha + prior * (1.0 - alpha)
        output[index] = prior
    return output


def _rolling(values: list[float], period: int, highest: bool) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    function = max if highest else min
    for index in range(period - 1, len(values)):
        output[index] = function(values[index - period + 1 : index + 1])
    return output


def _percent_change(values: list[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    for index in range(period, len(values)):
        prior = values[index - period]
        output[index] = None if prior == 0 else values[index] / prior - 1.0
    return output


def _ema_partial(values: list[float | None], period: int) -> list[float | None]:
    """EMA over a series whose leading entries may be undefined (seeded by the first full SMA)."""
    output: list[float | None] = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None:
        return output
    tail = [float(v) for v in values[start:] if v is not None]
    if len(tail) != len(values) - start:
        raise AssertionError("undefined value after the first defined value")
    for offset, value in enumerate(_ema(tail, period)):
        output[start + offset] = value
    return output


def _rsi(values: list[float], period: int) -> list[float | None]:
    """Wilder RSI; the first value uses the simple mean of the first ``period`` changes."""
    output: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return output
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period

    def value(g: float, l: float) -> float:
        if l == 0.0:
            return 100.0 if g > 0.0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    output[period] = value(gain, loss)
    for index in range(period + 1, len(values)):
        change = changes[index - 1]
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
        output[index] = value(gain, loss)
    return output


def _macd(values: list[float], fast: int, slow: int, signal: int, output: str) -> list[float | None]:
    fast_ema, slow_ema = _ema(values, fast), _ema(values, slow)
    line = [None if f is None or s is None else f - s for f, s in zip(fast_ema, slow_ema)]
    if output == "line":
        return line
    signal_line = _ema_partial(line, signal)
    if output == "signal":
        return signal_line
    return [None if m is None or s is None else m - s for m, s in zip(line, signal_line)]


def _atr(candles: tuple[dict[str, Any], ...], period: int) -> list[float | None]:
    """Wilder average true range; the first bar has no prior close and is not used."""
    output: list[float | None] = [None] * len(candles)
    ranges = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
              for p, c in zip(candles, candles[1:])]
    if len(ranges) < period:
        return output
    average = sum(ranges[:period]) / period
    output[period] = average
    for index in range(period, len(ranges)):
        average = (average * (period - 1) + ranges[index]) / period
        output[index + 1] = average
    return output


def _volatility(values: list[float], period: int) -> list[float | None]:
    """Sample standard deviation of the last ``period`` daily log returns (not annualized)."""
    output: list[float | None] = [None] * len(values)
    returns = [None] + [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    for index in range(period, len(values)):
        window = [float(r) for r in returns[index - period + 1 : index + 1] if r is not None]
        mean = sum(window) / period
        output[index] = math.sqrt(sum((r - mean) ** 2 for r in window) / (period - 1))
    return output


def compute_indicators(config: Mapping[str, Any], candles: tuple[dict[str, Any], ...]) -> dict[str, list[float | None]]:
    result: dict[str, list[float | None]] = {}
    for item in config["indicators"]:
        values = [float(row[item["field"]]) for row in candles]
        kind = item["type"]
        if kind in {"SMA", "SMA_VOLUME"}:
            output = _sma(values, item["period"])
        elif kind == "EMA":
            output = _ema(values, item["period"])
        elif kind == "HIGHEST":
            output = _rolling(values, item["period"], True)
        elif kind == "LOWEST":
            output = _rolling(values, item["period"], False)
        elif kind == "PERCENT_CHANGE":
            output = _percent_change(values, item["period"])
        elif kind == "RSI":
            output = _rsi(values, item["period"])
        elif kind == "MACD":
            output = _macd(values, item["fast"], item["period"], item["signal"], item["output"])
        elif kind == "ATR":
            output = _atr(candles, item["period"])
        elif kind == "VOLATILITY":
            output = _volatility(values, item["period"])
        else:  # protected by schema validation
            raise AssertionError(f"unhandled indicator {kind}")
        result[item["id"]] = output
    return result


def _operand(
    node: Mapping[str, Any], index: int, candles: tuple[dict[str, Any], ...], indicators: Mapping[str, list[float | None]]
) -> float | None:
    if "constant" in node:
        return float(node["constant"])
    target = index + int(node.get("offset", 0))
    if target < 0 or target >= len(candles):
        return None
    if "field" in node:
        return float(candles[target][node["field"]])
    if "indicator" in node:
        return indicators[node["indicator"]][target]
    operator = node["operator"]
    if operator in {"HIGHEST", "LOWEST", "PERCENT_CHANGE"}:
        lookback = int(node["lookback"])
        source = node["operands"][0]
        if operator == "PERCENT_CHANGE":
            current = _operand(source, target, candles, indicators)
            previous = _operand(source, target - lookback, candles, indicators)
            if current is None or previous in {None, 0.0}:
                return None
            return current / previous - 1.0
        start = target - lookback + 1
        if start < 0:
            return None
        values = [_operand(source, position, candles, indicators) for position in range(start, target + 1)]
        if any(value is None for value in values):
            return None
        return (max if operator == "HIGHEST" else min)(float(value) for value in values if value is not None)
    values = [_operand(value, target, candles, indicators) for value in node["operands"]]
    if any(value is None for value in values):
        return None
    left, right = (float(value) for value in values)
    if operator == "ADD":
        return left + right
    if operator == "SUBTRACT":
        return left - right
    if operator == "MULTIPLY":
        return left * right
    if operator == "DIVIDE":
        return None if right == 0 else left / right
    raise AssertionError(f"unhandled numeric operator {operator}")


def _condition(
    node: Mapping[str, Any], index: int, candles: tuple[dict[str, Any], ...], indicators: Mapping[str, list[float | None]]
) -> bool | None:
    operator = node["operator"]
    if operator == "AND":
        values = [_condition(value, index, candles, indicators) for value in node["conditions"]]
        return False if False in values else (None if None in values else True)
    if operator == "OR":
        values = [_condition(value, index, candles, indicators) for value in node["conditions"]]
        return True if True in values else (None if None in values else False)
    if operator == "NOT":
        value = _condition(node["conditions"][0], index, candles, indicators)
        return None if value is None else not value
    left = _operand(node["left"], index, candles, indicators)
    right = _operand(node["right"], index, candles, indicators)
    if left is None or right is None:
        return None
    if operator == "GT":
        return left > right
    if operator == "GTE":
        return left >= right
    if operator == "LT":
        return left < right
    if operator == "LTE":
        return left <= right
    if operator == "EQ":
        return left == right
    if index < 1:
        return None
    prior_left = _operand(node["left"], index - 1, candles, indicators)
    prior_right = _operand(node["right"], index - 1, candles, indicators)
    if prior_left is None or prior_right is None:
        return None
    if operator == "CROSS_ABOVE":
        return prior_left <= prior_right and left > right
    if operator == "CROSS_BELOW":
        return prior_left >= prior_right and left < right
    raise AssertionError(f"unhandled comparison operator {operator}")


def evaluate_strategy(
    strategy: Mapping[str, Any] | ValidatedStrategy,
    rows: Sequence[Mapping[str, Any]],
) -> EvaluationResult:
    """Evaluate completed daily candles without accessing a future row."""
    validated = strategy if isinstance(strategy, ValidatedStrategy) else validate_strategy(strategy)
    candles = normalize_candles(rows)
    indicators = compute_indicators(validated.config, candles)
    entries = tuple(_condition(validated.config["entry"], index, candles, indicators) is True for index in range(len(candles)))
    exits = tuple(_condition(validated.config["exit"]["condition"], index, candles, indicators) is True for index in range(len(candles)))
    return EvaluationResult(
        dates=tuple(row["date"] for row in candles),
        entry_signals=entries,
        exit_signals=exits,
        indicators={key: tuple(value) for key, value in indicators.items()},
        strategy_hash=validated.config_hash,
    )
