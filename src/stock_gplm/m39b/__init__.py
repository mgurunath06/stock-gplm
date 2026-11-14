"""M3.9-B deterministic, synthetic-verified corporate-action adjustment engine.

This package has no collector, filesystem loader, training hook or network client.
Callers must supply already validated, licence-approved facts and immutable candles.
"""

from .engine import (
    AdjustmentRefused,
    ConflictingEvents,
    DuplicateEvents,
    LookaheadError,
    adjust_series,
    bonus_factor,
    detect_suspicious_discontinuities,
    dividend_total_return_factor,
    split_factor,
)
from .models import (
    AdjustmentMode,
    AdjustmentResult,
    CandleSeries,
    CorporateActionEvent,
    EventType,
    LicenceStatus,
    RawCandle,
    SeriesBasis,
    SourceEvidence,
    ValidationState,
)

__all__ = [
    "AdjustmentMode", "AdjustmentRefused", "AdjustmentResult", "CandleSeries",
    "ConflictingEvents", "CorporateActionEvent", "DuplicateEvents", "EventType",
    "LicenceStatus", "LookaheadError", "RawCandle", "SeriesBasis", "SourceEvidence",
    "ValidationState", "adjust_series", "bonus_factor",
    "detect_suspicious_discontinuities", "dividend_total_return_factor", "split_factor",
]
