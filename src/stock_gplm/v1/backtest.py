"""Deterministic, long-only research backtest using the generic evaluator."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .evaluator import evaluate_strategy, normalize_candles
from .schema import ValidatedStrategy, validate_strategy


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
    """Backtest one instrument; signals at close can enter only next session open."""
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
    for index, candle in enumerate(candles):
        if pending_signal is not None and open_trade is None:
            raw_open = candle["open"]
            open_trade = {
                "signal": pending_signal,
                "entry_index": index,
                "entry_price": raw_open * (1.0 + costs),
            }
            pending_signal = None
        if open_trade is not None:
            entry = float(open_trade["entry_price"])
            held = index - int(open_trade["entry_index"]) + 1
            stop_price = entry * (1.0 - float(stop["value"]) / 100.0)
            target_price = entry * (1.0 + float(target["value"]) / 100.0)
            exit_price: float | None = None
            reason = ""
            # When both intraday bounds are touched, the conservative stop-first rule applies.
            if candle["low"] <= stop_price:
                exit_price, reason = stop_price * (1.0 - costs), "STOP_LOSS"
            elif candle["high"] >= target_price:
                exit_price, reason = target_price * (1.0 - costs), "TAKE_PROFIT"
            elif evaluation.exit_signals[index]:
                exit_price, reason = candle["close"] * (1.0 - costs), "EXIT_CONDITION"
            elif held >= maximum:
                exit_price, reason = candle["close"] * (1.0 - costs), "MAX_HOLDING"
            if exit_price is not None:
                raw_entry = candles[int(open_trade["entry_index"])]["open"]
                gross_exit = exit_price / (1.0 - costs) if costs < 1 else exit_price
                trades.append(
                    Trade(
                        entry_signal_date=candles[int(open_trade["signal"])]["date"],
                        entry_date=candles[int(open_trade["entry_index"])]["date"],
                        entry_price=entry,
                        exit_date=candle["date"],
                        exit_price=exit_price,
                        exit_reason=reason,
                        holding_sessions=held,
                        gross_return=gross_exit / raw_entry - 1.0,
                        net_return=exit_price / entry - 1.0,
                    )
                )
                open_trade = None
        if open_trade is None and index + 1 < len(candles) and evaluation.entry_signals[index]:
            pending_signal = index
    if open_trade is not None:
        index = len(candles) - 1
        candle = candles[index]
        entry = float(open_trade["entry_price"])
        exit_price = candle["close"] * (1.0 - costs)
        raw_entry = candles[int(open_trade["entry_index"])]["open"]
        trades.append(
            Trade(
                entry_signal_date=candles[int(open_trade["signal"])]["date"],
                entry_date=candles[int(open_trade["entry_index"])]["date"],
                entry_price=entry,
                exit_date=candle["date"],
                exit_price=exit_price,
                exit_reason="END_OF_DATA",
                holding_sessions=index - int(open_trade["entry_index"]) + 1,
                gross_return=candle["close"] / raw_entry - 1.0,
                net_return=exit_price / entry - 1.0,
            )
        )
    total = 1.0
    for trade in trades:
        total *= 1.0 + trade.net_return * allocation
    hash_payload = {
        "strategy_id": validated.strategy_id,
        "strategy_version": strategy_version,
        "strategy_hash": validated.config_hash,
        "data_hash": _data_hash(candles),
        "execution_policy": "completed_close_signal_next_session_open_v1",
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
