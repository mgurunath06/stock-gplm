"""Typed eligibility contracts for manifest-verified historical NSE daily data."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from .issuance import verify_seal

ADAPTER_VERSION = "stock-gplm.m5-historical-adapter.v2"
UNIVERSE_CONTRACT = "stock-gplm.m51-requested-universe.v1"
MAX_UNIVERSE = 200
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
        # M5.1 corporate-action reconciliation: conflicting or ambiguous evidence is never matched loosely.
        "CORPORATE_ACTION_CONTRADICTION", "CORPORATE_ACTION_AMBIGUOUS", "CORPORATE_ACTION_FIELDS_MISSING",
        "CORPORATE_ACTION_DETAILS_UNSUPPORTED", "CORPORATE_ACTION_EVENT_UNMATCHED",
    )
} | {
    code: FailureClass.EVIDENCE for code in (
        "IDENTITY_EVIDENCE_MISSING", "PUBLICATION_COVERAGE_GAP", "CALENDAR_EVIDENCE_MISSING",
        "MISSING_SESSIONS", "PIT_MEMBERSHIP_MISSING", "ALIAS_EVIDENCE_MISSING",
        "ALIAS_STITCHING_UNSUPPORTED", "CORPORATE_ACTION_COVERAGE_MISSING",
        "CORPORATE_ACTION_EVENT_UNRESOLVED", "CORPORATE_ACTION_UNSUPPORTED",
        "PROVIDER_PRICE_BASIS_UNVERIFIED",
        # M5.1: evidence or events that did not come through the file-backed evidence pipeline.
        "EVIDENCE_NOT_ISSUED", "CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED", "CORPORATE_ACTION_STATUS_UNRESOLVED",
        # M5.1.1: strict authority gates (see adapter.strict_authority_failures).
        "EVIDENCE_SYNTHETIC", "EVIDENCE_AUTHORITY_UNAPPROVED", "INSTRUMENT_CLASSIFICATION_UNVERIFIED",
        "COST_SCHEDULE_UNREGISTERED",
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
    # M5.1 bindings: the evidence, price basis and corporate-action treatment the bars were released under.
    evidence_sha256: str | None = None
    evidence_sources: dict[str, str] | None = None
    price_basis: str | None = None
    share_events: tuple[dict[str, Any], ...] = ()
    corporate_action_reconciliation: tuple[dict[str, Any], ...] = ()
    issuance_seal: str = field(default="", compare=False, repr=False)

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
        value.pop("issuance_seal")
        return _jsonable(value)

    def to_dict(self) -> dict[str, Any]:
        return self.binding() | {
            "decision_id": self.decision_id,
            "adapter_issued": self.adapter_issued,
            "investable": self.investable,
            "ranking_eligible": self.investable,
            "promotion_eligible": False,
            "paper_trading_eligible": False,
            "missing_evidence": [f.code for f in self.failures if f.failure_class == FailureClass.EVIDENCE],
        }

    @property
    def adapter_issued(self) -> bool:
        """True only when this process's adapter sealed exactly this content (see ``issuance``)."""
        return verify_seal("decision", self.decision_id, self.issuance_seal)

    @property
    def investable(self) -> bool:
        """True only for adapter-issued STRICT_RESEARCH decisions that passed every gate.

        A caller-constructed decision with a self-consistent identifier is never investable.
        """
        return (self.status == DecisionStatus.ELIGIBLE and self.request.mode == EligibilityMode.STRICT_RESEARCH
                and self.adapter_issued)

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
        if self.decision.price_basis is not None and self.basis != self.decision.price_basis:
            raise ValueError("dataset price basis differs from the basis its decision released")
        if tuple(self.share_events) != tuple(self.decision.share_events):
            raise ValueError("dataset share events differ from the events its decision released")
        if sorted(e.event_id for e in self.adjustment_events) != sorted(e["event_id"] for e in self.share_events):
            raise ValueError("dataset adjustment events differ from its released share events")
        cutoff = self.decision.holdout_cutoff
        if cutoff is None or any(bar.date >= cutoff for bar in self.bars):
            raise ValueError("dataset contains locked-holdout observations")

    @property
    def fingerprint(self) -> str:
        return canonical_hash({"decision_id": self.decision.decision_id,
                               "bars": [asdict(bar) for bar in self.bars], "basis": self.basis,
                               "share_events": list(self.share_events)})


def symbol_problem(value: Any) -> str | None:
    """Why ``value`` is not an exact canonical NAME-SERIES symbol, or ``None``.

    Nothing is normalized: case, whitespace or other variants are reported, never
    silently mapped onto a different symbol.
    """
    if not isinstance(value, str):
        return "SYMBOL_NOT_A_STRING"
    if value != value.strip():
        return "SYMBOL_HAS_SURROUNDING_WHITESPACE"
    if value.upper() != value and _SYMBOL.fullmatch(value.upper()):
        return "SYMBOL_NOT_CANONICAL_CASE"
    if _SYMBOL.fullmatch(value) is None:
        return "SYMBOL_INVALID_FORMAT"
    return None


def universe_problems(symbols: Any) -> list[dict[str, Any]]:
    """Per-entry reasons a requested symbol list cannot be a universe (empty when valid)."""
    if not isinstance(symbols, (list, tuple)):
        return [{"index": None, "symbol": None, "reason": "SYMBOLS_NOT_A_LIST"}]
    if not 1 <= len(symbols) <= MAX_UNIVERSE:
        return [{"index": None, "symbol": None, "reason": f"UNIVERSE_SIZE_OUT_OF_RANGE_1_{MAX_UNIVERSE}"}]
    problems: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    folded: dict[str, int] = {}
    for index, value in enumerate(symbols):
        reason = symbol_problem(value)
        if reason is None and value in seen:
            reason = f"DUPLICATE_OF_INDEX_{seen[value]}"
        if reason is None and isinstance(value, str) and value.casefold() in folded:
            reason = f"AMBIGUOUS_WITH_INDEX_{folded[value.casefold()]}"
        if reason is not None:
            problems.append({"index": index, "symbol": value if isinstance(value, str) else repr(value),
                             "reason": reason})
        if isinstance(value, str):
            seen.setdefault(value, index)
            folded.setdefault(value.strip().casefold(), index)
    return problems


@dataclass(frozen=True)
class UniverseRequest:
    """The complete requested universe for one run; never replaced by a surviving subset.

    ``diagnostic_subset`` is an explicit opt-in, valid only in ``DIAGNOSTIC_ONLY``,
    to analyse the usable members while recording every excluded member.
    """

    symbols: tuple[str, ...]
    start: str
    end: str
    mode: EligibilityMode
    diagnostic_subset: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", EligibilityMode(self.mode))
        problems = universe_problems(self.symbols)
        if problems:
            raise ValueError(f"requested universe is invalid: {problems}")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if not isinstance(self.diagnostic_subset, bool):
            raise ValueError("diagnostic_subset must be a boolean")
        if self.diagnostic_subset and self.mode != EligibilityMode.DIAGNOSTIC_ONLY:
            raise ValueError("diagnostic_subset is only allowed in DIAGNOSTIC_ONLY mode")
        for symbol in self.symbols:  # same range and symbol rules as a single request
            DataEligibilityRequest(symbol, self.start, self.end, self.mode)

    def canonical(self) -> dict[str, Any]:
        """Order-independent identity: members sorted, plus the order they were requested in."""
        return {"contract": UNIVERSE_CONTRACT, "symbols": sorted(self.symbols),
                "requested_order": list(self.symbols), "start": self.start, "end": self.end,
                "mode": self.mode.value, "diagnostic_subset": self.diagnostic_subset}

    @property
    def universe_sha256(self) -> str:
        value = self.canonical()
        value.pop("requested_order")
        return canonical_hash(value)

    def member_requests(self) -> list[DataEligibilityRequest]:
        return [DataEligibilityRequest(symbol, self.start, self.end, self.mode) for symbol in sorted(self.symbols)]


@dataclass(frozen=True)
class UniverseAssessment:
    """All-or-nothing eligibility for a requested universe.

    ``STRICT_RESEARCH`` releases data only when every requested member is
    ``ELIGIBLE``. ``DIAGNOSTIC_ONLY`` releases data only when every member is
    usable, unless the request explicitly opted into ``diagnostic_subset``.
    """

    universe: UniverseRequest
    decisions: tuple[DataEligibilityDecision, ...]
    status: DecisionStatus
    released: tuple[VerifiedHistoricalDataset, ...]
    excluded: tuple[dict[str, Any], ...]
    issuance_seal: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DecisionStatus(self.status))
        self.verify_consistency()

    def verify_consistency(self) -> None:
        if [d.request.symbol for d in self.decisions] != sorted(self.universe.symbols):
            raise ValueError("universe assessment must hold exactly one decision per requested symbol")
        for decision in self.decisions:
            decision.verify()
            request = decision.request
            if (request.start, request.end, request.mode) != (self.universe.start, self.universe.end,
                                                               self.universe.mode):
                raise ValueError("member decision does not match the requested universe range or mode")
        for dataset in self.released:
            dataset.verify()
        released = {d.decision.decision_id for d in self.released}
        usable = {d.decision_id for d in self.decisions if d.usable}
        if not released <= usable:
            raise ValueError("released datasets must come from this assessment's usable decisions")
        if self.status == DecisionStatus.ELIGIBLE:
            if self.universe.mode != EligibilityMode.STRICT_RESEARCH or released != {
                    d.decision_id for d in self.decisions} or any(
                    d.status != DecisionStatus.ELIGIBLE for d in self.decisions):
                raise ValueError("an ELIGIBLE universe must release every requested member")
        elif self.status == DecisionStatus.DIAGNOSTIC_ALLOWED:
            if self.universe.mode != EligibilityMode.DIAGNOSTIC_ONLY or not released:
                raise ValueError("a DIAGNOSTIC_ALLOWED universe must be diagnostic and release data")
            if not self.universe.diagnostic_subset and released != {d.decision_id for d in self.decisions}:
                raise ValueError("a partial diagnostic release requires an explicit diagnostic_subset request")
        elif released:
            raise ValueError("a BLOCKED or REJECTED universe releases no data")

    @property
    def assessment_id(self) -> str:
        return canonical_hash({"universe": self.universe.canonical(), "status": self.status.value,
                               "decisions": [d.decision_id for d in self.decisions],
                               "released": sorted(d.decision.decision_id for d in self.released),
                               "excluded": list(self.excluded)})

    @property
    def adapter_issued(self) -> bool:
        return verify_seal("universe", self.assessment_id, self.issuance_seal) and all(
            d.adapter_issued for d in self.decisions)

    @property
    def investable(self) -> bool:
        return (self.status == DecisionStatus.ELIGIBLE and self.adapter_issued
                and all(d.decision.investable for d in self.released))

    def to_dict(self) -> dict[str, Any]:
        return {"assessment_id": self.assessment_id, "universe": self.universe.canonical(),
                "universe_sha256": self.universe.universe_sha256, "status": self.status.value,
                "investable": self.investable, "adapter_issued": self.adapter_issued,
                "excluded": list(self.excluded),
                "members": [{"symbol": d.request.symbol, "status": d.status.value,
                             "failures": [{"code": f.code, "class": f.failure_class.value, "message": f.message}
                                          for f in d.failures],
                             "missing_evidence": [f.code for f in d.failures
                                                  if f.failure_class == FailureClass.EVIDENCE],
                             "decision_id": d.decision_id} for d in self.decisions]}


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
