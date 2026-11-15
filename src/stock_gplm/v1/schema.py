"""Strict schema and allowlist validation for declarative V1 strategies."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
MAX_TREE_DEPTH = 24
MAX_TREE_NODES = 2_000
MAX_LOOKBACK = 2_000
MAX_HOLDING_SESSIONS = 2_000

LOGICAL_OPERATORS = frozenset({"AND", "OR", "NOT"})
COMPARISON_OPERATORS = frozenset(
    {"GT", "GTE", "LT", "LTE", "EQ", "CROSS_ABOVE", "CROSS_BELOW"}
)
ARITHMETIC_OPERATORS = frozenset({"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"})
ROLLING_OPERATORS = frozenset({"HIGHEST", "LOWEST", "PERCENT_CHANGE"})
ALLOWED_OPERATORS = (
    LOGICAL_OPERATORS | COMPARISON_OPERATORS | ARITHMETIC_OPERATORS | ROLLING_OPERATORS
)
ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume"})
ALLOWED_INDICATORS = frozenset({
    "SMA", "SMA_VOLUME", "EMA", "HIGHEST", "LOWEST", "PERCENT_CHANGE",
    "RSI", "MACD", "ATR", "VOLATILITY",
})
# MACD is the only indicator with parameters beyond ``period`` (its slow EMA span).
MACD_OUTPUTS = frozenset({"line", "signal", "histogram"})
ALLOWED_TIMEFRAMES = frozenset({"1d"})
ALLOWED_INSTRUMENT_TYPES = frozenset({"EQ"})
ALLOWED_RISK_TYPES = frozenset({"percent"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class StrategyValidationError(ValueError):
    """A strategy is unsafe, unsupported, or structurally invalid."""


@dataclass(frozen=True)
class ValidatedStrategy:
    config: dict[str, Any]
    canonical_json: str
    config_hash: str
    dependencies: tuple[str, ...]

    @property
    def strategy_id(self) -> str:
        return str(self.config["strategy"]["id"])


def _fail(path: str, message: str) -> None:
    raise StrategyValidationError(f"{path}: {message}")


def _object(value: Any, path: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        _fail(path, f"missing fields {sorted(missing)}")
    if extra:
        _fail(path, f"unknown fields {sorted(extra)}")
    if any(not isinstance(key, str) for key in value):
        _fail(path, "all keys must be strings")
    return value


def _text(value: Any, path: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(path, f"must be a non-empty string of at most {maximum} characters")
    return value


def _number(value: Any, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a finite number")
    result = float(value)
    if result != result or abs(result) == float("inf"):
        _fail(path, "must be a finite number")
    if minimum is not None and result < minimum:
        _fail(path, f"must be at least {minimum}")
    return result


def _positive_int(value: Any, path: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        _fail(path, f"must be an integer from 1 through {maximum}")
    return value


def _offset(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")
    if value > 0:
        _fail(path, "future-session offsets are forbidden")
    if value < -MAX_LOOKBACK:
        _fail(path, f"offset exceeds {-MAX_LOOKBACK} session bound")
    return value


def _validate_indicator(raw: Any, path: str, seen: set[str]) -> dict[str, Any]:
    item = _object(raw, path, {"id", "type", "period"}, {"field", "fast", "signal", "output"})
    identifier = _text(item["id"], f"{path}.id", maximum=64)
    if not _IDENTIFIER.fullmatch(identifier):
        _fail(f"{path}.id", "must match [a-z][a-z0-9_-]{0,63}")
    if identifier in seen or identifier in ALLOWED_FIELDS:
        _fail(f"{path}.id", "must be unique and must not shadow a candle field")
    seen.add(identifier)
    kind = _text(item["type"], f"{path}.type", maximum=32).upper()
    if kind not in ALLOWED_INDICATORS:
        _fail(f"{path}.type", f"unsupported indicator {kind!r}")
    period = _positive_int(item["period"], f"{path}.period", maximum=MAX_LOOKBACK)
    default_field = "volume" if kind == "SMA_VOLUME" else "close"
    field = item.get("field", default_field)
    if field not in ALLOWED_FIELDS:
        _fail(f"{path}.field", f"must be one of {sorted(ALLOWED_FIELDS)}")
    if kind == "SMA_VOLUME" and field != "volume":
        _fail(f"{path}.field", "SMA_VOLUME requires volume")
    if kind == "ATR" and field != "close":
        _fail(f"{path}.field", "ATR is defined on high, low and close; field must be close")
    if kind in {"RSI", "VOLATILITY"} and period < 2:
        _fail(f"{path}.period", f"{kind} requires period >= 2")
    if kind != "MACD":
        extra = sorted({"fast", "signal", "output"} & set(item))
        if extra:
            _fail(path, f"{extra} are only valid for MACD")
        # Unchanged normalization keeps config hashes of pre-existing strategies stable.
        return {"id": identifier, "type": kind, "period": period, "field": field}
    fast = _positive_int(item.get("fast", 12), f"{path}.fast", maximum=MAX_LOOKBACK)
    signal = _positive_int(item.get("signal", 9), f"{path}.signal", maximum=MAX_LOOKBACK)
    if fast >= period:
        _fail(f"{path}.fast", "MACD fast span must be shorter than period (the slow span)")
    output = item.get("output", "line")
    if output not in MACD_OUTPUTS:
        _fail(f"{path}.output", f"must be one of {sorted(MACD_OUTPUTS)}")
    return {"id": identifier, "type": kind, "period": period, "field": field,
            "fast": fast, "signal": signal, "output": output}


def _validate_operand(
    raw: Any, path: str, indicators: set[str], counter: list[int], depth: int
) -> dict[str, Any]:
    counter[0] += 1
    if counter[0] > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
        _fail(path, "expression exceeds complexity limits")
    node = _object(raw, path, set(), {"field", "indicator", "constant", "operator", "operands", "lookback", "offset"})
    selectors = [key for key in ("field", "indicator", "constant", "operator") if key in node]
    if len(selectors) != 1:
        _fail(path, "must contain exactly one of field, indicator, constant, or operator")
    offset = _offset(node.get("offset", 0), f"{path}.offset")
    if "constant" in node:
        if set(node) - {"constant"}:
            _fail(path, "constant operands do not accept other fields")
        return {"constant": _number(node["constant"], f"{path}.constant")}
    if "field" in node:
        if set(node) - {"field", "offset"}:
            _fail(path, "field operands only accept offset")
        if node["field"] not in ALLOWED_FIELDS:
            _fail(f"{path}.field", f"must be one of {sorted(ALLOWED_FIELDS)}")
        return {"field": node["field"], "offset": offset}
    if "indicator" in node:
        if set(node) - {"indicator", "offset"}:
            _fail(path, "indicator operands only accept offset")
        if node["indicator"] not in indicators:
            _fail(f"{path}.indicator", "references an undefined indicator")
        return {"indicator": node["indicator"], "offset": offset}

    operator = str(node["operator"]).upper()
    if operator not in ARITHMETIC_OPERATORS | ROLLING_OPERATORS:
        _fail(f"{path}.operator", "must be an allowlisted numeric operator")
    operands = node.get("operands")
    if not isinstance(operands, list):
        _fail(f"{path}.operands", "must be an array")
    required_count = 2 if operator in ARITHMETIC_OPERATORS else 1
    if len(operands) != required_count:
        _fail(f"{path}.operands", f"{operator} requires {required_count} operands")
    result: dict[str, Any] = {
        "operator": operator,
        "operands": [
            _validate_operand(value, f"{path}.operands[{index}]", indicators, counter, depth + 1)
            for index, value in enumerate(operands)
        ],
    }
    if operator in ROLLING_OPERATORS:
        result["lookback"] = _positive_int(node.get("lookback"), f"{path}.lookback", maximum=MAX_LOOKBACK)
    elif "lookback" in node:
        _fail(f"{path}.lookback", "is only valid for rolling operators")
    if offset:
        result["offset"] = offset
    return result


def _validate_condition(
    raw: Any, path: str, indicators: set[str], counter: list[int], depth: int = 0
) -> dict[str, Any]:
    counter[0] += 1
    if counter[0] > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
        _fail(path, "condition tree exceeds complexity limits")
    node = _object(raw, path, {"operator"}, {"conditions", "left", "right"})
    operator = str(node["operator"]).upper()
    if operator not in LOGICAL_OPERATORS | COMPARISON_OPERATORS:
        _fail(f"{path}.operator", f"unsupported condition operator {operator!r}")
    if operator in LOGICAL_OPERATORS:
        if set(node) != {"operator", "conditions"} or not isinstance(node["conditions"], list):
            _fail(path, "logical nodes require only a conditions array")
        expected = 1 if operator == "NOT" else None
        if not node["conditions"] or (expected is not None and len(node["conditions"]) != expected):
            _fail(f"{path}.conditions", f"{operator} requires {'one' if expected else 'one or more'} condition(s)")
        return {
            "operator": operator,
            "conditions": [
                _validate_condition(value, f"{path}.conditions[{index}]", indicators, counter, depth + 1)
                for index, value in enumerate(node["conditions"])
            ],
        }
    if set(node) != {"operator", "left", "right"}:
        _fail(path, "comparison nodes require operator, left, and right")
    return {
        "operator": operator,
        "left": _validate_operand(node["left"], f"{path}.left", indicators, counter, depth + 1),
        "right": _validate_operand(node["right"], f"{path}.right", indicators, counter, depth + 1),
    }


def _validate_risk_rule(raw: Any, path: str) -> dict[str, Any]:
    rule = _object(raw, path, {"type", "value"})
    kind = str(rule["type"]).lower()
    if kind not in ALLOWED_RISK_TYPES:
        _fail(f"{path}.type", f"must be one of {sorted(ALLOWED_RISK_TYPES)}")
    value = _number(rule["value"], f"{path}.value", minimum=0.000001)
    if kind == "percent" and value > 100:
        _fail(f"{path}.value", "percent risk cannot exceed 100")
    return {"type": kind, "value": value}


def validate_strategy(raw: Mapping[str, Any]) -> ValidatedStrategy:
    """Return a canonical, hash-bound strategy or fail closed."""
    if not isinstance(raw, dict):
        raise StrategyValidationError("strategy document must be an object")
    try:
        encoded_size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError(f"strategy document is not JSON-compatible: {exc}") from exc
    if encoded_size > MAX_CONFIG_BYTES:
        raise StrategyValidationError(f"strategy document exceeds {MAX_CONFIG_BYTES} bytes")
    root = _object(
        raw,
        "$",
        {"schema_version", "strategy", "universe", "indicators", "entry", "exit", "risk", "position_sizing", "trading_costs"},
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != SCHEMA_VERSION:
        _fail("$.schema_version", f"must equal {SCHEMA_VERSION}")
    strategy = _object(root["strategy"], "$.strategy", {"id", "name", "description", "enabled", "timeframe"})
    strategy_id = _text(strategy["id"], "$.strategy.id", maximum=64)
    if not _IDENTIFIER.fullmatch(strategy_id):
        _fail("$.strategy.id", "must match [a-z][a-z0-9_-]{0,63}")
    if type(strategy["enabled"]) is not bool:
        _fail("$.strategy.enabled", "must be boolean")
    if strategy["timeframe"] not in ALLOWED_TIMEFRAMES:
        _fail("$.strategy.timeframe", f"must be one of {sorted(ALLOWED_TIMEFRAMES)}")
    canonical_strategy = {
        "id": strategy_id,
        "name": _text(strategy["name"], "$.strategy.name", maximum=120),
        "description": _text(strategy["description"], "$.strategy.description", maximum=2_000),
        "enabled": strategy["enabled"],
        "timeframe": strategy["timeframe"],
    }
    universe = _object(root["universe"], "$.universe", {"exchange", "instrument_type"}, {"symbols"})
    if universe["exchange"] != "NSE":
        _fail("$.universe.exchange", "only NSE is supported")
    if universe["instrument_type"] not in ALLOWED_INSTRUMENT_TYPES:
        _fail("$.universe.instrument_type", "only EQ is supported; ETFs remain excluded")
    symbols = universe.get("symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 10_000 or any(not isinstance(v, str) or not v for v in symbols):
        _fail("$.universe.symbols", "must be an array of at most 10000 non-empty strings")
    canonical_universe = {"exchange": "NSE", "instrument_type": "EQ", "symbols": sorted(set(symbols))}

    if not isinstance(root["indicators"], list) or len(root["indicators"]) > 256:
        _fail("$.indicators", "must be an array of at most 256 indicators")
    seen: set[str] = set()
    indicators = [_validate_indicator(value, f"$.indicators[{index}]", seen) for index, value in enumerate(root["indicators"])]
    counter = [0]
    entry = _validate_condition(root["entry"], "$.entry", seen, counter)
    exit_obj = _object(root["exit"], "$.exit", {"condition", "max_holding_sessions"})
    exit_condition = _validate_condition(exit_obj["condition"], "$.exit.condition", seen, counter)
    max_holding = _positive_int(exit_obj["max_holding_sessions"], "$.exit.max_holding_sessions", maximum=MAX_HOLDING_SESSIONS)

    risk = _object(root["risk"], "$.risk", {"stop_loss", "take_profit"})
    stop_loss = _validate_risk_rule(risk["stop_loss"], "$.risk.stop_loss")
    take_profit = _validate_risk_rule(risk["take_profit"], "$.risk.take_profit")
    sizing = _object(root["position_sizing"], "$.position_sizing", {"type", "value"})
    if sizing["type"] != "fixed_fraction":
        _fail("$.position_sizing.type", "V1 supports fixed_fraction only")
    sizing_value = _number(sizing["value"], "$.position_sizing.value", minimum=0.000001)
    if sizing_value > 1:
        _fail("$.position_sizing.value", "fixed_fraction cannot exceed 1")
    costs = _object(root["trading_costs"], "$.trading_costs", {"profile", "commission_bps", "slippage_bps"})
    commission = _number(costs["commission_bps"], "$.trading_costs.commission_bps", minimum=0)
    slippage = _number(costs["slippage_bps"], "$.trading_costs.slippage_bps", minimum=0)
    if commission + slippage > 1_000:
        _fail("$.trading_costs", "combined one-way costs cannot exceed 1000 bps")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "strategy": canonical_strategy,
        "universe": canonical_universe,
        "indicators": indicators,
        "entry": entry,
        "exit": {"condition": exit_condition, "max_holding_sessions": max_holding},
        "risk": {"stop_loss": stop_loss, "take_profit": take_profit},
        "position_sizing": {"type": sizing["type"], "value": sizing_value},
        "trading_costs": {
            "profile": _text(costs["profile"], "$.trading_costs.profile", maximum=64),
            "commission_bps": commission,
            "slippage_bps": slippage,
        },
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return ValidatedStrategy(
        config=normalized,
        canonical_json=canonical,
        config_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        dependencies=tuple(sorted(seen)),
    )
