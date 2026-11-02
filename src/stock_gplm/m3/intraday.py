"""Intraday label contract. It accepts real minute bars only.

M3.4 adds the ``ShoonyaMinuteV1`` ingestion adapter, but its coverage and session
gates are blocked, so no schema is confirmed for labels and production calls refuse
every series. Minute bars are never derived from daily candles.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Mapping, Sequence

from .features import ARITHMETIC
from .labels import IntradayDataRequired
from .readiness import _decimal

CONFIRMED_MINUTE_SCHEMAS: frozenset[str] = frozenset()
SYNTHETIC_FIXTURE_SCHEMA = "SYNTHETIC_TEST_FIXTURE"
INTRADAY_OFFSETS_MINUTES = (15, 30, 60)
MINIMUM_BARS_PER_SESSION = 2


@dataclass(frozen=True)
class MinuteBar:
    timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if self.timestamp_utc.utcoffset() != timedelta(0):
            raise ValueError("minute bar timestamp must be UTC")
        if self.timestamp_utc.second or self.timestamp_utc.microsecond:
            raise ValueError("minute bar timestamp must fall on a whole minute")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("minute bar OHLC bounds are inconsistent")
        if self.low <= 0 or self.volume < 0:
            raise ValueError("minute bar prices must be positive and volume nonnegative")


@dataclass(frozen=True)
class SessionBounds:
    """Supplied first and last bar times; session hours are never assumed."""

    first_bar_utc: datetime
    last_bar_utc: datetime
    provenance: str

    def __post_init__(self) -> None:
        if self.first_bar_utc >= self.last_bar_utc:
            raise ValueError("session bounds must be ascending")
        if not self.provenance.strip():
            raise ValueError("session bounds require provenance")


@dataclass(frozen=True)
class MinuteSeries:
    symbol: str
    schema: str
    bars: tuple[MinuteBar, ...]

    def __post_init__(self) -> None:
        stamps = [item.timestamp_utc for item in self.bars]
        if not stamps:
            raise IntradayDataRequired("minute series is empty")
        if any(left >= right for left, right in zip(stamps, stamps[1:])):
            raise ValueError("minute bars must be strictly ascending")
        counts: dict[str, int] = defaultdict(int)
        for stamp in stamps:
            counts[stamp.date().isoformat()] += 1
        if min(counts.values()) < MINIMUM_BARS_PER_SESSION:
            raise IntradayDataRequired(
                "a session has too few bars to be minute data; daily bars are not accepted"
            )


@dataclass(frozen=True)
class IntradayLabelRow:
    symbol: str
    as_of_utc: str
    evidence: str
    status: str
    entry_utc: str | None
    label_available_utc: str | None
    gross_returns: dict[str, str | None]
    gross_return_to_session_close: str | None


def build_intraday_labels(
    series: MinuteSeries, bounds: Mapping[str, SessionBounds], *, synthetic_fixture: bool = False,
) -> list[IntradayLabelRow]:
    """Build same-session outcomes; a path with a missing minute has no value."""
    if not isinstance(series, MinuteSeries):
        raise IntradayDataRequired("intraday labels require a validated MinuteSeries")
    if synthetic_fixture:
        if series.schema != SYNTHETIC_FIXTURE_SCHEMA:
            raise IntradayDataRequired("only a labeled test fixture may use the synthetic path")
        evidence = "SYNTHETIC_TEST_FIXTURE"
    elif series.schema not in CONFIRMED_MINUTE_SCHEMAS:
        raise IntradayDataRequired(f"minute schema is not confirmed: {series.schema}")
    else:
        evidence = series.schema
    by_time = {item.timestamp_utc: item for item in series.bars}
    minute = timedelta(minutes=1)
    result: list[IntradayLabelRow] = []
    with localcontext(ARITHMETIC):
        for bar in series.bars:
            stamp = bar.timestamp_utc
            session = bounds.get(stamp.date().isoformat())
            returns: dict[str, str | None] = {f"{item}m": None for item in INTRADAY_OFFSETS_MINUTES}
            to_close: str | None = None
            entry_time = stamp + minute
            if session is None or not session.first_bar_utc <= stamp <= session.last_bar_utc:
                status = "SESSION_BOUNDS_UNKNOWN"
                entry_time = None
            elif stamp == session.last_bar_utc:
                status = "NO_ENTRY_IN_SESSION"
                entry_time = None
            elif entry_time not in by_time:
                status = "MISSING_ENTRY_BAR"
            else:
                status = "COMPLETE"
                entry = by_time[entry_time].open
                for offset in INTRADAY_OFFSETS_MINUTES:
                    exit_time = stamp + offset * minute
                    if exit_time > session.last_bar_utc:
                        continue
                    path = [stamp + step * minute for step in range(1, offset + 1)]
                    if all(item in by_time for item in path):
                        returns[f"{offset}m"] = _decimal(by_time[exit_time].close / entry - 1)
                    else:
                        status = "MISSING_MINUTE_IN_HORIZON"
                steps = int((session.last_bar_utc - stamp) / minute)
                if all(stamp + step * minute in by_time for step in range(1, steps + 1)):
                    to_close = _decimal(by_time[session.last_bar_utc].close / entry - 1)
                else:
                    status = "MISSING_MINUTE_IN_HORIZON"
            available = None
            if status == "COMPLETE" and session is not None:
                available = session.last_bar_utc.astimezone(timezone.utc).isoformat()
            result.append(IntradayLabelRow(
                series.symbol, stamp.isoformat(), evidence, status,
                entry_time.isoformat() if entry_time is not None else None,
                available, returns, to_close,
            ))
    return result
