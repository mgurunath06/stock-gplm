"""Chronological walk-forward plans with horizon-specific purge and embargo.

No shuffling exists here. A sample may enter a partition only when its label is
known before the next partition's first as-of session plus the embargo. This
module defines evaluation boundaries; it trains nothing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .evidence import SessionAxis
from .labels import LabelContract, LabelRow

SPLIT_CONTRACT = "stock-gplm.m32-walk-forward.v1"
REQUIRED_EVALUATION = (
    "out_of_sample_benchmark_relative", "realistic_costs_slippage_liquidity",
    "calibrated_outcome_probabilities", "experiment_provenance",
    "point_in_time_universe",
)


class LeakageError(ValueError):
    """A sample's outcome would be known inside a later partition."""


@dataclass(frozen=True)
class WalkForwardPolicy:
    """All lengths are NSE sessions. These values require approval before any trial."""

    minimum_train_sessions: int
    validation_sessions: int
    test_sessions: int
    step_sessions: int
    embargo_sessions: int
    locked_holdout_sessions: int
    trial_budget: int
    expanding_train: bool = True

    def __post_init__(self) -> None:
        for name in (
            "minimum_train_sessions", "validation_sessions", "test_sessions",
            "step_sessions", "locked_holdout_sessions", "trial_budget",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.embargo_sessions) is not int or self.embargo_sessions < 0:
            raise ValueError("embargo_sessions must be a nonnegative integer")
        if self.step_sessions < self.test_sessions:
            raise ValueError("test partitions must not overlap across folds")


def default_policy(contract: LabelContract) -> WalkForwardPolicy:
    """Scale partitions to the horizon so long outcomes get long evaluation spans."""
    span = max(63, contract.max_offset)
    return WalkForwardPolicy(
        minimum_train_sessions=max(252, 3 * contract.max_offset),
        validation_sessions=span, test_sessions=span, step_sessions=span,
        embargo_sessions=5, locked_holdout_sessions=max(126, contract.max_offset),
        trial_budget=20,
    )


@dataclass(frozen=True)
class Partition:
    """As-of date bounds, inclusive."""

    first_as_of: str
    last_as_of: str

    def contains(self, day: str) -> bool:
        return self.first_as_of <= day <= self.last_as_of


@dataclass(frozen=True)
class Fold:
    index: int
    train: Partition
    validation: Partition
    test: Partition


@dataclass(frozen=True)
class WalkForwardPlan:
    contract: str
    horizon: str
    purge_sessions: int
    policy: WalkForwardPolicy
    session_source: str
    folds: tuple[Fold, ...]
    locked_holdout: Partition | None
    required_evaluation: tuple[str, ...] = REQUIRED_EVALUATION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_walk_forward(
    axis: SessionAxis, contract: LabelContract, policy: WalkForwardPolicy | None = None,
) -> WalkForwardPlan:
    """Lay folds on the axis. Too little history yields no folds, never shorter ones."""
    if contract.granularity != "DAILY":
        raise ValueError("walk-forward plans over a daily axis require a daily contract")
    policy = policy or default_policy(contract)
    purge = contract.max_offset
    separation = purge + policy.embargo_sessions + 1
    count = len(axis)
    sessions = axis.sessions
    holdout_start = count - policy.locked_holdout_sessions
    holdout = Partition(sessions[holdout_start], sessions[-1]) if holdout_start > 0 else None
    folds: list[Fold] = []
    # Train ends `separation` before validation begins, whose indexes are therefore
    # offset by the training minimum; each later fold moves forward by one step.
    validation_start = policy.minimum_train_sessions + separation - 1
    train_first = 0
    while holdout is not None:
        validation_end = validation_start + policy.validation_sessions - 1
        test_start = validation_end + separation
        test_end = test_start + policy.test_sessions - 1
        if test_end + separation > holdout_start:
            break
        train_last = validation_start - separation
        folds.append(Fold(
            len(folds),
            Partition(sessions[train_first], sessions[train_last]),
            Partition(sessions[validation_start], sessions[validation_end]),
            Partition(sessions[test_start], sessions[test_end]),
        ))
        validation_start += policy.step_sessions
        if not policy.expanding_train:
            train_first += policy.step_sessions
    return WalkForwardPlan(
        SPLIT_CONTRACT, contract.name, purge, policy, axis.source, tuple(folds), holdout,
    )


def verify_plan(plan: WalkForwardPlan, axis: SessionAxis) -> None:
    """Prove every boundary in session counts; independent of the fold builder."""
    separation = plan.purge_sessions + plan.policy.embargo_sessions + 1

    def require(left: str, right: str) -> None:
        if axis.position(right) - axis.position(left) < separation:
            raise LeakageError(f"partitions {left} and {right} violate purge and embargo")

    previous_test: Partition | None = None
    for fold in plan.folds:
        require(fold.train.last_as_of, fold.validation.first_as_of)
        require(fold.validation.last_as_of, fold.test.first_as_of)
        if plan.locked_holdout is None:
            raise LeakageError("folds without a locked holdout are not allowed")
        require(fold.test.last_as_of, plan.locked_holdout.first_as_of)
        if previous_test is not None and fold.test.first_as_of <= previous_test.last_as_of:
            raise LeakageError("test partitions overlap across folds")
        previous_test = fold.test


def select_samples(
    rows: Iterable[LabelRow], partition: Partition, *, next_partition_first: str | None,
    axis: SessionAxis, embargo_sessions: int,
) -> list[LabelRow]:
    """Keep complete labels in a partition; a resolved label past the boundary raises."""
    selected: list[LabelRow] = []
    for row in rows:
        if row.status != "COMPLETE" or not partition.contains(row.as_of_date):
            continue
        if row.label_available_date is None:
            raise LeakageError("complete label has no availability date")
        if next_partition_first is not None:
            distance = axis.position(next_partition_first) - axis.position(row.label_available_date)
            if distance <= embargo_sessions:
                raise LeakageError(
                    f"label for {row.as_of_date} resolves inside the boundary before {next_partition_first}"
                )
        selected.append(row)
    return selected


