"""Deterministic, long-only single-instrument backtest using the generic evaluator.

Candles passed here are caller-supplied, so every result is classified
``CALLER_SUPPLIED_UNVERIFIED``. Historical research runs must go through
``stock_gplm.m5``, which obtains eligibility from manifest-verified inputs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .evaluator import evaluate_strategy, normalize_candles
from .execution import EXECUTION_POLICY, resting_exit
from .schema import ValidatedStrategy, validate_strategy

DATA_CLASSIFICATION = "CALLER_SUPPLIED_UNVERIFIED"


@dataclass(frozen=True)
class Trade:
    entry_signal_date: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    holding_sessions: int
    gross_return: float
    net_return: float


@dataclass(frozen=True)
class BacktestResult:
    result_hash: str
    strategy_id: str
    strategy_version: int | None
    strategy_hash: str
    data_hash: str
    execution_policy: str
    trades: tuple[Trade, ...]
    total_net_return: float
    data_classification: str = DATA_CLASSIFICATION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _data_hash(candles: tuple[dict[str, Any], ...]) -> str:
    payload = json.dumps(candles, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_backtest(
    strategy: Mapping[str, Any] | ValidatedStrategy,
    rows: Sequence[Mapping[str, Any]],
    *,
    strategy_version: int | None = None,
    data_quality_eligible: bool,
) -> BacktestResult:
    """Backtest one instrument; close decisions execute only at the next session open.

    ``data_quality_eligible`` is the caller's acknowledgement for synthetic
    engineering data. It is not provenance evidence and cannot make a result
    eligible for research ranking.
    """
    if not data_quality_eligible:
        raise ValueError("data-quality eligibility gate failed")
    validated = strategy if isinstance(strategy, ValidatedStrategy) else validate_strategy(strategy)
    candles = normalize_candles(rows)
    evaluation = evaluate_strategy(validated, candles)
    config = validated.config
    costs = (config["trading_costs"]["commission_bps"] + config["trading_costs"]["slippage_bps"]) / 10_000.0
    maximum = config["exit"]["max_holding_sessions"]
    stop = config["risk"]["stop_loss"]
    target = config["risk"]["take_profit"]
    allocation = float(config["position_sizing"]["value"])
    trades: list[Trade] = []
    pending_signal: int | None = None
    open_trade: dict[str, Any] | None = None
    pending_exit: str | None = None

    def close_trade(index: int, raw_exit: float, reason: str) -> None:
        assert open_trade is not None
        entry = float(open_trade["entry_price"])
        raw_entry = candles[int(open_trade["entry_index"])]["open"]
        exit_price = raw_exit * (1.0 - costs)
        trades.append(
            Trade(
                entry_signal_date=candles[int(open_trade["signal"])]["date"],
                entry_date=candles[int(open_trade["entry_index"])]["date"],
                entry_price=entry,
                exit_date=candles[index]["date"],
                exit_price=exit_price,
                exit_reason=reason,
                holding_sessions=index - int(open_trade["entry_index"]) + 1,
                gross_return=raw_exit / raw_entry - 1.0,
                net_return=exit_price / entry - 1.0,
            )
        )

    for index, candle in enumerate(candles):
        # Session open: orders decided at the previous completed close execute here.
        if pending_exit is not None and open_trade is not None:
            close_trade(index, candle["open"], pending_exit)
            open_trade, pending_exit = None, None
        if pending_signal is not None and open_trade is None:
            open_trade = {
                "signal": pending_signal,
                "entry_index": index,
                "entry_price": candle["open"] * (1.0 + costs),
                # Levels reference the observed market fill, not the cost-inflated price.
                "stop_price": candle["open"] * (1.0 - float(stop["value"]) / 100.0),
                "target_price": candle["open"] * (1.0 + float(target["value"]) / 100.0),
            }
            pending_signal = None
        # Intrabar: resting stop and target orders.
        if open_trade is not None:
            fill = resting_exit(candle, float(open_trade["stop_price"]), float(open_trade["target_price"]))
            if fill is not None:
                close_trade(index, fill[0], fill[1])
                open_trade = None
        # Session close: decisions for the next open; the close itself is never a fill.
        if open_trade is not None:
            held = index - int(open_trade["entry_index"]) + 1
            if evaluation.exit_signals[index]:
                pending_exit = "EXIT_CONDITION"
            elif held >= maximum:
                pending_exit = "MAX_HOLDING"
        elif index + 1 < len(candles) and evaluation.entry_signals[index]:
            pending_signal = index
    if open_trade is not None:
        # Not a fill: the position is marked at the final completed close.
        close_trade(len(candles) - 1, candles[-1]["close"], "END_OF_DATA_MARK")
    total = 1.0
    for trade in trades:
        total *= 1.0 + trade.net_return * allocation
    hash_payload = {
        "strategy_id": validated.strategy_id,
        "strategy_version": strategy_version,
        "strategy_hash": validated.config_hash,
        "data_hash": _data_hash(candles),
        "execution_policy": EXECUTION_POLICY,
        "data_classification": DATA_CLASSIFICATION,
        "trades": [asdict(value) for value in trades],
        "total_net_return": total - 1.0,
    }
    result_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return BacktestResult(
        result_hash=result_hash,
        strategy_id=validated.strategy_id,
        strategy_version=strategy_version,
        strategy_hash=validated.config_hash,
        data_hash=hash_payload["data_hash"],
        execution_policy=hash_payload["execution_policy"],
        trades=tuple(trades),
        total_net_return=total - 1.0,
    )
