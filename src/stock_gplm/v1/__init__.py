"""Configuration-driven, local-only V1 strategy research engine."""

from .backtest import BacktestResult, run_backtest
from .evaluator import EvaluationResult, evaluate_strategy
from .paper import PaperDecision, generate_paper_decision
from .registry import StrategyRegistry
from .schema import StrategyValidationError, validate_strategy

__all__ = [
    "BacktestResult",
    "EvaluationResult",
    "PaperDecision",
    "StrategyRegistry",
    "StrategyValidationError",
    "evaluate_strategy",
    "generate_paper_decision",
    "run_backtest",
    "validate_strategy",
]
