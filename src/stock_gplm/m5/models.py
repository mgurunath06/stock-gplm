"""Typed eligibility contracts for manifest-verified historical NSE daily data."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

ADAPTER_VERSION = "stock-gplm.m5-historical-adapter.v1"
_SYMBOL = re.compile(r"^[A-Z0-9&._-]{1,48}-(?P<series>[A-Z0-9]{1,4})$")
_TOKEN = re.compile(r"^[0-9]{1,12}$")


class EligibilityMode(StrEnum):
    STRICT_RESEARCH = "STRICT_RESEARCH"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class FailureClass(StrEnum):
    INTEGRITY = "INTEGRITY"  # rejected in every mode; diagnostic mode is not a bypass
    EVIDENCE = "EVIDENCE"  # blocks STRICT_RESEARCH; recorded as absent in DIAGNOSTIC_ONLY


class DecisionStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DIAGNOSTIC_ALLOWED = "DIAGNOSTIC_ALLOWED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


FAILURE_CLASSES: dict[str, FailureClass] = {
    code: FailureClass.INTEGRITY for code in (
        "REQUEST_INVALID", "UNSUPPORTED_SERIES", "HOLDOUT_BOUNDARY_UNVERIFIED", "HOLDOUT_OVERLAP",
        "PUBLICATION_LOCK_ACTIVE", "SYMBOL_NOT_FOUND", "IDENTITY_MISMATCH", "ETF_EXCLUDED",
        "UNPUBLISHED_SEGMENT", "ORPHAN_MARKER", "UNEXPECTED_FILE", "SEGMENT_REJECTED",
        "SEGMENT_QUARANTINED", "METADATA_MISMATCH", "CONTRADICTORY_OBSERVATION", "NO_OBSERVATIONS",
        "CALENDAR_CONTRADICTION", "LISTING_CONTRADICTION", "QUARANTINED_SOURCE",
    )
} | {
    code: FailureClass.EVIDENCE for code in (
        "IDENTITY_EVIDENCE_MISSING", "PUBLICATION_COVERAGE_GAP", "CALENDAR_EVIDENCE_MISSING",
        "MISSING_SESSIONS", "PIT_MEMBERSHIP_MISSING", "ALIAS_EVIDENCE_MISSING",
        "ALIAS_STITCHING_UNSUPPORTED", "CORPORATE_ACTION_COVERAGE_MISSING",
        "CORPORATE_ACTION_EVENT_UNRESOLVED", "CORPORATE_ACTION_UNSUPPORTED",
        "PROVIDER_PRICE_BASIS_UNVERIFIED",
    )
}


def canonical_hash(value: Any) -> str:
    """SHA-256 of sorted, compact, finite JSON (the project-wide identity convention)."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EligibilityFailure:
    code: str
    message: str
    failure_class: FailureClass = field(init=False)

    def __post_init__(self) -> None:
        if self.code not in FAILURE_CLASSES:
            raise ValueError(f"unregistered eligibility failure code {self.code!r}")
        object.__setattr__(self, "failure_class", FAILURE_CLASSES[self.code])


@dataclass(frozen=True)
class EligibilityEvidence:
    """One evidence component and whether it was verified for this request."""

    component: str
    status: str  # VERIFIED, MISSING, NOT_APPLICABLE or HEURISTIC
    detail: str
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"VERIFIED", "MISSING", "NOT_APPLICABLE", "HEURISTIC"}:
            raise ValueError("unsupported evidence status")


@dataclass(frozen=True)
class DataEligibilityRequest:
    symbol: str
    start: str
    end: str
    mode: EligibilityMode
    expected_token: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", EligibilityMode(self.mode))
        if not isinstance(self.symbol, str) or _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol must look like NAME-SERIES, for example RELIANCE-EQ")
        start = date.fromisoformat(self.start)
        end = date.fromisoformat(self.end)
        if start > end:
            raise ValueError("request start is after end")
        if (end - start).days > 366 * 20:
            raise ValueError("request range exceeds 20 years")
        if self.expected_token is not None and _TOKEN.fullmatch(self.expected_token) is None:
            raise ValueError("expected_token must be a numeric exchange token")

    @property
    def series(self) -> str:
        match = _SYMBOL.fullmatch(self.symbol)
        assert match is not None
        return match.group("series")


@dataclass(frozen=True)
class CanonicalDailyBar:
    """One observed session; prices are exact decimal strings exactly as the provider published them."""

    date: str
    open: str
    high: str
    low: str
    close: str
    volume: int
    source_timestamp_utc: str
    segment_file: str
    session_timezone: str = "Asia/Kolkata"

    def evaluator_row(self) -> dict[str, Any]:
        return {"date": self.date, "open": float(self.open), "high": float(self.high), "low": float(self.low),
                "close": float(self.close), "volume": float(self.volume)}


@dataclass(frozen=True)
class DataEligibilityDecision:
    adapter_version: str
    request: DataEligibilityRequest
    status: DecisionStatus
    failures: tuple[EligibilityFailure, ...]
    evidence: tuple[EligibilityEvidence, ...]
    warnings: tuple[str, ...]
    segments: tuple[dict[str, Any], ...]
    holdout_cutoff: str | None
    bar_count: int
    first_bar: str | None
    last_bar: str | None
    bars_sha256: str | None
    decision_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DecisionStatus(self.status))
        if not self.decision_id:
            object.__setattr__(self, "decision_id", canonical_hash(self.binding()))
        self.verify()

    def verify(self) -> None:
        """Fail if any field was altered after the adapter issued this decision."""
        if self.decision_id != canonical_hash(self.binding()):
            raise ValueError("eligibility decision identifier does not match its content")
        has_integrity = any(f.failure_class == FailureClass.INTEGRITY for f in self.failures)
        has_evidence_gap = any(f.failure_class == FailureClass.EVIDENCE for f in self.failures)
        expected = (DecisionStatus.REJECTED if has_integrity else DecisionStatus.DIAGNOSTIC_ALLOWED
                    if self.request.mode == EligibilityMode.DIAGNOSTIC_ONLY else DecisionStatus.BLOCKED
                    if has_evidence_gap else DecisionStatus.ELIGIBLE)
        if self.status != expected:
            raise ValueError(f"decision status {self.status} contradicts its failures (expected {expected})")

    def binding(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("decision_id")
        return _jsonable(value)

    def to_dict(self) -> dict[str, Any]:
        return self.binding() | {
            "decision_id": self.decision_id,
            "investable": self.investable,
            "ranking_eligible": self.investable,
            "promotion_eligible": False,
            "paper_trading_eligible": False,
            "missing_evidence": [f.code for f in self.failures if f.failure_class == FailureClass.EVIDENCE],
        }

    @property
    def investable(self) -> bool:
        """True only for STRICT_RESEARCH decisions that passed every gate."""
        return self.status == DecisionStatus.ELIGIBLE and self.request.mode == EligibilityMode.STRICT_RESEARCH

    @property
    def usable(self) -> bool:
        return self.status in {DecisionStatus.ELIGIBLE, DecisionStatus.DIAGNOSTIC_ALLOWED}


@dataclass(frozen=True)
class VerifiedHistoricalDataset:
    """Bars released by the adapter together with the decision that released them."""

    decision: DataEligibilityDecision
    bars: tuple[CanonicalDailyBar, ...]
    basis: str
    share_events: tuple[dict[str, Any], ...] = ()
    adjustment_events: tuple[Any, ...] = ()  # validated stock_gplm.m39b CorporateActionEvent records

    def __post_init__(self) -> None:
        self.verify()

    def verify(self) -> None:
        """Re-check the release invariants; called again by every consumer."""
        self.decision.verify()
        if not self.decision.usable:
            raise ValueError("only ELIGIBLE or DIAGNOSTIC_ALLOWED decisions release data")
        if bars_hash(self.bars) != self.decision.bars_sha256 or len(self.bars) != self.decision.bar_count:
            raise ValueError("dataset bars differ from the bars its decision released")
        cutoff = self.decision.holdout_cutoff
        if cutoff is None or any(bar.date >= cutoff for bar in self.bars):
            raise ValueError("dataset contains locked-holdout observations")

    @property
    def fingerprint(self) -> str:
        return canonical_hash({"decision_id": self.decision.decision_id,
                               "bars": [asdict(bar) for bar in self.bars], "basis": self.basis,
                               "share_events": list(self.share_events)})


def bars_hash(bars: tuple[CanonicalDailyBar, ...] | list[CanonicalDailyBar]) -> str:
    return canonical_hash([asdict(bar) for bar in bars])


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
