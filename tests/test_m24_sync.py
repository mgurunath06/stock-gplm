from __future__ import annotations

import gzip
import fcntl
import hashlib
import json
import os
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_gplm.m2 import read_shoonya_nse_eq_inventory_v1, run_inventory_v1
from stock_gplm.m24.models import CollectorWindow, SyncLimits, SyncRequestV1
from stock_gplm.m24.sync import (
    build_plan, discover_catalog, pull_plan, reconcile_run, validate_run,
)
from stock_gplm.m24.transport import (
    LocalFixtureTransport, TransportConfigV1, TransportError, validate_relative_path,
)


HEADER = "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"


def _master(path: Path, symbols: list[str]) -> Path:
    rows = "".join(
        f"NSE,{index},1,{symbol.removesuffix('-EQ')},{symbol},EQ,0.05,\n"
        for index, symbol in enumerate(symbols, 1)
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("NSE_symbols.txt", HEADER + rows)
    return path


def _windows() -> tuple[CollectorWindow, ...]:
    start = date(2026, 1, 1)
    return tuple(
        CollectorWindow(f"collector-{index + 1}", (start + timedelta(days=index)).isoformat(), (start + timedelta(days=index)).isoformat())
        for index in range(5)
    )


def _request(tmp_path: Path, master: Path, destination: Path, symbols: int = 2) -> SyncRequestV1:
    return SyncRequestV1(
        master_file=master.name,
        master_sha256=hashlib.sha256(master.read_bytes()).hexdigest(),
        destination_root=str(destination.resolve()),
        expected_eq_symbols=symbols,
        baseline_complete_symbols=1,
        baseline_partial_symbols=symbols - 1,
        collector_windows=_windows(),
        limits=SyncLimits(
            max_payload_bytes=16 * 1024 * 1024, max_sidecar_bytes=64 * 1024,
            max_triplets=20, max_total_bytes=64 * 1024 * 1024,
            minimum_free_bytes=1, chunk_bytes=64 * 1024,
            max_attempts=3, parallel_transfers=1,
        ),
    )


def _publish(root: Path, symbol: str, window: CollectorWindow) -> Path:
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    day = date.fromisoformat(window.start_date)
    timestamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    row = {
        "time": day.strftime("%d-%b-%Y").upper(),
        "into": "100.00", "inth": "110.00", "intl": "90.00", "intc": "105.00",
        "intv": "1000", "ssboe": str(timestamp),
    }
    payload = directory / f"{window.start_date}_{window.end_date}.json.gz"
    raw = gzip.compress(json.dumps([json.dumps(row)]).encode(), mtime=0)
    payload.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    metadata = payload.with_name(payload.name.removesuffix(".json.gz") + ".meta.json")
    metadata.write_text(json.dumps({
        "records": 1, "sha256_compressed": digest, "quality_status": "UNVALIDATED_RAW",
    }))
    Path(f"{payload}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": payload.name, "sha256": digest,
        "bytes": len(raw), "records": 1,
    }))
    return payload


def _setup(tmp_path: Path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA-EQ", "BETA-EQ"])
    remote = tmp_path / "remote"
    remote.mkdir()
    payload = _publish(remote, "ALPHA-EQ", _windows()[0])
    destination = tmp_path / "raw"
    request = _request(tmp_path, master, destination)
    catalog = discover_catalog(
        request=request, master_path=master, transport=LocalFixtureTransport(remote),
        output_root=tmp_path / "catalogs", require_current_universe=False,
    )
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "plans")
    plan_id = json.loads((plan / "plan-manifest.json").read_text())["plan_id"]
    return master, remote, payload, destination, request, catalog, plan, plan_id


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_request_requires_five_nonoverlapping_windows_and_reconciled_baseline(tmp_path):
    master = _master(tmp_path / "master.zip", ["ALPHA-EQ", "BETA-EQ"])
    request = _request(tmp_path, master, tmp_path / "raw")
    assert len(request.collector_windows) == 5
    with pytest.raises(ValueError, match="exactly five"):
        SyncRequestV1(
            master_file=master.name, master_sha256=request.master_sha256,
            destination_root=request.destination_root, expected_eq_symbols=2,
            baseline_complete_symbols=1, baseline_partial_symbols=1,
            collector_windows=request.collector_windows[:4], limits=request.limits,
        )
    with pytest.raises(ValueError, match="reconcile"):
        SyncRequestV1(
            master_file=master.name, master_sha256=request.master_sha256,
            destination_root=request.destination_root, expected_eq_symbols=2,
            baseline_complete_symbols=2, baseline_partial_symbols=1,
            collector_windows=request.collector_windows, limits=request.limits,
        )


def test_security_and_resource_contracts_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="transport profile"):
        TransportConfigV1(remote_root="/approved", host_alias="another-host")
    with pytest.raises(ValueError, match="unsafe"):
        TransportConfigV1(remote_root='/approved/"bad')
    with pytest.raises(TransportError, match="unsupported remote"):
        validate_relative_path("../secret")
    with pytest.raises(ValueError, match="compressed limit"):
        SyncLimits(
            max_payload_bytes=16 * 1024 * 1024 + 1, max_sidecar_bytes=64 * 1024,
            max_triplets=1, max_total_bytes=1, minimum_free_bytes=1,
            chunk_bytes=1, max_attempts=1, parallel_transfers=1,
        )


def test_discovery_accepts_complete_marker_and_rejects_bad_source(tmp_path):
    master, remote, _, _, request, catalog, _, _ = _setup(tmp_path)
    rows = _jsonl(catalog / "source-catalog.jsonl")
    assert len(rows) == 1
    assert rows[0]["source_classification"] == "VERIFIED_PUBLISHED_MARKER"
    assert rows[0]["payload"]["sha256"]
    bad = _publish(remote, "BETA-EQ", _windows()[1])
    Path(f"{bad}.done").write_text(json.dumps({
        "status": "IN_PROGRESS", "file": bad.name, "sha256": "0" * 64,
        "bytes": bad.stat().st_size, "records": 1,
    }))
    second = discover_catalog(
        request=request, master_path=master, transport=LocalFixtureTransport(remote),
        output_root=tmp_path / "catalogs-2", require_current_universe=False,
    )
    exceptions = _jsonl(second / "exceptions.jsonl")
    assert exceptions[0]["source_classification"] == "SOURCE_REJECTED"


def test_future_daily_marker_is_eligible_only_after_pinned_boundary(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA-EQ", "BETA-EQ"])
    remote = tmp_path / "remote"
    remote.mkdir()
    daily = CollectorWindow("daily", "2026-01-06", "2026-01-06")
    _publish(remote, "ALPHA-EQ", daily)
    request = replace(
        _request(tmp_path, master, tmp_path / "raw"), daily_sync_start="2026-01-06"
    )
    catalog = discover_catalog(
        request=request, master_path=master, transport=LocalFixtureTransport(remote),
        output_root=tmp_path / "catalogs", require_current_universe=False,
    )
    assert _jsonl(catalog / "source-catalog.jsonl")[0]["window"]["kind"] == "DAILY"


def test_pull_publishes_marker_last_validates_and_is_idempotent(tmp_path):
    _, remote, _, destination, request, catalog, plan, plan_id = _setup(tmp_path)
    published: list[str] = []
    run = pull_plan(
        request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, sleeper=lambda _: None,
        after_publish=published.append,
    )
    assert published[-1].endswith(".done")
    payload = next((destination / "ALPHA-EQ").glob("*.json.gz"))
    assert not os.access(payload, os.W_OK) or payload.stat().st_mode & 0o222 == 0
    result = _jsonl(run / "transfer-results.jsonl")[0]
    assert result["transfer_classification"] == "VERIFIED_TRANSFER"
    assert result["content_classification"] == "ACCEPT"
    validation = validate_run(request=request, run_dir=run, output_root=tmp_path / "validation")
    assert _jsonl(validation / "validation-results.jsonl")[0]["publication_classification"] == "ACCEPT"
    second_plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "plans-2")
    assert _jsonl(second_plan / "transfer-plan.jsonl")[0]["action"] == "SKIP_IDENTICAL"
    assert pull_plan(
        request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id,
    ) == run


def test_interrupted_payload_resumes_from_checkpoint(tmp_path):
    _, remote, _, destination, request, _, plan, plan_id = _setup(tmp_path)
    class PersistentInterruption:
        def __init__(self) -> None:
            self.delegate = LocalFixtureTransport(remote, interrupt_after_bytes=10)
            self.payload_calls = 0

        def list_markers(self, symbols):
            return self.delegate.list_markers(symbols)

        def fetch(self, relative_path, destination_path, *, resume):
            if relative_path.endswith(".json.gz"):
                self.payload_calls += 1
                if self.payload_calls > 1:
                    from stock_gplm.m24.transport import TransientTransportError
                    raise TransientTransportError("synthetic interrupted transfer")
            return self.delegate.fetch(relative_path, destination_path, resume=resume)

    with pytest.raises(TransportError, match="interrupted"):
        pull_plan(
            request=request, plan_dir=plan,
            transport=PersistentInterruption(),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id, sleeper=lambda _: None,
        )
    lock_path = destination / ".stock-gplm-m24.lock"
    assert lock_path.exists()
    with lock_path.open("r+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
    part = next((tmp_path / "checkpoints").rglob("*.json.gz"))
    assert part.stat().st_size == 10
    run = pull_plan(
        request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, sleeper=lambda _: None,
    )
    assert _jsonl(run / "transfer-results.jsonl")[0]["transfer_classification"] == "VERIFIED_TRANSFER"


def test_crash_after_payload_publication_recovers_without_overwrite(tmp_path):
    _, remote, _, destination, request, _, plan, plan_id = _setup(tmp_path)
    def crash(name: str) -> None:
        if name.endswith(".json.gz"):
            raise RuntimeError("synthetic publication crash")
    with pytest.raises(RuntimeError, match="publication crash"):
        pull_plan(
            request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id, sleeper=lambda _: None, after_publish=crash,
        )
    symbol_dir = destination / "ALPHA-EQ"
    assert len(list(symbol_dir.glob("*.json.gz"))) == 1
    assert not list(symbol_dir.glob("*.done"))
    pull_plan(
        request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, sleeper=lambda _: None,
    )
    assert len(list(symbol_dir.glob("*.done"))) == 1


def test_destination_conflict_is_never_overwritten(tmp_path):
    _, _, _, destination, request, catalog, _, _ = _setup(tmp_path)
    symbol = destination / "ALPHA-EQ"
    symbol.mkdir(parents=True)
    (symbol / f"{_windows()[0].start_date}_{_windows()[0].end_date}.json.gz").write_bytes(b"wrong")
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "conflict-plan")
    row = _jsonl(plan / "transfer-plan.jsonl")[0]
    assert row["action"] == "DESTINATION_CONFLICT"
    assert next(symbol.glob("*.json.gz")).read_bytes() == b"wrong"


@pytest.mark.parametrize("mutate", ["metadata", "payload"])
def test_source_changes_after_discovery_are_rejected(tmp_path, mutate):
    _, remote, payload, _, request, _, plan, plan_id = _setup(tmp_path)
    if mutate == "metadata":
        metadata = payload.with_name(payload.name.removesuffix(".json.gz") + ".meta.json")
        metadata.write_text(metadata.read_text() + " ")
    else:
        payload.write_bytes(payload.read_bytes() + b"changed")
    with pytest.raises(TransportError, match="changed|differs"):
        pull_plan(
            request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id, sleeper=lambda _: None,
        )


def test_reconciliation_emits_every_symbol_window_and_keeps_research_separate(tmp_path):
    master, remote, _, _, request, catalog, plan, plan_id = _setup(tmp_path)
    run = pull_plan(
        request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, sleeper=lambda _: None,
    )
    validation = validate_run(request=request, run_dir=run, output_root=tmp_path / "validation")
    output = reconcile_run(
        request=request, master_path=master, catalog_dir=catalog,
        output_root=tmp_path / "reconciliation", require_current_universe=False,
        run_dir=run, validation_dir=validation,
    )
    rows = _jsonl(output / "symbol-window-reconciliation.jsonl")
    assert len(rows) == 2 * 5
    assert sum(item["source_classification"] == "VERIFIED_PUBLISHED" for item in rows) == 1
    published = next(item for item in rows if item["source_classification"] == "VERIFIED_PUBLISHED")
    assert published["transfer_classification"] == "VERIFIED_TRANSFER"
    assert published["content_classification"] == "ACCEPT"
    assert {item["historical_classification"] for item in rows} == {"NOT_EVALUATED"}
    assert {item["training_readiness"] for item in rows} == {"NOT_READY"}


def test_tampered_plan_and_audit_are_rejected(tmp_path):
    _, remote, _, _, request, _, plan, plan_id = _setup(tmp_path)
    plan_file = plan / "transfer-plan.jsonl"
    plan_file.write_text(plan_file.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        pull_plan(
            request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id,
        )


def test_m23_refuses_active_m24_publication_lock(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA-EQ"])
    raw = tmp_path / "raw"
    raw.mkdir()
    lock_stream = (raw / ".stock-gplm-m24.lock").open("w", encoding="utf-8")
    lock_stream.write("synthetic-run\n")
    lock_stream.flush()
    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    request_path = tmp_path / "m23-request.json"
    request_path.write_text(json.dumps({
        "schema_version": 1,
        "horizons": [{"name": "one", "start_date": "2026-01-01", "end_date": "2026-01-01"}],
    }))
    try:
        with pytest.raises(RuntimeError, match="active M2.4"):
            run_inventory_v1(
                master_path=master, master_sha256=hashlib.sha256(master.read_bytes()).hexdigest(),
                eq_inventory=read_shoonya_nse_eq_inventory_v1(master), raw_root=raw,
                request_path=request_path, output_root=tmp_path / "reports", expected_eq_count=1,
            )
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
