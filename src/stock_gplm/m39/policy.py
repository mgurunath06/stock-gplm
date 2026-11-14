"""M3.9 licensing, sample-size and research-integrity policy gates."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping

ALLOWED_SYMBOLS = frozenset({"RELIANCE", "INFY", "TCS"})
LOCKED_HOLDOUT_START = date(2026, 4, 29)
# One server month ending on a 31-day month can include the same date in the
# prior month, so the inclusive declared interval needs at most 32 days.
MAX_CALENDAR_DAYS = 32
PROHIBITED_DESTINATION_PARTS = frozenset({
    "raw", "datasets", "dataset", "features", "feature-store", "training", "models", "backtests",
    "m3", "m35", "m37", "m38",
})


def validate_sample_request(symbol: str, start: str, end: str, *, allow_sample_fetch: bool) -> tuple[date, date]:
    if not allow_sample_fetch:
        raise PermissionError("sample fetch is disabled; pass --allow-sample-fetch after reviewing NSE terms")
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError(f"sample symbol must be one of {sorted(ALLOWED_SYMBOLS)}")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("sample start is after end")
    if last >= LOCKED_HOLDOUT_START:
        raise ValueError(f"sample end must be before locked holdout {LOCKED_HOLDOUT_START}")
    if (last - first).days + 1 > MAX_CALENDAR_DAYS:
        raise ValueError(f"sample span may not exceed {MAX_CALENDAR_DAYS} calendar days")
    return first, last


def validate_tool_arguments(arguments: Mapping[str, Any], *, symbol: str, start: str, end: str) -> None:
    """Ensure the operator-supplied MCP arguments cannot quietly widen the declared sample."""
    flattened = {str(value) for value in arguments.values() if isinstance(value, (str, int, float))}
    # get_stock_history exposes endDate + a bounded month count, not a start date.
    # The returned observations are checked against both declared bounds by the caller.
    if symbol not in flattened or end not in flattened:
        raise ValueError("tool arguments must explicitly contain the declared symbol and end")
    if "months" in arguments and (isinstance(arguments["months"], bool) or arguments["months"] != 1):
        raise ValueError("historical sample calls must request exactly one month")
    for value in flattened:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            continue
        if day < date.fromisoformat(start) or day > date.fromisoformat(end):
            raise ValueError("tool arguments contain a date outside the declared sample interval")


def assert_reference_only_destination(path: Path) -> None:
    # resolve() follows any existing parent symlink, preventing a benign-looking
    # report path from redirecting writes into a protected data tree.
    parts = {part.lower() for part in Path(path).resolve(strict=False).parts}
    overlap = parts & PROHIBITED_DESTINATION_PARTS
    if overlap:
        raise ValueError(f"NSE MCP output cannot enter research/training data paths: {sorted(overlap)}")
