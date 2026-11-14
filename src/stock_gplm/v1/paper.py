"""Local, non-routable paper decisions produced by the generic evaluator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .evaluator import evaluate_strategy
from .registry import StrategyRegistry


@dataclass(frozen=True)
class PaperDecision:
    intent_id: int
    strategy_id: str
    strategy_version: int
    config_hash: str
    symbol: str
    side: str
    signal_date: str
    status: str = "SIMULATED_NOT_ROUTABLE"


def generate_paper_decision(
    registry: StrategyRegistry,
    strategy_id: str,
    version: int,
    *,
    symbol: str,
    rows: Sequence[Mapping[str, Any]],
    position_open: bool,
    quantity: float,
    data_quality_eligible: bool,
) -> PaperDecision | None:
    """Evaluate the latest completed session and record a local paper intent.

    This function has no broker dependency. A flat state responds only to the
    entry tree with BUY; an open state responds only to the exit tree with SELL.
    """
    if not data_quality_eligible:
        raise ValueError("data-quality eligibility gate failed")
    record = registry.get(strategy_id, version)
    if record.activation_status != "ACTIVE":
        raise ValueError("paper evaluation requires the exact active strategy version")
    evaluation = evaluate_strategy(record.config, rows)
    if not evaluation.dates:
        raise ValueError("paper evaluation requires at least one completed candle")
    signal = evaluation.exit_signals[-1] if position_open else evaluation.entry_signals[-1]
    if not signal:
        return None
    side = "SELL" if position_open else "BUY"
    signal_date = evaluation.dates[-1]
    intent_id = registry.create_paper_intent(
        strategy_id,
        version,
        symbol=symbol,
        side=side,
        quantity=quantity,
        signal_at=signal_date,
    )
    return PaperDecision(
        intent_id=intent_id,
        strategy_id=strategy_id,
        strategy_version=version,
        config_hash=record.config_hash,
        symbol=symbol,
        side=side,
        signal_date=signal_date,
    )
