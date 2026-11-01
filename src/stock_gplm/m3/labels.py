"""Versioned forward-outcome contracts, kept strictly apart from features."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Literal, Sequence

from .evidence import ReferenceEvidence, SessionAxis
from .features import ARITHMETIC, _rolling_extreme
from .readiness import DatasetRow, _decimal

LABEL_CONTRACT = "stock-gplm.m32-labels.v1"
AS_OF_POLICY = "AFTER_SESSION_CLOSE_ON_AS_OF_DATE"
COST_POLICY = "GROSS_NOT_COST_ADJUSTED"
INTRADAY = "intraday"
LabelStatus = Literal[
    "COMPLETE", "CENSORED", "MISSING_CANDLE_IN_HORIZON",
    "CORPORATE_ACTION_IN_HORIZON", "DELISTED_IN_HORIZON",
]


class IntradayDataRequired(ValueError):
    """An intraday outcome was requested without real minute-level data."""


@dataclass(frozen=True)
class LabelContract:
    """One horizon: as-of, entry, exit and availability rules in explicit units."""

    name: str
    granularity: Literal["DAILY", "MINUTE"]
    unit: Literal["NSE_SESSION", "MINUTE"]
    min_offset: int
    max_offset: int
    as_of_policy: str
    entry_policy: str
    exit_policy: str
    available_policy: str
    cost_policy: str = COST_POLICY

    def __post_init__(self) -> None:
        if not 1 <= self.min_offset <= self.max_offset:
            raise ValueError("label offsets must satisfy 1 <= min <= max")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _daily(name: str, first: int, last: int) -> LabelContract:
    return LabelContract(
        name=name, granularity="DAILY", unit="NSE_SESSION", min_offset=first, max_offset=last,
        as_of_policy=AS_OF_POLICY,
        entry_policy="OPEN_OF_SESSION_AS_OF_PLUS_1",
        exit_policy="CLOSE_OF_SESSION_AS_OF_PLUS_OFFSET",
        available_policy="AFTER_SESSION_CLOSE_ON_AS_OF_PLUS_MAX_OFFSET",
    )


# Horizon buckets do not overlap. One month is taken as 21 sessions.
DAILY_LABEL_CONTRACTS = (
    _daily("1-7d", 1, 7),
    _daily("7-15d", 8, 15),
    _daily("15-30d", 16, 30),
    _daily("1-3m", 31, 63),
    _daily("3-6m", 64, 126),
    _daily("6-12m", 127, 252),
)
INTRADAY_LABEL_CONTRACT = LabelContract(
    name=INTRADAY, granularity="MINUTE", unit="MINUTE", min_offset=1, max_offset=375,
    as_of_policy="AFTER_CLOSE_OF_MINUTE_BAR",
    entry_policy="OPEN_OF_NEXT_MINUTE_BAR",
    exit_policy="CLOSE_OF_BAR_AT_OFFSET_WITHIN_THE_SAME_SESSION",
    available_policy="AFTER_CLOSE_OF_EXIT_BAR",
)
LABEL_CONTRACTS = (INTRADAY_LABEL_CONTRACT, *DAILY_LABEL_CONTRACTS)
CONTRACTS_BY_NAME = {item.name: item for item in LABEL_CONTRACTS}


@dataclass(frozen=True)
class LabelRow:
    """One outcome. Values are None unless status is COMPLETE; never a fill value."""

    symbol: str
    as_of_date: str
    horizon: str
    status: str
    entry_date: str | None
    exit_date: str | None
    label_available_date: str | None
    gross_return_at_min_offset: str | None
    gross_return_at_max_offset: str | None
    max_favorable_excursion: str | None
    max_adverse_excursion: str | None
    corporate_action_coverage: str
    membership_at_as_of: str


def build_daily_labels(
    rows: Sequence[DatasetRow], axis: SessionAxis, contract: LabelContract,
    evidence: ReferenceEvidence | None = None,
) -> list[LabelRow]:
    """Build labels for one symbol, counting offsets on the session axis, not on rows."""
    if contract.granularity != "DAILY":
        raise IntradayDataRequired(
            "intraday labels require real minute bars and are never derived from daily candles"
        )
    if not rows:
        return []
    if len({item.symbol for item in rows}) > 1:
        raise ValueError("label input must contain one symbol")
    evidence = evidence or ReferenceEvidence()
    symbol = rows[0].symbol
    actions = evidence.discontinuities(symbol)
    delisted = evidence.delisting_date(symbol)
    first_position = axis.position(rows[0].trading_date)
    last_position = axis.position(rows[-1].trading_date)
    by_position: dict[int, DatasetRow] = {}
    for row in rows:
        position = axis.position(row.trading_date)
        if position in by_position:
            raise ValueError("label input repeats a session")
        by_position[position] = row
    span = last_position - first_position + 1
    window = contract.max_offset

    # Absent candles are sentinels that only an already-rejected window could read.
    dense_high = [
        Decimal(by_position[first_position + i].high) if first_position + i in by_position
        else Decimal("-Infinity") for i in range(span)
    ]
    dense_low = [
        Decimal(by_position[first_position + i].low) if first_position + i in by_position
        else Decimal("Infinity") for i in range(span)
    ]
    present = [0] * (span + 1)
    for i in range(span):
        present[i + 1] = present[i] + (first_position + i in by_position)

    result: list[LabelRow] = []
    with localcontext(ARITHMETIC):
        highest = _rolling_extreme(dense_high, window, largest=True)
        lowest = _rolling_extreme(dense_low, window, largest=False)
        for position, row in sorted(by_position.items()):
            entry_position = position + 1
            exit_position = position + contract.max_offset
            min_position = position + contract.min_offset
            known = exit_position < len(axis)
            entry_date = axis.sessions[entry_position] if entry_position < len(axis) else None
            exit_date = axis.sessions[exit_position] if known else None
            status: LabelStatus = "COMPLETE"
            if delisted is not None and row.trading_date < delisted and (
                exit_date is None or delisted <= exit_date
            ):
                status = "DELISTED_IN_HORIZON"
            elif not known:
                status = "CENSORED"
            elif exit_position > last_position or present[exit_position - first_position + 1] - present[entry_position - first_position] != window:
                status = "MISSING_CANDLE_IN_HORIZON"
            elif bisect_right(actions, exit_date) > bisect_right(actions, entry_date):
                # An action on the entry date is already in that session's open.
                status = "CORPORATE_ACTION_IN_HORIZON"
            values: tuple[str | None, str | None, str | None, str | None] = (None, None, None, None)
            if status == "COMPLETE":
                entry = Decimal(by_position[entry_position].open)
                index = exit_position - first_position
                values = (
                    _decimal(Decimal(by_position[min_position].close) / entry - 1),
                    _decimal(Decimal(by_position[exit_position].close) / entry - 1),
                    _decimal(highest[index] / entry - 1),
                    _decimal(lowest[index] / entry - 1),
                )
            result.append(LabelRow(
                symbol, row.trading_date, contract.name, status, entry_date, exit_date,
                exit_date if status == "COMPLETE" else None, *values,
                evidence.corporate_action_coverage(symbol, row.trading_date, exit_date or row.trading_date),
                evidence.membership(symbol, row.trading_date),
            ))
    return result
