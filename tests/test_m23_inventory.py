from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from stock_gplm.m2 import (
    import_corporate_actions_v1,
    import_historical_symbol_mappings_v1,
    import_listing_history_v1,
    import_nse_calendar_v1,
    import_provider_unavailability_v1,
    read_shoonya_nse_eq_inventory_v1,
    run_inventory_v1,
)


HEADER = "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"


def _master(path: Path, symbols: list[str]) -> Path:
    rows = "".join(
        f"NSE,{index},1,{symbol},{symbol}-EQ,EQ,0.05,\n"
        for index, symbol in enumerate(symbols, 1)
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("NSE_symbols.txt", HEADER + rows)
    return path


def _publish(root: Path, symbol: str, start: date, end: date, observed: date) -> Path:
    directory = root / f"{symbol}-EQ"
    directory.mkdir(parents=True)
    path = directory / f"{start.isoformat()}_{end.isoformat()}.json.gz"
    timestamp = int(datetime(observed.year, observed.month, observed.day, tzinfo=timezone.utc).timestamp())
    row = {
        "time": observed.strftime("%d-%b-%Y").upper(),
        "into": "100.00", "inth": "110.00", "intl": "90.00", "intc": "105.00",
        "intv": "1000", "ssboe": str(timestamp),
    }
    raw = gzip.compress(json.dumps([json.dumps(row)]).encode(), mtime=0)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_name(path.name.removesuffix(".json.gz") + ".meta.json").write_text(json.dumps({
        "records": 1, "sha256_compressed": digest, "quality_status": "UNVALIDATED_RAW",
    }))
    Path(f"{path}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": path.name, "sha256": digest,
        "bytes": len(raw), "records": 1,
    }))
    return path


def _manifest(payload: Path, contract: str, start: str, end: str, coverage: str = "COMPLETE") -> Path:
    manifest = payload.with_name(payload.name + ".manifest.json")
    manifest.write_text(json.dumps({
        "schema_version": 1, "contract": contract, "payload_file": payload.name,
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "source": "synthetic authoritative fixture", "source_reference": "test://m23",
        "publication_date": "2026-01-05", "acquired_at_utc": "2026-01-05T12:00:00Z",
        "effective_start": start, "effective_end": end, "coverage_status": coverage,
    }))
    return manifest


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_listing_history_contract_preserves_authoritative_bounds(tmp_path):
    payload = tmp_path / "listing.json"
    payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "ALPHA-EQ", "listing_date": "2022-01-03",
        "delisting_date": None, "membership_verified_through": "2026-01-02",
    }]}))
    imported = import_listing_history_v1(
        payload, _manifest(payload, "stock-gplm.listing-history.v1", "2022-01-01", "2026-01-04")
    )
    assert imported.records[0].listing_date == "2022-01-03"
    assert imported.records[0].provenance.reference == "test://m23"


def test_listing_history_rejects_dates_outside_manifest(tmp_path):
    payload = tmp_path / "listing.json"
    payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "ALPHA-EQ", "listing_date": "2021-12-31",
        "delisting_date": None, "membership_verified_through": None,
    }]}))
    manifest = _manifest(payload, "stock-gplm.listing-history.v1", "2022-01-01", "2026-01-04")
    with pytest.raises(ValueError, match="outside manifest range"):
        import_listing_history_v1(payload, manifest)


def test_provider_unavailability_requires_checksum_bound_contract(tmp_path):
    payload = tmp_path / "outages.json"
    payload.write_text(json.dumps({"outages": [{
        "start_date": "2026-01-02", "end_date": "2026-01-02", "reason": "published outage",
    }]}))
    manifest = _manifest(
        payload, "stock-gplm.provider-unavailability.v1", "2026-01-01", "2026-01-04"
    )
    imported = import_provider_unavailability_v1(payload, manifest)
    assert imported.outages[0].provenance.source == "synthetic authoritative fixture"
    payload.write_text(json.dumps({"outages": []}))
    with pytest.raises(ValueError, match="SHA-256"):
        import_provider_unavailability_v1(payload, manifest)


def test_inventory_reconciles_every_symbol_streams_nonoverlapping_intervals_and_is_deterministic(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA", "BETA", "GAMMA"])
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    eq = read_shoonya_nse_eq_inventory_v1(master)
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    _publish(raw_root, "ALPHA", date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 1))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "two-days", "start_date": "2026-01-01", "end_date": "2026-01-02"}],
    }))
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raw_root.rglob("*") if path.is_file()}
    first = run_inventory_v1(
        master_path=master, master_sha256=master_sha, eq_inventory=eq,
        raw_root=raw_root, request_path=request, output_root=tmp_path / "reports-a",
        expected_eq_count=3,
    )
    second = run_inventory_v1(
        master_path=master, master_sha256=master_sha, eq_inventory=eq,
        raw_root=raw_root, request_path=request, output_root=tmp_path / "reports-b",
        expected_eq_count=3,
    )
    assert first.name == second.name
    for name in (
        "segments.jsonl", "coverage-intervals.jsonl", "symbol-coverage.jsonl",
        "exceptions.jsonl", "inventory-summary.json", "run-manifest.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    summary = json.loads((first / "inventory-summary.json").read_text())
    assert summary["universe"] == {
        "expected_eq_symbols": 3, "reconciled_eq_symbols": 3,
        "zero_artifact_symbols": 2, "master_sha256": master_sha,
    }
    symbol_rows = _lines(first / "symbol-coverage.jsonl")
    assert [item["symbol"] for item in symbol_rows] == ["ALPHA-EQ", "BETA-EQ", "GAMMA-EQ"]
    assert symbol_rows[1]["artifact_count"] == 0
    intervals = _lines(first / "coverage-intervals.jsonl")
    for symbol in ("ALPHA-EQ", "BETA-EQ", "GAMMA-EQ"):
        rows = [item for item in intervals if item["symbol"] == symbol]
        assert rows[0]["start_date"] == "2026-01-01"
        assert rows[-1]["end_date"] == "2026-01-02"
        for left, right in zip(rows, rows[1:]):
            assert date.fromisoformat(left["end_date"]) < date.fromisoformat(right["start_date"])
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in raw_root.rglob("*") if path.is_file()}
    assert after == before
    with pytest.raises(FileExistsError, match="already exists"):
        run_inventory_v1(
            master_path=master, master_sha256=master_sha, eq_inventory=eq,
            raw_root=raw_root, request_path=request, output_root=tmp_path / "reports-a",
            expected_eq_count=3,
        )


def test_inventory_refuses_unreconciled_universe_and_cli_requires_explicit_count(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA", "BETA"])
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "one", "start_date": "2026-01-01", "end_date": "2026-01-01"}],
    }))
    result = subprocess.run([
        sys.executable, "-m", "stock_gplm.m2.inventory_cli",
        "--master", str(master), "--raw-root", str(raw_root),
        "--request", str(request), "--output-root", str(tmp_path / "reports"),
        "--expected-eq-count", "2715",
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 3
    assert "does not equal explicit expected count 2715" in result.stderr
    assert not (tmp_path / "reports").exists()


def test_synthetic_2715_symbol_zero_artifact_reconciliation(tmp_path):
    symbols = [f"S{index:04d}" for index in range(2715)]
    master = _master(tmp_path / "NSE_symbols.txt.zip", symbols)
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "one-day", "start_date": "2026-01-02", "end_date": "2026-01-02"}],
    }))
    run_dir = run_inventory_v1(
        master_path=master, master_sha256=master_sha,
        eq_inventory=read_shoonya_nse_eq_inventory_v1(master),
        raw_root=raw_root, request_path=request, output_root=tmp_path / "reports",
        expected_eq_count=2715,
    )
    summary = json.loads((run_dir / "inventory-summary.json").read_text())
    assert summary["universe"]["reconciled_eq_symbols"] == 2715
    assert summary["universe"]["zero_artifact_symbols"] == 2715
    assert len(_lines(run_dir / "symbol-coverage.jsonl")) == 2715
    assert len(_lines(run_dir / "coverage-intervals.jsonl")) == 2715


def test_provider_unavailable_interval_uses_imported_authoritative_evidence(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA"])
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    _publish(raw_root, "ALPHA", date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 1))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "two-days", "start_date": "2026-01-01", "end_date": "2026-01-02"}],
    }))
    calendar_payload = tmp_path / "calendar.csv"
    calendar_payload.write_text(
        "date,status,label\n2026-01-01,TRADING_SESSION,\n2026-01-02,TRADING_SESSION,\n"
    )
    calendar_manifest = _manifest(
        calendar_payload, "stock-gplm.nse-calendar.v1", "2026-01-01", "2026-01-02"
    )
    listing_payload = tmp_path / "listing.json"
    listing_payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "ALPHA-EQ", "listing_date": "2026-01-01",
        "delisting_date": None, "membership_verified_through": "2026-01-02",
    }]}))
    listing_manifest = _manifest(
        listing_payload, "stock-gplm.listing-history.v1", "2026-01-01", "2026-01-02"
    )
    mapping_payload = tmp_path / "mapping.json"
    mapping_payload.write_text(json.dumps({"records": [{
        "canonical_symbol": "ALPHA-EQ", "alias_symbol": "ALPHA-EQ",
        "valid_from": "2026-01-01", "valid_to": "2026-01-02", "isin": None,
    }]}))
    mapping_manifest = _manifest(
        mapping_payload, "stock-gplm.historical-symbol-mappings.v1", "2026-01-01", "2026-01-02"
    )
    actions_payload = tmp_path / "actions.json"
    actions_payload.write_text(json.dumps({"catalogs": [{
        "symbol": "ALPHA-EQ", "coverage_status": "COMPLETE",
        "coverage_start": "2026-01-01", "coverage_end": "2026-01-02", "actions": [],
    }]}))
    actions_manifest = _manifest(
        actions_payload, "stock-gplm.corporate-actions.v1", "2026-01-01", "2026-01-02"
    )
    outage_payload = tmp_path / "outages.json"
    outage_payload.write_text(json.dumps({"outages": [{
        "start_date": "2026-01-02", "end_date": "2026-01-02", "reason": "published outage",
    }]}))
    outage_manifest = _manifest(
        outage_payload, "stock-gplm.provider-unavailability.v1", "2026-01-01", "2026-01-02"
    )
    run_dir = run_inventory_v1(
        master_path=master, master_sha256=master_sha,
        eq_inventory=read_shoonya_nse_eq_inventory_v1(master),
        raw_root=raw_root, request_path=request, output_root=tmp_path / "reports",
        expected_eq_count=1,
        calendar=import_nse_calendar_v1(calendar_payload, calendar_manifest).calendar,
        listing_history=import_listing_history_v1(listing_payload, listing_manifest),
        mappings=import_historical_symbol_mappings_v1(mapping_payload, mapping_manifest),
        actions=import_corporate_actions_v1(actions_payload, actions_manifest),
        unavailability=import_provider_unavailability_v1(outage_payload, outage_manifest),
        reference_files=(
            calendar_payload, calendar_manifest, listing_payload, listing_manifest,
            mapping_payload, mapping_manifest, actions_payload, actions_manifest,
            outage_payload, outage_manifest,
        ),
    )
    outage_interval = next(
        item for item in _lines(run_dir / "coverage-intervals.jsonl")
        if item["start_date"] == "2026-01-02"
    )
    assert outage_interval["historical_status"] == "PROVIDER_UNAVAILABLE"
    assert outage_interval["blockers"] == ["AUTHORITATIVE_PROVIDER_OUTAGE"]
