"""Strict normalization and read-only NSE-versus-Shoonya candle comparison."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from stock_gplm.ingestion.daily import validate_daily

MAX_SAMPLE_ROWS = 100
PRICE_FIELDS = ("open", "high", "low", "close")
FIELD_ALIASES = {
    "symbol": ("symbol", "SYMBOL", "ticker"),
    "trading_date": ("trading_date", "date", "DATE", "timestamp"),
    "open": ("open", "OPEN", "open_price", "OPEN_PRICE"),
    "high": ("high", "HIGH", "high_price", "HIGH_PRICE"),
    "low": ("low", "LOW", "low_price", "LOW_PRICE"),
    "close": ("close", "CLOSE", "close_price", "CLOSE_PRICE"),
    "volume": ("volume", "VOLUME", "total_traded_quantity", "TOTTRDQTY"),
}


@dataclass(frozen=True)
class Observation:
    symbol: str
    trading_date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str
    source_timestamp: str | None = None
    adjustment: str = "UNADJUSTED"


def _strict_json(text: str) -> Any:
    def constant(value: str) -> None:
        raise ValueError(f"nonstandard JSON constant {value}")

    return json.loads(text, parse_constant=constant)


def tool_payload(result: Mapping[str, Any]) -> Any:
    """Extract one structured payload without retaining the raw MCP envelope."""
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise ValueError("tool response needs one text content item or structuredContent")
    item = content[0]
    if item.get("type") != "text" or not isinstance(item.get("text"), str):
        raise ValueError("tool response content is not JSON text")
    return _strict_json(item["text"])


def _record_list(value: Any, *, allow_empty: bool = False) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict):
        candidates = [v for v in value.values() if isinstance(v, list)]
        if len(candidates) != 1:
            raise ValueError("payload must contain exactly one record list")
        records = candidates[0]
    else:
        raise ValueError("payload is not an object or list")
    if not records and not allow_empty:
        raise ValueError("historical result is empty")
    if len(records) > MAX_SAMPLE_ROWS:
        raise ValueError(f"sample exceeds the {MAX_SAMPLE_ROWS}-row safety limit")
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("historical result contains a non-object record")
    return records


def _field(row: Mapping[str, Any], name: str) -> Any:
    present = [key for key in FIELD_ALIASES[name] if key in row]
    if len(present) != 1:
        raise ValueError(f"record needs exactly one recognized {name} field")
    return row[present[0]]


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} is not numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def normalize_nse_history(payload: Any, *, requested_symbol: str, source: str) -> list[Observation]:
    """Normalize a bounded MCP payload; unknown layouts are refused, not guessed."""
    observations: list[Observation] = []
    for row in _record_list(payload):
        symbol = str(_field(row, "symbol")).upper()
        if symbol != requested_symbol:
            raise ValueError(f"symbol mismatch: requested {requested_symbol}, received {symbol}")
        raw_date = str(_field(row, "trading_date"))[:10]
        session = date.fromisoformat(raw_date).isoformat()
        prices = {name: _decimal(_field(row, name), name) for name in PRICE_FIELDS}
        raw_volume = _field(row, "volume")
        if isinstance(raw_volume, bool) or Decimal(str(raw_volume)) != Decimal(str(raw_volume)).to_integral_value():
            raise ValueError("volume must be a whole number")
        volume = int(raw_volume)
        if volume < 0 or not prices["low"] <= min(prices["open"], prices["close"]) <= max(
                prices["open"], prices["close"]) <= prices["high"]:
            raise ValueError("record has invalid volume or OHLC bounds")
        observations.append(Observation(symbol, session, **prices, volume=volume, source=source,
                                        source_timestamp=str(row.get("source_timestamp")) if row.get(
                                            "source_timestamp") is not None else None,
                                        adjustment="UNADJUSTED"))
    return observations


def normalize_corporate_actions(payload: Any, *, requested_symbol: str) -> list[dict[str, Any]]:
    """Keep explicit action facts only; never derive an adjustment ratio from prose."""
    records = _record_list(payload, allow_empty=True)
    actions: list[dict[str, Any]] = []
    for row in records:
        symbol_key = next((key for key in ("symbol", "SYMBOL") if key in row), None)
        date_key = next((key for key in ("ex_date", "exDate", "EX_DATE", "date") if key in row), None)
        if symbol_key is None or date_key is None:
            raise ValueError("corporate-action record lacks symbol or ex-date")
        symbol = str(row[symbol_key]).upper()
        if symbol != requested_symbol:
            raise ValueError(f"corporate-action symbol mismatch: requested {requested_symbol}, received {symbol}")
        ex_date = date.fromisoformat(str(row[date_key])[:10]).isoformat()
        kind_key = next((key for key in ("action_type", "actionType", "type") if key in row), None)
        purpose_key = next((key for key in ("purpose", "PURPOSE", "description") if key in row), None)
        actions.append({"symbol": symbol, "ex_date": ex_date,
                        "action_type": str(row[kind_key]).upper() if kind_key else "UNKNOWN",
                        "purpose": str(row[purpose_key]) if purpose_key else None,
                        "adjustment_status": "UNKNOWN", "adjustment_factor": None,
                        "provenance": "NSE_MCP_EXPLICIT_FIELDS"})
    return actions


def load_shoonya_segment(path: Path, *, symbol: str, start: str, end: str,
                         holdout_start: str = "2026-04-29") -> list[Observation]:
    """Load one already-published segment only when its filename cannot include holdout rows."""
    parts = Path(path).name.removesuffix(".json.gz").split("_")
    if len(parts) != 2:
        raise ValueError("Shoonya segment filename must declare its date bounds")
    segment_start, segment_end = (date.fromisoformat(value) for value in parts)
    if segment_end >= date.fromisoformat(holdout_start):
        raise ValueError("Shoonya segment could contain locked-holdout data and will not be opened")
    if date.fromisoformat(start) < segment_start or date.fromisoformat(end) > segment_end:
        raise ValueError("comparison interval is outside the Shoonya segment bounds")
    report = validate_daily(Path(path))
    if report.disposition != "ACCEPT":
        raise ValueError(f"Shoonya segment is not accepted: {report.disposition}")
    expected = f"{symbol}-EQ"
    if report.symbol != expected:
        raise ValueError(f"Shoonya symbol mapping mismatch: expected {expected}, found {report.symbol}")
    return [Observation(symbol, candle.trading_date, candle.open, candle.high, candle.low, candle.close,
                        candle.volume, "SHOONYA:ShoonyaDailyV1", candle.source_timestamp_utc, "UNADJUSTED")
            for candle in report.content_quality.normalized_candles
            if start <= candle.trading_date <= end]


def compare_observations(nse: Iterable[Observation], shoonya: Iterable[Observation], *,
                         price_abs_tolerance: Decimal = Decimal("0.01"),
                         price_rel_tolerance: Decimal = Decimal("0.0001"),
                         volume_tolerance: int = 0,
                         corporate_actions: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    if price_abs_tolerance < 0 or price_rel_tolerance < 0 or volume_tolerance < 0:
        raise ValueError("comparison tolerances must be nonnegative")

    def indexed(rows: Iterable[Observation], label: str) -> tuple[dict[str, Observation], list[str]]:
        out: dict[str, Observation] = {}
        duplicates: list[str] = []
        for row in rows:
            if row.trading_date in out:
                duplicates.append(row.trading_date)
            else:
                out[row.trading_date] = row
        return out, sorted(set(duplicates))

    left, nse_duplicates = indexed(nse, "NSE")
    right, shoonya_duplicates = indexed(shoonya, "Shoonya")
    action_dates = {str(item["ex_date"]): str(item.get("action_type", "UNKNOWN"))
                    for item in corporate_actions if "ex_date" in item}
    compared: list[dict[str, Any]] = []
    mismatches = 0
    for day in sorted(left.keys() & right.keys()):
        nse_row, shoonya_row = left[day], right[day]
        differences: dict[str, Any] = {}
        row_match = True
        for field in PRICE_FIELDS:
            expected, observed = getattr(nse_row, field), getattr(shoonya_row, field)
            delta = observed - expected
            allowed = max(price_abs_tolerance, price_rel_tolerance * abs(expected))
            match = abs(delta) <= allowed
            differences[field] = {"nse": str(expected), "shoonya": str(observed),
                                  "difference": str(delta), "allowed": str(allowed), "match": match}
            row_match &= match
        volume_delta = shoonya_row.volume - nse_row.volume
        volume_match = abs(volume_delta) <= volume_tolerance
        differences["volume"] = {"nse": nse_row.volume, "shoonya": shoonya_row.volume,
                                 "difference": volume_delta, "allowed": volume_tolerance,
                                 "match": volume_match}
        row_match &= volume_match
        mismatches += int(not row_match)
        compared.append({"trading_date": day, "match": row_match, "differences": differences,
                         "corporate_action_flag": action_dates.get(day),
                         "price_basis": {"nse": nse_row.adjustment, "shoonya": shoonya_row.adjustment},
                         "interpretation": "OBSERVED_DIFFERENCE" if not row_match else "WITHIN_TOLERANCE"})
    return {
        "contract": "stock-gplm.m39-reference-comparison.v1",
        "counts": {"nse": len(left), "shoonya": len(right), "compared": len(compared),
                   "mismatched_dates": mismatches},
        "missing_from_nse": sorted(right.keys() - left.keys()),
        "missing_from_shoonya": sorted(left.keys() - right.keys()),
        "duplicates": {"nse": nse_duplicates, "shoonya": shoonya_duplicates},
        "provenance": {
            "nse_sources": sorted({row.source for row in left.values()}),
            "shoonya_sources": sorted({row.source for row in right.values()}),
            "nse_source_timestamps_present": sum(row.source_timestamp is not None for row in left.values()),
            "shoonya_source_timestamps_present": sum(row.source_timestamp is not None for row in right.values()),
        },
        "corporate_action_dates": action_dates,
        "adjustment_policy": "NO_ADJUSTMENTS_APPLIED; flags are observations, not explanations",
        "rows": compared,
    }
