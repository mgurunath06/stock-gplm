"""Point-in-time fusion of intraday bars with strictly prior daily features.

Two instants govern every row. A bar's values are usable no earlier than one minute
after its label, which is safe whether the provider labels a bar by start or end. A
daily value for date D is usable no earlier than 00:00 IST on D+1 because the daily
close and range are final only after the session; the provider's publication time
is unknown, so this is a lower bound, not evidence of actual availability. A session
therefore reads only daily rows dated before it, and nothing is bridged or filled.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Mapping, Sequence

from .features import ARITHMETIC, FEATURE_NAMES
from .minute import IST, MinuteBar, SegmentReport, SessionPolicy
from .readiness import _decimal
from .splits import LeakageError

FUSION_CONTRACT = "stock-gplm.m34-fusion.v1"
RETURN_PERIODS = (1, 5, 15)
VOLUME_WINDOW = 20
VOLATILITY_WINDOW = 20
MINUTE_FEATURES = (
    "minute_of_session", *(f"return_{item}m" for item in RETURN_PERIODS),
    "return_from_session_open", "session_range_pct", "session_close_location",
    "session_volume", "session_vwap_typical", "close_to_session_vwap",
    f"volume_ratio_{VOLUME_WINDOW}m", f"realized_volatility_{VOLATILITY_WINDOW}m",
    "gap_from_prior_daily_close", "close_to_prior_daily_close",
    "close_to_prior_daily_sma_20", "close_to_prior_daily_ema_20",
)
DAILY_COLUMNS = tuple(f"daily_{item}" for item in FEATURE_NAMES)
IDENTITY_COLUMNS = (
    "symbol", "session_date", "bar_label_ist", "bar_label_utc", "feature_available_utc", "bar_flags",
    "daily_status", "daily_as_of_date", "daily_available_utc", "daily_source_sha256",
)
FUSION_COLUMNS = (*IDENTITY_COLUMNS, *MINUTE_FEATURES, *DAILY_COLUMNS)


@dataclass(frozen=True)
class DailySnapshot:
    """One symbol's daily close and M3.2 features, knowable after ``as_of_date``."""

    as_of_date: str
    close: Decimal
    source_sha256: str
    features: Mapping[str, str | None]


def daily_available_at(as_of_date: str) -> datetime:
    return datetime.combine(date.fromisoformat(as_of_date) + timedelta(days=1), datetime.min.time(), IST)


def _prior_session(axis: Sequence[str], session_date: str) -> str | None:
    position = bisect_left(axis, session_date)
    return axis[position - 1] if position else None


def select_daily(
    daily: Mapping[str, DailySnapshot], axis: Sequence[str], session_date: str,
) -> tuple[str, DailySnapshot | None]:
    """Return the immediately prior axis session's row, or a status saying why not."""
    prior = _prior_session(axis, session_date)
    if prior is None:
        return "NO_PRIOR_SESSION", None
    snapshot = daily.get(prior)
    if snapshot is None:
        return "PRIOR_SESSION_CANDLE_MISSING", None
    return "PRIOR_SESSION", snapshot


def _text(value: Decimal | None) -> str | None:
    return _decimal(value) if value is not None else None


def fuse_session(
    report: SegmentReport, daily: Mapping[str, DailySnapshot], axis: Sequence[str], policy: SessionPolicy,
) -> list[dict[str, str | None]]:
    """Build one row per regular bar, each reading only bars at or before itself."""
    if report.decision in {"REJECT", "QUARANTINE"}:
        raise ValueError("a rejected or quarantined segment cannot contribute features")
    positions = {item: index for index, item in enumerate(policy.regular_minutes)}
    status, snapshot = select_daily(daily, axis, report.session_date)
    prior_features: Mapping[str, str | None] = snapshot.features if snapshot else {}
    regular = [item for item in report.bars if item.clock in positions]
    rows: list[dict[str, str | None]] = []
    with localcontext(ARITHMETIC):
        by_position: dict[int, MinuteBar] = {positions[item.clock]: item for item in regular}
        session_open = by_position[0].open if 0 in by_position else None
        high: Decimal | None = None
        low: Decimal | None = None
        total_volume = 0
        notional = Decimal(0)
        for bar in regular:
            position = positions[bar.clock]
            high = bar.high if high is None else max(high, bar.high)
            low = bar.low if low is None else min(low, bar.low)
            total_volume += bar.volume
            notional += (bar.high + bar.low + bar.close) / 3 * bar.volume
            complete = all(item in by_position for item in range(position + 1))
            values: dict[str, Decimal | None] = {}
            for period in RETURN_PERIODS:
                earlier = by_position.get(position - period)
                contiguous = all(position - step in by_position for step in range(period + 1))
                values[f"return_{period}m"] = bar.close / earlier.close - 1 if earlier and contiguous else None
            values["return_from_session_open"] = bar.close / session_open - 1 if session_open else None
            values["session_range_pct"] = (high - low) / bar.close if complete else None
            values["session_close_location"] = (
                (bar.close - low) / (high - low) if complete and high != low else None
            )
            vwap = notional / total_volume if complete and total_volume else None
            values["session_vwap_typical"] = vwap
            values["close_to_session_vwap"] = bar.close / vwap - 1 if vwap else None
            prior_volume = [by_position.get(position - step) for step in range(1, VOLUME_WINDOW + 1)]
            base = (
                Decimal(sum(item.volume for item in prior_volume if item)) / VOLUME_WINDOW
                if all(prior_volume) else None
            )
            values[f"volume_ratio_{VOLUME_WINDOW}m"] = Decimal(bar.volume) / base if base else None
            window = [by_position.get(position - step) for step in range(VOLATILITY_WINDOW + 1)]
            if all(window):
                ordered = [item.close for item in reversed(window) if item]
                moves = [right / left - 1 for left, right in zip(ordered, ordered[1:])]
                mean = sum(moves, Decimal(0)) / len(moves)
                variance = sum(((item - mean) ** 2 for item in moves), Decimal(0)) / (len(moves) - 1)
                values[f"realized_volatility_{VOLATILITY_WINDOW}m"] = variance.sqrt()
            else:
                values[f"realized_volatility_{VOLATILITY_WINDOW}m"] = None
            prior_close = snapshot.close if snapshot else None
            values["gap_from_prior_daily_close"] = session_open / prior_close - 1 if session_open and prior_close else None
            values["close_to_prior_daily_close"] = bar.close / prior_close - 1 if prior_close else None
            for name, column in (("sma_20", "close_to_prior_daily_sma_20"), ("ema_20", "close_to_prior_daily_ema_20")):
                reference = prior_features.get(name)
                values[column] = bar.close / Decimal(reference) - 1 if reference else None
            row: dict[str, str | None] = {
                "symbol": report.symbol, "session_date": report.session_date,
                "bar_label_ist": bar.start_ist.isoformat(),
                "bar_label_utc": bar.start_utc.isoformat().replace("+00:00", "Z"),
                "feature_available_utc": (bar.start_utc + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                "bar_flags": "|".join(bar.flags), "daily_status": status,
                "daily_as_of_date": snapshot.as_of_date if snapshot else None,
                "daily_available_utc": (
                    daily_available_at(snapshot.as_of_date).astimezone(bar.start_utc.tzinfo).isoformat().replace("+00:00", "Z")
                    if snapshot else None
                ),
                "daily_source_sha256": snapshot.source_sha256 if snapshot else None,
                "minute_of_session": str(position),
                "session_volume": str(total_volume) if complete else None,
            }
            for name, value in values.items():
                row[name] = _text(value)
            for name in FEATURE_NAMES:
                row[f"daily_{name}"] = prior_features.get(name)
            assert_causal(row)
            rows.append(row)
    return rows


def assert_causal(row: Mapping[str, str | None]) -> None:
    """Refuse a row whose daily input could not be known when the row is usable."""
    as_of = row["daily_as_of_date"]
    if as_of is None:
        if any(row[item] is not None for item in DAILY_COLUMNS):
            raise LeakageError("a row without a prior daily session carries daily values")
        return
    if as_of >= str(row["session_date"]):
        raise LeakageError(f"daily row {as_of} is not before session {row['session_date']}")
    usable = datetime.fromisoformat(str(row["feature_available_utc"]).replace("Z", "+00:00"))
    if daily_available_at(as_of) > usable:
        raise LeakageError(f"daily row {as_of} is not available at {usable.isoformat()}")


def causality_audit(
    report: SegmentReport, daily: Mapping[str, DailySnapshot], axis: Sequence[str], policy: SessionPolicy,
    *, prefix_step: int = 25,
) -> int:
    """Recompute from truncated inputs; a changed value proves a future dependency."""
    full = fuse_session(report, daily, axis, policy)
    past_axis = [item for item in axis if item < report.session_date]
    past_daily = {key: value for key, value in daily.items() if key < report.session_date}
    if fuse_session(report, past_daily, past_axis, policy) != full:
        raise LeakageError(f"{report.symbol} {report.session_date}: output depends on same-day or later daily data")
    checks = 1
    regular_count = len(full)
    regular_clocks = set(policy.regular_minutes)
    for cut in (*range(prefix_step, regular_count, prefix_step), regular_count):
        kept = [item for item in report.bars if item.clock in regular_clocks][:cut]
        partial = SegmentReport(report.symbol, report.session_date, report.decision, bars=tuple(kept))
        if fuse_session(partial, daily, axis, policy) != full[:cut]:
            raise LeakageError(f"{report.symbol} {report.session_date}: bar {cut} depends on a later bar")
        checks += 1
    return checks
