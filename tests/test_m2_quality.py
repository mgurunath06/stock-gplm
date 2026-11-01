from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_gplm.m2 import (
    CalendarDay,
    CorporateAction,
    CorporateActionCatalog,
    HorizonRequest,
    Instrument,
    InstrumentInventory,
    NSETradingCalendar,
    Provenance,
    ProviderUnavailable,
    analyze_history,
)


def _provenance(source: str = "synthetic-test") -> Provenance:
    return Provenance(source=source, as_of_date="2026-10-31", reference="test-only")


def _row(day: date, close: str = "105.00") -> dict[str, str]:
    return {
        "time": day.strftime("%d-%b-%Y").upper(),
        "into": "100.00", "inth": "110.00", "intl": "90.00", "intc": close,
        "intv": "123.00",
        "ssboe": str(int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())),
    }


def _publish(root: Path, symbol: str, start: date, end: date, rows) -> Path:
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{start.isoformat()}_{end.isoformat()}"
    path = directory / f"{base}.json.gz"
    raw = gzip.compress(json.dumps([json.dumps(item) for item in rows]).encode(), mtime=0)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_name(f"{base}.meta.json").write_text(json.dumps({
        "quality_status": "UNVALIDATED_RAW", "records": len(rows),
        "sha256_compressed": digest,
    }))
    path.with_name(f"{path.name}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": path.name, "sha256": digest,
        "bytes": len(raw), "records": len(rows),
    }))
    return path


def _inventory(symbol: str = "TEST-EQ", listing: str | None = "2026-01-02"):
    provenance = _provenance("synthetic-instrument")
    fields = {"nse_symbol": provenance}
    if listing:
        fields["listing_date"] = provenance
        fields["membership_verified_through"] = provenance
    return InstrumentInventory(
        as_of_date="2026-10-31",
        instruments=(Instrument(
            symbol,
            listing_date=listing,
            membership_verified_through="2026-12-31" if listing else None,
            field_provenance=fields,
        ),),
    )


def _calendar(start: date, end: date, statuses: dict[str, str]) -> NSETradingCalendar:
    provenance = _provenance("synthetic-calendar")
    days = []
    cursor = start
    while cursor <= end:
        status = statuses.get(cursor.isoformat(), "TRADING_SESSION")
        days.append(CalendarDay(cursor.isoformat(), status, provenance))
        cursor += timedelta(days=1)
    return NSETradingCalendar(
        coverage_start=start.isoformat(), coverage_end=end.isoformat(),
        coverage_complete=True, days=tuple(days),
    )


def _action_catalog(
    symbol: str, start: date, end: date, actions: tuple[CorporateAction, ...] = ()
) -> CorporateActionCatalog:
    return CorporateActionCatalog(
        symbol, "COMPLETE", start.isoformat(), end.isoformat(),
        _provenance("synthetic-action-catalog"), actions,
    )


def test_complete_synthetic_history_is_accepted(tmp_path):
    start, end = date(2026, 1, 2), date(2026, 1, 5)
    statuses = {"2026-01-03": "MARKET_CLOSED", "2026-01-04": "MARKET_CLOSED"}
    rows = [_row(date(2026, 1, 2)), _row(date(2026, 1, 5))]
    source = _publish(tmp_path, "TEST-EQ", start, end, rows)
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(),
        calendar=_calendar(start, end, statuses),
        horizons=[HorizonRequest("synthetic-horizon", start.isoformat(), end.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", start, end),
    )
    assert report.decision == "ACCEPT"
    horizon = report.horizons[0]
    assert horizon.complete
    assert horizon.coverage_ratio == "1.000000"
    assert horizon.classification_counts == {"OBSERVED": 2, "MARKET_CLOSED": 2}
    assert report.raw_candle_acceptance.decision == "ACCEPT"
    assert report.historical_completeness.decision == "ACCEPT"
    assert report.model_training_readiness.decision == "NOT_READY"


def test_three_nonoverlapping_segments_are_checked_together(tmp_path):
    days = [date(2026, 1, day) for day in (2, 3, 4)]
    paths = [
        _publish(tmp_path / f"part-{index}", "TEST-EQ", day, day, [_row(day)])
        for index, day in enumerate(days)
    ]
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=paths, inventory=_inventory(),
        calendar=_calendar(days[0], days[-1], {}),
        horizons=[HorizonRequest("three-segment", days[0].isoformat(), days[-1].isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", days[0], days[-1]),
    )
    assert report.decision == "ACCEPT"
    assert len(report.segments) == 3
    assert not report.duplicate_dates and not report.contradictory_dates


def test_gap_between_segments_is_missing_expected_session(tmp_path):
    first, missing, last = (date(2026, 1, day) for day in (2, 3, 4))
    paths = [
        _publish(tmp_path / "first", "TEST-EQ", first, first, [_row(first)]),
        _publish(tmp_path / "last", "TEST-EQ", last, last, [_row(last)]),
    ]
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=paths, inventory=_inventory(),
        calendar=_calendar(first, last, {}),
        horizons=[HorizonRequest("gap-test", first.isoformat(), last.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", first, last),
    )
    assert report.decision == "QUARANTINE"
    assert "MISSING_EXPECTED_SESSION" in report.reasons
    assessments = {item.date: item.classification for item in report.horizons[0].dates}
    assert assessments[missing.isoformat()] == "MISSING_EXPECTED_SESSION"


def test_all_missing_date_classifications_are_distinct(tmp_path):
    start, end = date(2026, 1, 1), date(2026, 1, 6)
    source = _publish(tmp_path, "TEST-EQ", start, end, [_row(end)])
    calendar = NSETradingCalendar(
        days=(
            CalendarDay("2026-01-02", "MARKET_CLOSED", _provenance()),
            CalendarDay("2026-01-03", "TRADING_SESSION", _provenance()),
            CalendarDay("2026-01-04", "TRADING_SESSION", _provenance()),
            CalendarDay("2026-01-06", "TRADING_SESSION", _provenance()),
        )
    )
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(listing="2026-01-02"),
        calendar=calendar,
        horizons=[HorizonRequest("classification-test", start.isoformat(), end.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", start, end),
        provider_unavailable=[ProviderUnavailable(
            "2026-01-03", "2026-01-03", _provenance("provider-notice"), "documented outage"
        )],
    )
    by_date = {item.date: item.classification for item in report.horizons[0].dates}
    assert by_date == {
        "2026-01-01": "PRE_LISTING",
        "2026-01-02": "MARKET_CLOSED",
        "2026-01-03": "PROVIDER_UNAVAILABLE",
        "2026-01-04": "MISSING_EXPECTED_SESSION",
        "2026-01-05": "UNKNOWN",
        "2026-01-06": "OBSERVED",
    }
    assert report.decision == "QUARANTINE"


def test_identical_cross_segment_duplicate_and_overlap_are_reported(tmp_path):
    day = date(2026, 1, 3)
    first = _publish(tmp_path, "TEST-EQ", date(2026, 1, 2), day, [_row(day)])
    second = _publish(tmp_path, "TEST-EQ", day, date(2026, 1, 4), [_row(day)])
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[first, second], inventory=_inventory(),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("duplicate-test", day.isoformat(), day.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", day, day),
    )
    assert day.isoformat() in report.duplicate_dates
    assert not report.contradictory_dates
    assert {item.code for item in report.issues} >= {
        "SEGMENT_RANGE_OVERLAP", "DUPLICATE_TRADING_DATE"
    }
    assert report.decision == "QUARANTINE"


def test_contradictory_cross_segment_ohlcv_is_reported(tmp_path):
    day = date(2026, 1, 3)
    first = _publish(tmp_path, "TEST-EQ", date(2026, 1, 2), day, [_row(day)])
    other_root = tmp_path / "second-provider-export"
    second = _publish(other_root, "TEST-EQ", day, date(2026, 1, 4), [_row(day, "106.00")])
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[first, second], inventory=_inventory(),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("contradiction-test", day.isoformat(), day.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", day, day),
    )
    assert day.isoformat() in report.contradictory_dates
    assert any(item.code == "CONTRADICTORY_OHLCV" for item in report.issues)
    assert report.decision == "QUARANTINE"


def test_unknown_listing_date_blocks_completeness(tmp_path):
    day = date(2026, 1, 3)
    source = _publish(tmp_path, "TEST-EQ", day, day, [_row(day)])
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(listing=None),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("unknown-listing", day.isoformat(), day.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", day, day),
    )
    assert report.decision == "QUARANTINE"
    assert "LISTING_DATE_UNKNOWN" in report.reasons
    assert not report.horizons[0].complete


def test_unresolved_corporate_action_is_metadata_and_blocker(tmp_path):
    day = date(2026, 1, 3)
    source = _publish(tmp_path, "TEST-EQ", day, day, [_row(day)])
    action = CorporateAction(
        "TEST-EQ", "SPLIT", day.isoformat(), _provenance("action-source"),
        adjustment_status="POLICY_REQUIRED", details={"ratio": "2:1"},
    )
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("action-test", day.isoformat(), day.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", day, day, (action,)),
    )
    assert report.decision == "QUARANTINE"
    assert report.corporate_actions[0]["details"] == {"ratio": "2:1"}
    assert "CORPORATE_ACTION_ADJUSTMENT_UNRESOLVED" in report.reasons


def test_rejected_m1_segment_rejects_m2(tmp_path):
    day = date(2026, 1, 3)
    source = _publish(tmp_path, "TEST-EQ", day, day, [_row(day)])
    source.write_bytes(source.read_bytes() + b"tampered")
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("rejected", day.isoformat(), day.isoformat())],
        corporate_action_catalog=_action_catalog("TEST-EQ", day, day),
    )
    assert report.decision == "REJECT"
    assert "SEGMENT_PUBLICATION_REJECTED" in report.reasons


def test_existing_real_reliance_segment_regression_without_completeness_claim():
    source = Path(
        "data/raw/shoonya/NSE/RELIANCE-EQ/2026-10-01_2026-10-07.json.gz"
    )
    if not source.exists():
        pytest.skip("local accepted RELIANCE segment is not present")
    inventory = _inventory(symbol="RELIANCE-EQ", listing=None)
    observed = {"2026-10-01", "2026-10-05", "2026-10-06", "2026-10-07"}
    calendar = NSETradingCalendar(days=tuple(
        CalendarDay(day, "TRADING_SESSION", _provenance("synthetic-regression-calendar"))
        for day in sorted(observed)
    ))
    report = analyze_history(
        symbol="RELIANCE-EQ", segment_paths=[source], inventory=inventory,
        calendar=calendar,
        horizons=[HorizonRequest("real-segment-regression", "2026-10-01", "2026-10-07")],
    )
    assert report.segments[0].publication_integrity_valid
    assert report.segments[0].content_quality_valid
    assert report.segments[0].records == 4
    assert report.segments[0].normalized_dates == tuple(sorted(observed))
    assert report.decision == "QUARANTINE"
    assert "LISTING_DATE_UNKNOWN" in report.reasons


def test_unknown_corporate_action_coverage_is_not_called_partial(tmp_path):
    day = date(2026, 1, 3)
    source = _publish(tmp_path, "TEST-EQ", day, day, [_row(day)])
    catalog = CorporateActionCatalog("TEST-EQ", "UNKNOWN")
    report = analyze_history(
        symbol="TEST-EQ", segment_paths=[source], inventory=_inventory(),
        calendar=_calendar(day, day, {}),
        horizons=[HorizonRequest("unknown-actions", day.isoformat(), day.isoformat())],
        corporate_action_catalog=catalog,
    )
    assert "CORPORATE_ACTION_COVERAGE_UNKNOWN" in report.reasons
    assert "CORPORATE_ACTION_COVERAGE_INCOMPLETE" not in report.reasons


def test_m2_cli_writes_inventory_and_quality_reports(tmp_path):
    day = date(2026, 1, 3)
    source = _publish(tmp_path / "raw", "TEST-EQ", day, day, [_row(day)])
    provenance = {"source": "synthetic-test", "as_of_date": "2026-10-31"}
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps({
        "schema_version": 1, "as_of_date": "2026-10-31", "instruments": [{
            "nse_symbol": "TEST-EQ", "isin": None, "shoonya_token": None,
            "listing_date": "2026-01-01", "delisting_date": None,
            "membership_verified_through": "2026-12-31", "aliases": [],
            "field_provenance": {
                "nse_symbol": provenance, "listing_date": provenance,
                "membership_verified_through": provenance,
            },
        }]
    }))
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps({
        "schema_version": 1, "exchange": "NSE", "timezone": "Asia/Kolkata",
        "coverage_start": day.isoformat(), "coverage_end": day.isoformat(),
        "coverage_complete": True,
        "days": [{"date": day.isoformat(), "status": "TRADING_SESSION", "provenance": provenance}],
    }))
    config_path = tmp_path / "quality-config.json"
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "cli-test", "start_date": day.isoformat(), "end_date": day.isoformat()}],
        "corporate_action_catalog": {
            "symbol": "TEST-EQ", "coverage_status": "COMPLETE",
            "coverage_start": day.isoformat(), "coverage_end": day.isoformat(),
            "provenance": provenance, "actions": [],
        },
        "provider_unavailable": [],
    }))
    output = tmp_path / "reports"
    result = subprocess.run([
        sys.executable, "-m", "stock_gplm.m2.cli", "--symbol", "TEST-EQ",
        "--inventory", str(inventory_path), "--calendar", str(calendar_path),
        "--quality-config", str(config_path), "--output-dir", str(output), str(source),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads((output / "TEST-EQ.historical-quality.json").read_text())["decision"] == "ACCEPT"
    assert json.loads((output / "TEST-EQ.inventory.json").read_text())["schema_version"] == 1
