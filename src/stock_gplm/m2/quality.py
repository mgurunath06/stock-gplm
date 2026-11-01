"""Cross-segment historical-quality analysis without source mutation or filling."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from stock_gplm.ingestion import validate_daily

from .models import (
    CorporateActionCatalog,
    HorizonRequest,
    InstrumentInventory,
    NSETradingCalendar,
    ProviderUnavailable,
)

Classification = Literal[
    "OBSERVED",
    "PRE_LISTING",
    "MARKET_CLOSED",
    "PROVIDER_UNAVAILABLE",
    "MISSING_EXPECTED_SESSION",
    "UNKNOWN",
]
Severity = Literal["INFO", "WARNING", "ERROR"]


@dataclass(frozen=True)
class DecisionAssessment:
    decision: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: Severity
    message: str
    date: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class DateAssessment:
    date: str
    classification: Classification
    reason: str


@dataclass(frozen=True)
class HorizonCompleteness:
    name: str
    start_date: str
    end_date: str
    complete: bool
    observed_sessions: int
    applicable_expected_sessions: int
    coverage_ratio: str | None
    classification_counts: dict[str, int]
    dates: tuple[DateAssessment, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class SegmentAssessment:
    source: str
    requested_start: str | None
    requested_end: str | None
    publication_integrity_valid: bool
    content_quality_valid: bool
    disposition: str
    records: int
    chronology: str
    normalized_dates: tuple[str, ...]
    issues: tuple[dict[str, Any], ...]


@dataclass
class HistoricalQualityReport:
    symbol: str
    decision: Literal["ACCEPT", "QUARANTINE", "REJECT"]
    reasons: list[str]
    inventory_as_of_date: str
    instrument: dict[str, Any] | None
    calendar: dict[str, Any]
    segments: list[SegmentAssessment]
    issues: list[QualityIssue]
    duplicate_dates: dict[str, list[str]]
    contradictory_dates: dict[str, list[str]]
    corporate_actions: list[dict[str, Any]]
    corporate_action_coverage: dict[str, Any]
    horizons: list[HorizonCompleteness]
    raw_candle_acceptance: DecisionAssessment
    historical_completeness: DecisionAssessment
    model_training_readiness: DecisionAssessment
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _calendar_snapshot(calendar: NSETradingCalendar) -> dict[str, Any]:
    return {
        "exchange": calendar.exchange,
        "timezone": calendar.timezone,
        "coverage_start": calendar.coverage_start,
        "coverage_end": calendar.coverage_end,
        "coverage_complete": calendar.coverage_complete,
        "supplied_dates": len(calendar.days),
    }


def _segment_assessment(path: Path, report) -> SegmentAssessment:
    content = report.content_quality
    issues = [asdict(issue) for issue in report.publication_integrity.issues]
    issues.extend(asdict(issue) for issue in content.issues)
    return SegmentAssessment(
        source=str(path),
        requested_start=report.segment_start,
        requested_end=report.segment_end,
        publication_integrity_valid=report.publication_integrity.valid,
        content_quality_valid=content.valid,
        disposition=report.disposition,
        records=content.records_seen,
        chronology=content.chronology,
        normalized_dates=tuple(item.trading_date for item in content.normalized_candles),
        issues=tuple(issues),
    )


def _candle_signature(candle) -> tuple[Any, ...]:
    return (
        candle.source_timestamp_utc,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
        candle.volume,
    )


def _days(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _classify_missing(
    day: date,
    membership: str,
    calendar_status: str,
    unavailable: tuple[ProviderUnavailable, ...],
) -> DateAssessment:
    iso = day.isoformat()
    if membership == "PRE_LISTING":
        return DateAssessment(iso, "PRE_LISTING", "verified listing date is later")
    if calendar_status == "MARKET_CLOSED":
        return DateAssessment(iso, "MARKET_CLOSED", "explicit calendar entry")
    if calendar_status in {"TRADING_SESSION", "SPECIAL_SESSION"}:
        outage = next((item for item in unavailable if item.contains(day)), None)
        if outage:
            return DateAssessment(iso, "PROVIDER_UNAVAILABLE", outage.reason)
        if membership == "LISTED":
            return DateAssessment(
                iso, "MISSING_EXPECTED_SESSION", "listed instrument and explicit session"
            )
        if membership == "DELISTED":
            return DateAssessment(iso, "UNKNOWN", "date is after verified delisting")
        return DateAssessment(iso, "UNKNOWN", "listing membership is unknown")
    return DateAssessment(iso, "UNKNOWN", "no explicit calendar entry")


def _horizon_completeness(
    request: HorizonRequest,
    instrument,
    calendar: NSETradingCalendar,
    observed_dates: set[str],
    unavailable: tuple[ProviderUnavailable, ...],
    global_blockers: set[str],
) -> HorizonCompleteness:
    assessments: list[DateAssessment] = []
    observed_sessions = 0
    expected_sessions = 0
    blockers = set(global_blockers)
    for day in _days(date.fromisoformat(request.start_date), date.fromisoformat(request.end_date)):
        iso = day.isoformat()
        membership = instrument.membership_on(day) if instrument else "UNKNOWN"
        calendar_status = calendar.status_on(day)
        if calendar_status in {"TRADING_SESSION", "SPECIAL_SESSION"} and membership == "LISTED":
            expected_sessions += 1
        if iso in observed_dates:
            assessments.append(DateAssessment(iso, "OBSERVED", "validated ShoonyaDailyV1 candle"))
            if calendar_status in {"TRADING_SESSION", "SPECIAL_SESSION"} and membership == "LISTED":
                observed_sessions += 1
            if calendar_status == "MARKET_CLOSED":
                blockers.add("CANDLE_ON_MARKET_CLOSED_DATE")
            elif calendar_status == "UNKNOWN":
                blockers.add("CALENDAR_DATE_UNKNOWN")
            if membership != "LISTED":
                blockers.add(f"OBSERVED_WITH_MEMBERSHIP_{membership}")
            continue
        assessment = _classify_missing(day, membership, calendar_status, unavailable)
        assessments.append(assessment)
        if assessment.classification in {
            "PROVIDER_UNAVAILABLE", "MISSING_EXPECTED_SESSION", "UNKNOWN"
        }:
            blockers.add(assessment.classification)

    counts: dict[str, int] = {}
    for item in assessments:
        counts[item.classification] = counts.get(item.classification, 0) + 1
    ratio = None
    if expected_sessions:
        ratio = str(
            (Decimal(observed_sessions) / Decimal(expected_sessions)).quantize(
                Decimal("0.000001")
            )
        )
    return HorizonCompleteness(
        name=request.name,
        start_date=request.start_date,
        end_date=request.end_date,
        complete=not blockers,
        observed_sessions=observed_sessions,
        applicable_expected_sessions=expected_sessions,
        coverage_ratio=ratio,
        classification_counts=counts,
        dates=tuple(assessments),
        blockers=tuple(sorted(blockers)),
    )


def analyze_history(
    *,
    symbol: str,
    segment_paths: list[Path],
    inventory: InstrumentInventory,
    calendar: NSETradingCalendar,
    horizons: list[HorizonRequest],
    corporate_action_catalog: CorporateActionCatalog | None = None,
    provider_unavailable: list[ProviderUnavailable] | None = None,
) -> HistoricalQualityReport:
    """Analyze explicitly supplied local segments; never mutate or fill them."""
    if not segment_paths:
        raise ValueError("at least one segment path is required")
    if not horizons:
        raise ValueError("at least one explicit horizon request is required")
    unavailable = tuple(provider_unavailable or [])
    instrument = inventory.get(symbol)
    issues: list[QualityIssue] = []
    global_blockers: set[str] = set()
    if instrument is None:
        issues.append(QualityIssue("INSTRUMENT_UNKNOWN", "WARNING", symbol))
        global_blockers.add("INSTRUMENT_UNKNOWN")
    elif instrument.listing_date is None:
        issues.append(
            QualityIssue(
                "LISTING_DATE_UNKNOWN",
                "WARNING",
                "historical membership and completeness cannot be established",
            )
        )
        global_blockers.add("LISTING_DATE_UNKNOWN")
    elif instrument.delisting_date is None and instrument.membership_verified_through is None:
        issues.append(
            QualityIssue(
                "MEMBERSHIP_COVERAGE_UNKNOWN",
                "WARNING",
                "listing is known but continued membership has no verified boundary",
            )
        )
        global_blockers.add("MEMBERSHIP_COVERAGE_UNKNOWN")

    m1_reports = [(Path(path), validate_daily(Path(path))) for path in segment_paths]
    segments = [_segment_assessment(path, report) for path, report in m1_reports]
    publication_failed = any(not report.publication_integrity.valid for _, report in m1_reports)
    content_failed = any(
        report.publication_integrity.valid and not report.content_quality.valid
        for _, report in m1_reports
    )
    if publication_failed:
        global_blockers.add("SEGMENT_PUBLICATION_REJECTED")
    if content_failed:
        global_blockers.add("SEGMENT_CONTENT_QUARANTINED")

    ranges = [
        (path, date.fromisoformat(report.segment_start), date.fromisoformat(report.segment_end))
        for path, report in m1_reports
        if report.segment_start and report.segment_end
    ]
    for index, (left_path, left_start, left_end) in enumerate(ranges):
        for right_path, right_start, right_end in ranges[index + 1 :]:
            if max(left_start, right_start) <= min(left_end, right_end):
                issues.append(
                    QualityIssue(
                        "SEGMENT_RANGE_OVERLAP",
                        "WARNING",
                        "requested segment ranges overlap",
                        sources=(str(left_path), str(right_path)),
                    )
                )
                global_blockers.add("SEGMENT_RANGE_OVERLAP")

    by_date: dict[str, list[tuple[str, Any]]] = {}
    for path, report in m1_reports:
        if not report.content_quality.valid:
            continue
        for candle in report.content_quality.normalized_candles:
            by_date.setdefault(candle.trading_date, []).append((str(path), candle))

    duplicate_dates: dict[str, list[str]] = {}
    contradictory_dates: dict[str, list[str]] = {}
    for trading_date, entries in by_date.items():
        if len(entries) < 2:
            continue
        sources = [source for source, _ in entries]
        signatures = {_candle_signature(candle) for _, candle in entries}
        if len(signatures) == 1:
            duplicate_dates[trading_date] = sources
            issues.append(
                QualityIssue(
                    "DUPLICATE_TRADING_DATE", "WARNING", "identical candle repeated",
                    date=trading_date, sources=tuple(sources)
                )
            )
            global_blockers.add("DUPLICATE_TRADING_DATE")
        else:
            contradictory_dates[trading_date] = sources
            issues.append(
                QualityIssue(
                    "CONTRADICTORY_OHLCV", "ERROR", "same date has contradictory records",
                    date=trading_date, sources=tuple(sources)
                )
            )
            global_blockers.add("CONTRADICTORY_OHLCV")

    horizon_start = min(date.fromisoformat(item.start_date) for item in horizons)
    horizon_end = max(date.fromisoformat(item.end_date) for item in horizons)
    if corporate_action_catalog is None or corporate_action_catalog.symbol != symbol:
        relevant_actions = []
        corporate_action_coverage = {
            "status": "UNKNOWN", "coverage_start": None, "coverage_end": None,
            "provenance": None,
        }
        global_blockers.add("CORPORATE_ACTION_COVERAGE_UNKNOWN")
    else:
        relevant_actions = list(corporate_action_catalog.actions)
        corporate_action_coverage = {
            "status": corporate_action_catalog.coverage_status,
            "coverage_start": corporate_action_catalog.coverage_start,
            "coverage_end": corporate_action_catalog.coverage_end,
            "provenance": (
                asdict(corporate_action_catalog.provenance)
                if corporate_action_catalog.provenance else None
            ),
        }
        if corporate_action_catalog.coverage_status == "UNKNOWN":
            global_blockers.add("CORPORATE_ACTION_COVERAGE_UNKNOWN")
        elif not corporate_action_catalog.covers(horizon_start, horizon_end):
            global_blockers.add("CORPORATE_ACTION_COVERAGE_INCOMPLETE")
    for action in relevant_actions:
        effective = date.fromisoformat(action.effective_date)
        if horizon_start <= effective <= horizon_end and action.adjustment_status in {
            "UNKNOWN", "UNADJUSTED", "POLICY_REQUIRED"
        }:
            issues.append(
                QualityIssue(
                    "CORPORATE_ACTION_ADJUSTMENT_UNRESOLVED",
                    "WARNING",
                    f"{action.action_type} has status {action.adjustment_status}; no adjustment applied",
                    date=action.effective_date,
                )
            )
            global_blockers.add("CORPORATE_ACTION_ADJUSTMENT_UNRESOLVED")

    observed_dates = set(by_date)
    horizon_reports = [
        _horizon_completeness(
            request, instrument, calendar, observed_dates, unavailable, global_blockers
        )
        for request in horizons
    ]
    all_blockers = set(global_blockers)
    for horizon in horizon_reports:
        all_blockers.update(horizon.blockers)
    if publication_failed:
        decision: Literal["ACCEPT", "QUARANTINE", "REJECT"] = "REJECT"
    elif content_failed or all_blockers or not all(item.complete for item in horizon_reports):
        decision = "QUARANTINE"
    else:
        decision = "ACCEPT"
    reasons = sorted(all_blockers)
    if not reasons:
        reasons = ["all supplied segments and requested horizon windows are complete"]

    if publication_failed:
        raw_decision = DecisionAssessment("REJECT", ("SEGMENT_PUBLICATION_REJECTED",))
    elif content_failed:
        raw_decision = DecisionAssessment("QUARANTINE", ("SEGMENT_CONTENT_QUARANTINED",))
    else:
        raw_decision = DecisionAssessment("ACCEPT", ("all supplied M1 segments passed",))
    historical_decision = DecisionAssessment(decision, tuple(reasons))
    training_reasons = {
        "MODEL_TRAINING_OUTSIDE_M2_1",
        "CHRONOLOGICAL_HOLDOUT_NOT_CONFIGURED",
        "LEAKAGE_CONTROLS_NOT_EVALUATED",
    }
    if decision != "ACCEPT":
        training_reasons.add("HISTORICAL_COMPLETENESS_NOT_ACCEPTED")

    return HistoricalQualityReport(
        symbol=symbol,
        decision=decision,
        reasons=reasons,
        inventory_as_of_date=inventory.as_of_date,
        instrument=asdict(instrument) if instrument else None,
        calendar=_calendar_snapshot(calendar),
        segments=segments,
        issues=issues,
        duplicate_dates=duplicate_dates,
        contradictory_dates=contradictory_dates,
        corporate_actions=[asdict(item) for item in relevant_actions],
        corporate_action_coverage=corporate_action_coverage,
        horizons=horizon_reports,
        raw_candle_acceptance=raw_decision,
        historical_completeness=historical_decision,
        model_training_readiness=DecisionAssessment(
            "NOT_READY", tuple(sorted(training_reasons))
        ),
    )
