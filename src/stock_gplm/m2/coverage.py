"""Reference-data coverage by symbol and explicit research horizon."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Literal

from .models import CorporateActionCatalog, HorizonRequest, InstrumentInventory, NSETradingCalendar
from .reference_data import ReferenceDataManifestV1


@dataclass(frozen=True)
class SymbolHorizonCoverage:
    symbol: str
    horizon: str
    start_date: str
    end_date: str
    decision: Literal["READY", "INCOMPLETE"]
    blockers: tuple[str, ...]
    identity_fields_known: dict[str, bool]
    alias_history_status: str
    calendar_counts: dict[str, int]
    membership_counts: dict[str, int]
    corporate_action_coverage: str


@dataclass(frozen=True)
class ReferenceCoverageReport:
    decision: Literal["READY", "INCOMPLETE"]
    coverage: tuple[SymbolHorizonCoverage, ...]
    sources: tuple[dict[str, Any], ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _days(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def build_reference_coverage_report(
    *,
    symbols: list[str],
    horizons: list[HorizonRequest],
    inventory: InstrumentInventory,
    calendar: NSETradingCalendar,
    corporate_action_catalogs: list[CorporateActionCatalog],
    sources: list[ReferenceDataManifestV1] | None = None,
) -> ReferenceCoverageReport:
    """Report evidence coverage without inferring any missing reference facts."""
    if not symbols or not horizons:
        raise ValueError("coverage report requires symbols and horizons")
    catalogs = {item.symbol: item for item in corporate_action_catalogs}
    rows: list[SymbolHorizonCoverage] = []
    for symbol in symbols:
        instrument = inventory.get(symbol)
        for horizon in horizons:
            start = date.fromisoformat(horizon.start_date)
            end = date.fromisoformat(horizon.end_date)
            calendar_counts: dict[str, int] = {}
            membership_counts: dict[str, int] = {}
            for day in _days(start, end):
                status = calendar.status_on(day)
                calendar_counts[status] = calendar_counts.get(status, 0) + 1
                membership = instrument.membership_on(day) if instrument else "UNKNOWN"
                membership_counts[membership] = membership_counts.get(membership, 0) + 1

            identity = {
                "nse_symbol": instrument is not None,
                "isin": instrument is not None and instrument.isin is not None,
                "shoonya_token": instrument is not None and instrument.shoonya_token is not None,
                "listing_date": instrument is not None and instrument.listing_date is not None,
                "membership_boundary": instrument is not None and (
                    instrument.delisting_date is not None
                    or instrument.membership_verified_through is not None
                ),
            }
            blockers: set[str] = set()
            if instrument is None:
                blockers.add("INSTRUMENT_UNKNOWN")
                alias_status = "UNKNOWN"
            else:
                alias_status = instrument.alias_history_status
                if not identity["listing_date"]:
                    blockers.add("LISTING_DATE_UNKNOWN")
                if not identity["membership_boundary"]:
                    blockers.add("MEMBERSHIP_BOUNDARY_UNKNOWN")
                if alias_status != "COMPLETE":
                    blockers.add("ALIAS_HISTORY_INCOMPLETE")
            if calendar_counts.get("UNKNOWN", 0):
                blockers.add("CALENDAR_COVERAGE_INCOMPLETE")
            if membership_counts.get("UNKNOWN", 0):
                blockers.add("MEMBERSHIP_COVERAGE_INCOMPLETE")
            catalog = catalogs.get(symbol)
            corporate_status = catalog.coverage_status if catalog else "UNKNOWN"
            if catalog is None or not catalog.covers(start, end):
                blockers.add("CORPORATE_ACTION_COVERAGE_INCOMPLETE")
            rows.append(
                SymbolHorizonCoverage(
                    symbol=symbol,
                    horizon=horizon.name,
                    start_date=horizon.start_date,
                    end_date=horizon.end_date,
                    decision="INCOMPLETE" if blockers else "READY",
                    blockers=tuple(sorted(blockers)),
                    identity_fields_known=identity,
                    alias_history_status=alias_status,
                    calendar_counts=calendar_counts,
                    membership_counts=membership_counts,
                    corporate_action_coverage=corporate_status,
                )
            )
    return ReferenceCoverageReport(
        decision="READY" if all(row.decision == "READY" for row in rows) else "INCOMPLETE",
        coverage=tuple(rows),
        sources=tuple(item.to_dict() for item in (sources or [])),
    )
