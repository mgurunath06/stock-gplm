from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

from stock_gplm.m2 import (
    CalendarDay,
    CorporateActionCatalog,
    HorizonRequest,
    Instrument,
    InstrumentInventory,
    NSETradingCalendar,
    Provenance,
    build_reference_coverage_report,
    import_corporate_actions_v1,
    import_historical_symbol_mappings_v1,
    import_nse_calendar_v1,
    import_shoonya_instrument_master_v1,
    merge_historical_symbol_mappings,
)


def _manifest(
    payload: Path,
    contract: str,
    *,
    start: str = "2026-01-01",
    end: str = "2026-01-04",
    coverage: str = "PARTIAL",
) -> Path:
    path = payload.with_name(f"{payload.name}.manifest.json")
    path.write_text(json.dumps({
        "schema_version": 1,
        "contract": contract,
        "payload_file": payload.name,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "source": "synthetic authoritative fixture",
        "source_reference": "test://reference/source",
        "publication_date": "2026-01-05",
        "acquired_at_utc": "2026-01-05T12:30:00Z",
        "effective_start": start,
        "effective_end": end,
        "coverage_status": coverage,
    }))
    return path


def test_import_calendar_csv_preserves_metadata_and_explicit_statuses(tmp_path):
    payload = tmp_path / "nse-calendar.csv"
    payload.write_text(
        "date,status,label\n"
        "2026-01-01,MARKET_CLOSED,Holiday\n"
        "2026-01-02,TRADING_SESSION,\n"
        "2026-01-03,SPECIAL_SESSION,Special test session\n"
        "2026-01-04,UNKNOWN,Source unresolved\n"
    )
    manifest = _manifest(payload, "stock-gplm.nse-calendar.v1", coverage="COMPLETE")
    imported = import_nse_calendar_v1(payload, manifest)
    assert imported.manifest.publication_date == "2026-01-05"
    assert imported.manifest.acquired_at_utc == "2026-01-05T12:30:00Z"
    assert imported.calendar.coverage_complete
    assert imported.calendar.status_on(date(2026, 1, 1)) == "MARKET_CLOSED"
    assert imported.calendar.status_on(date(2026, 1, 3)) == "SPECIAL_SESSION"
    assert imported.calendar.status_on(date(2026, 1, 4)) == "UNKNOWN"


def test_import_calendar_json_does_not_infer_omitted_dates(tmp_path):
    payload = tmp_path / "nse-calendar.json"
    payload.write_text(json.dumps({"records": [
        {"date": "2025-12-31", "status": "TRADING_SESSION", "label": None}
    ]}))
    manifest = _manifest(
        payload, "stock-gplm.nse-calendar.v1",
        start="2025-12-30", end="2025-12-31", coverage="PARTIAL",
    )
    imported = import_nse_calendar_v1(payload, manifest)
    assert imported.calendar.status_on(date(2025, 12, 30)) == "UNKNOWN"


def test_calendar_import_rejects_hash_mismatch_and_out_of_range(tmp_path):
    payload = tmp_path / "calendar.csv"
    payload.write_text("date,status,label\n2026-01-01,MARKET_CLOSED,Holiday\n")
    manifest = _manifest(payload, "stock-gplm.nse-calendar.v1", start="2021-01-01")
    with pytest.raises(ValueError, match="within 2022-2026"):
        import_nse_calendar_v1(payload, manifest)
    value = json.loads(manifest.read_text())
    value["effective_start"] = "2026-01-01"
    value["sha256"] = "0" * 64
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="SHA-256"):
        import_nse_calendar_v1(payload, manifest)


def test_reference_manifest_rejects_unsupported_fields(tmp_path):
    payload = tmp_path / "calendar.csv"
    payload.write_text("date,status,label\n2026-01-01,MARKET_CLOSED,Holiday\n")
    manifest = _manifest(payload, "stock-gplm.nse-calendar.v1")
    value = json.loads(manifest.read_text())
    value["unverified_note"] = "must not be silently accepted"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="missing or unsupported"):
        import_nse_calendar_v1(payload, manifest)


def _master_zip(path: Path, include_isin: bool = True) -> None:
    columns = ["Exchange", "Token", "TradingSymbol"] + (["ISIN"] if include_isin else [])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    first = {"Exchange": "NSE", "Token": "2885", "TradingSymbol": "RELIANCE-EQ"}
    second = {"Exchange": "BSE", "Token": "500325", "TradingSymbol": "RELIANCE"}
    if include_isin:
        first["ISIN"] = "INE002A01018"
        second["ISIN"] = "INE002A01018"
    writer.writerows([first, second])
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("NSE_symbols.txt", buffer.getvalue())


def test_import_shoonya_master_reads_zip_and_does_not_claim_history(tmp_path):
    payload = tmp_path / "NSE_symbols.txt.zip"
    _master_zip(payload)
    manifest = _manifest(
        payload, "stock-gplm.shoonya-instrument-master.v1",
        start="2026-01-05", end="2026-01-05", coverage="PARTIAL",
    )
    imported = import_shoonya_instrument_master_v1(payload, manifest)
    instrument = imported.inventory.get("RELIANCE-EQ")
    assert imported.source_records == 2 and imported.nse_records == 1
    assert instrument is not None
    assert instrument.shoonya_token == "2885"
    assert instrument.isin == "INE002A01018"
    assert instrument.listing_date is None
    assert instrument.membership_on(date(2026, 1, 5)) == "UNKNOWN"
    assert instrument.alias_history_status == "UNKNOWN"


def test_import_shoonya_master_leaves_absent_isin_unknown(tmp_path):
    payload = tmp_path / "NSE_symbols.txt.zip"
    _master_zip(payload, include_isin=False)
    manifest = _manifest(
        payload, "stock-gplm.shoonya-instrument-master.v1",
        start="2026-01-05", end="2026-01-05",
    )
    instrument = import_shoonya_instrument_master_v1(payload, manifest).inventory.instruments[0]
    assert instrument.isin is None


def test_historical_symbol_mapping_contract_and_overlap_rejection(tmp_path):
    payload = tmp_path / "symbol-mappings.json"
    payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "NEW-EQ", "alias_symbol": "OLD-EQ",
        "valid_from": "2022-01-01", "valid_to": "2023-12-31", "isin": None,
    }]}))
    manifest = _manifest(
        payload, "stock-gplm.historical-symbol-mappings.v1",
        start="2022-01-01", end="2026-12-31",
    )
    imported = import_historical_symbol_mappings_v1(payload, manifest)
    assert imported.records[0].alias_symbol == "OLD-EQ"
    value = json.loads(payload.read_text())
    value["records"].append({
        "canonical_symbol": "NEW-EQ", "alias_symbol": "OLDER-EQ",
        "valid_from": "2023-01-01", "valid_to": "2024-12-31", "isin": None,
    })
    payload.write_text(json.dumps(value))
    manifest = _manifest(
        payload, "stock-gplm.historical-symbol-mappings.v1",
        start="2022-01-01", end="2026-12-31",
    )
    with pytest.raises(ValueError, match="alias validity ranges overlap"):
        import_historical_symbol_mappings_v1(payload, manifest)


def test_historical_mapping_must_be_within_manifest_range(tmp_path):
    payload = tmp_path / "symbol-mappings.json"
    payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "NEW-EQ", "alias_symbol": "OLD-EQ",
        "valid_from": "2021-01-01", "valid_to": "2023-12-31", "isin": None,
    }]}))
    manifest = _manifest(
        payload, "stock-gplm.historical-symbol-mappings.v1",
        start="2022-01-01", end="2026-12-31",
    )
    with pytest.raises(ValueError, match="outside manifest range"):
        import_historical_symbol_mappings_v1(payload, manifest)


def test_historical_mappings_enrich_without_creating_membership(tmp_path):
    provenance = _provenance()
    inventory = InstrumentInventory("2026-01-05", (
        Instrument(
            "NEW-EQ", shoonya_token="1",
            field_provenance={"nse_symbol": provenance, "shoonya_token": provenance},
        ),
    ))
    payload = tmp_path / "mappings.json"
    payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "NEW-EQ", "alias_symbol": "OLD-EQ",
        "valid_from": "2022-01-01", "valid_to": "2023-12-31",
        "isin": "INE002A01018",
    }]}))
    manifest = _manifest(
        payload, "stock-gplm.historical-symbol-mappings.v1",
        start="2022-01-01", end="2026-12-31", coverage="COMPLETE",
    )
    enriched = merge_historical_symbol_mappings(
        inventory, import_historical_symbol_mappings_v1(payload, manifest)
    )
    instrument = enriched.get("NEW-EQ")
    assert instrument is not None
    assert instrument.isin == "INE002A01018"
    assert instrument.aliases[0].symbol == "OLD-EQ"
    assert instrument.alias_history_status == "COMPLETE"
    assert instrument.membership_on(date(2023, 1, 1)) == "UNKNOWN"


def test_corporate_action_contract_preserves_provenance_and_status(tmp_path):
    payload = tmp_path / "corporate-actions.json"
    payload.write_text(json.dumps({"catalogs": [{
        "symbol": "TEST-EQ", "coverage_status": "COMPLETE",
        "coverage_start": "2026-01-01", "coverage_end": "2026-01-04",
        "actions": [{
            "action_type": "SPLIT", "effective_date": "2026-01-03",
            "adjustment_status": "POLICY_REQUIRED", "details": {"ratio": "2:1"},
        }],
    }]}))
    manifest = _manifest(payload, "stock-gplm.corporate-actions.v1", coverage="COMPLETE")
    imported = import_corporate_actions_v1(payload, manifest)
    action = imported.catalogs[0].actions[0]
    assert action.adjustment_status == "POLICY_REQUIRED"
    assert action.provenance.source == "synthetic authoritative fixture"


def _provenance() -> Provenance:
    return Provenance("synthetic reference", "2026-01-05", "test://source")


def test_reference_coverage_reports_ready_and_incomplete_horizons():
    provenance = _provenance()
    complete_instrument = Instrument(
        nse_symbol="READY-EQ", isin="INE002A01018", shoonya_token="1",
        listing_date="2022-01-01", membership_verified_through="2026-12-31",
        alias_history_status="COMPLETE",
        field_provenance={
            "nse_symbol": provenance, "isin": provenance, "shoonya_token": provenance,
            "listing_date": provenance, "membership_verified_through": provenance,
            "alias_history_status": provenance,
        },
    )
    unknown_instrument = Instrument(
        nse_symbol="UNKNOWN-EQ", field_provenance={"nse_symbol": provenance}
    )
    inventory = InstrumentInventory("2026-01-05", (complete_instrument, unknown_instrument))
    calendar = NSETradingCalendar(days=(
        CalendarDay("2026-01-01", "MARKET_CLOSED", provenance),
        CalendarDay("2026-01-02", "TRADING_SESSION", provenance),
    ))
    catalog = CorporateActionCatalog(
        "READY-EQ", "COMPLETE", "2026-01-01", "2026-01-02", provenance, ()
    )
    report = build_reference_coverage_report(
        symbols=["READY-EQ", "UNKNOWN-EQ"],
        horizons=[HorizonRequest("two-day", "2026-01-01", "2026-01-02")],
        inventory=inventory,
        calendar=calendar,
        corporate_action_catalogs=[catalog],
    )
    assert report.decision == "INCOMPLETE"
    assert report.coverage[0].decision == "READY"
    assert report.coverage[1].decision == "INCOMPLETE"
    assert "LISTING_DATE_UNKNOWN" in report.coverage[1].blockers


def test_reference_cli_writes_normalized_reports_without_claiming_history(tmp_path):
    source_dir = tmp_path / "reference-source"
    source_dir.mkdir()
    calendar_payload = source_dir / "calendar.csv"
    calendar_payload.write_text("date,status,label\n2026-01-02,TRADING_SESSION,\n")
    calendar_manifest = _manifest(
        calendar_payload, "stock-gplm.nse-calendar.v1",
        start="2026-01-02", end="2026-01-02", coverage="COMPLETE",
    )
    master_payload = source_dir / "NSE_symbols.txt.zip"
    _master_zip(master_payload)
    master_manifest = _manifest(
        master_payload, "stock-gplm.shoonya-instrument-master.v1",
        start="2026-01-05", end="2026-01-05",
    )
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "symbols": ["RELIANCE-EQ"],
        "horizons": [{
            "name": "one-day", "start_date": "2026-01-02", "end_date": "2026-01-02"
        }],
    }))
    output = tmp_path / "reports"
    result = subprocess.run([
        sys.executable, "-m", "stock_gplm.m2.reference_cli",
        "--calendar-payload", str(calendar_payload),
        "--calendar-manifest", str(calendar_manifest),
        "--instrument-payload", str(master_payload),
        "--instrument-manifest", str(master_manifest),
        "--coverage-request", str(request),
        "--output-dir", str(output),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    coverage = json.loads((output / "reference-coverage.json").read_text())
    assert coverage["decision"] == "INCOMPLETE"
    assert "LISTING_DATE_UNKNOWN" in coverage["coverage"][0]["blockers"]
    assert json.loads((output / "reference-manifests.json").read_text())["sources"]
