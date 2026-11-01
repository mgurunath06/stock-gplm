"""Verified adapter for the observed Shoonya serialized daily-history schema."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_NAME = "ShoonyaDailyV1"
TIMESTAMP_POLICY = "ssboe_posix_seconds_utc_date_equals_trading_date"
FIELDS = frozenset({"time", "into", "inth", "intl", "intc", "intv", "ssboe"})

_DATE = re.compile(r"^(?P<day>\d{2})-(?P<month>[A-Z]{3})-(?P<year>\d{4})$")
_FIXED_POINT = re.compile(r"^\d+(?:\.\d+)?$")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


class UnsupportedShoonyaDailySchema(ValueError):
    """The record is not structurally ShoonyaDailyV1."""


@dataclass(frozen=True)
class Candle:
    trading_date: str
    source_timestamp_utc: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def decode_structure(value: Any) -> dict[str, str]:
    """Decode one serialized row and require the exact V1 string-field shape."""
    if not isinstance(value, str):
        raise UnsupportedShoonyaDailySchema("V1 record must be a serialized JSON string")
    try:
        decoded = json.loads(
            value,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise UnsupportedShoonyaDailySchema(f"invalid serialized JSON object: {exc}") from exc
    if not isinstance(decoded, dict):
        raise UnsupportedShoonyaDailySchema("serialized V1 record must decode to an object")
    actual = frozenset(decoded)
    if actual != FIELDS:
        missing = sorted(FIELDS - actual)
        extra = sorted(actual - FIELDS)
        raise UnsupportedShoonyaDailySchema(
            f"V1 field mismatch; missing={missing}, extra={extra}"
        )
    if any(not isinstance(value, str) for value in decoded.values()):
        raise UnsupportedShoonyaDailySchema("all V1 field values must be strings")
    return decoded


def _trading_date(value: str) -> date:
    match = _DATE.fullmatch(value)
    if match is None or match.group("month") not in _MONTHS:
        raise ValueError("time must match uppercase DD-MON-YYYY")
    try:
        return date(
            int(match.group("year")),
            _MONTHS[match.group("month")],
            int(match.group("day")),
        )
    except ValueError as exc:
        raise ValueError("time is not a valid calendar date") from exc


def _decimal(value: str, field: str, *, positive: bool) -> Decimal:
    if _FIXED_POINT.fullmatch(value) is None:
        raise ValueError(f"{field} must be an unsigned fixed-point decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a valid decimal") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field} must be finite and {qualifier}")
    return number


def parse_decoded(row: dict[str, str]) -> Candle:
    """Validate values and normalize a structurally valid V1 record."""
    trading_date = _trading_date(row["time"])
    if not row["ssboe"].isdigit():
        raise ValueError("ssboe must be an unsigned integer string")
    try:
        source_timestamp = datetime.fromtimestamp(int(row["ssboe"]), timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("ssboe is outside the supported POSIX timestamp range") from exc
    if source_timestamp.date() != trading_date:
        raise ValueError("ssboe UTC date disagrees with time trading date")

    opening = _decimal(row["into"], "into", positive=True)
    high = _decimal(row["inth"], "inth", positive=True)
    low = _decimal(row["intl"], "intl", positive=True)
    close = _decimal(row["intc"], "intc", positive=True)
    volume_decimal = _decimal(row["intv"], "intv", positive=False)
    if volume_decimal != volume_decimal.to_integral_value():
        raise ValueError("intv must represent a whole number of units")
    if not low <= min(opening, close) <= max(opening, close) <= high:
        raise ValueError("OHLC bounds are inconsistent")

    return Candle(
        trading_date=trading_date.isoformat(),
        source_timestamp_utc=source_timestamp.isoformat().replace("+00:00", "Z"),
        open=opening,
        high=high,
        low=low,
        close=close,
        volume=int(volume_decimal),
    )


class ShoonyaDailyV1:
    """Namespace for the versioned V1 schema contract."""

    schema_name = SCHEMA_NAME
    timestamp_policy = TIMESTAMP_POLICY
    fields = FIELDS
    decode_structure = staticmethod(decode_structure)
    parse_decoded = staticmethod(parse_decoded)
