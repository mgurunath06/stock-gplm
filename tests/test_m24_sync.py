from __future__ import annotations

import gzip
import fcntl
import hashlib
import json
import os
import shutil
import sys
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_gplm.m2 import read_shoonya_nse_eq_inventory_v1, run_inventory_v1
from stock_gplm.m24 import sync as stock_sync
from stock_gplm.m24.models import CollectorWindow, SyncLimits, SyncRequestV1
from stock_gplm.m24.sync import (
    build_plan, discover_catalog, pull_plan, reconcile_run, validate_run,
)
from stock_gplm.m24.transport import (
    MAX_BATCH_FETCH_FILES, LocalFixtureTransport, TransientTransportError,
    TransportConfigV1, TransportError,
    WindowsOpenSSHTransport, _run_bounded, validate_relative_path,
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


def test_windows_ssh_marker_find_is_fixed_and_root_relative(monkeypatch):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved/root/"))
    commands = []

    def run(value, **_kwargs):
        commands.append(value)
        return 0, (
            "/approved/root/RELIANCE-EQ/2026-01-01_2026-01-02.json.gz.done\n"
            "/approved/root/TCS-EQ/2026-01-01_2026-01-02.json.gz.done\n"
        ), ""

    monkeypatch.setattr("stock_gplm.m24.transport._run_bounded", run)
    markers = transport.list_markers(("RELIANCE-EQ", "TCS-EQ"))
    assert commands[0][-11:] == [
        "find", "/approved/root", "-mindepth", "2", "-maxdepth", "2",
        "-type", "f", "-name", "'*.json.gz.done'", "-print",
    ]
    assert markers == (
        "RELIANCE-EQ/2026-01-01_2026-01-02.json.gz.done",
        "TCS-EQ/2026-01-01_2026-01-02.json.gz.done",
    )


@pytest.mark.parametrize(
    "output, message",
    (
        ("", "zero publications"),
        ("/other/ALPHA-EQ/2026-01-01_2026-01-01.json.gz.done\n", "outside remote_root"),
        ("/approved/not-a-publication\n", "unsupported path"),
    ),
)
def test_windows_sftp_marker_enumeration_fails_closed(monkeypatch, output, message):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    monkeypatch.setattr(
        "stock_gplm.m24.transport._run_bounded", lambda *_args, **_kwargs: (0, output, ""),
    )
    with pytest.raises(TransportError, match=message):
        transport.list_markers(("ALPHA-EQ",))


def test_windows_sftp_zero_exit_stderr_is_not_swallowed(monkeypatch):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    batch_text = []

    def windows_path(path):
        batch_text.append(path.read_text(encoding="utf-8"))
        return "C:/safe/batch"

    monkeypatch.setattr(transport, "_windows_path", windows_path)
    monkeypatch.setattr(
        "stock_gplm.m24.transport._run_bounded",
        lambda *_args, **_kwargs: (0, "sftp> -ls missing\n", "remote readdir: No such file\n"),
    )
    with pytest.raises(TransportError, match="despite a zero exit status"):
        transport._run_batch(["-ls missing"])
    assert batch_text == ["-ls missing\n"]


def test_discovery_rejects_an_empty_marker_set(tmp_path):
    class EmptyTransport:
        def list_markers(self, _symbols):
            return ()

        def fetch(self, _relative_path, _destination, *, resume):
            raise AssertionError(f"fetch must not run; resume={resume}")

    master = _master(tmp_path / "master.zip", ["ALPHA-EQ", "BETA-EQ"])
    with pytest.raises(TransportError, match="zero publications"):
        discover_catalog(
            request=_request(tmp_path, master, tmp_path / "raw"),
            master_path=master, transport=EmptyTransport(),
            output_root=tmp_path / "reports", require_current_universe=False,
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

        def list_files(self):
            return self.delegate.list_files()

        def fetch(self, relative_path, destination_path, *, resume):
            if relative_path.endswith(".json.gz"):
                self.payload_calls += 1
                if self.payload_calls > 1:
                    raise TransientTransportError("synthetic interrupted transfer")
            return self.delegate.fetch(relative_path, destination_path, resume=resume)

        def fetch_many(self, requests, *, resume_payloads=False):
            for relative_path, destination_path in requests:
                self.fetch(
                    relative_path, destination_path,
                    resume=resume_payloads and relative_path.endswith(".json.gz"),
                )

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
        # Size and content tampering are both stopped before publication.
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


class _SpyTransport(LocalFixtureTransport):
    """Fixture that records and can fail batches, like one SFTP session each."""

    def __init__(self, root: Path, *, failures: list[Exception] | None = None) -> None:
        super().__init__(root)
        self.failures = failures or []
        self.batches: list[tuple[tuple[str, Path], ...]] = []

    def fetch_many(self, requests, *, resume_payloads=False):
        self.batches.append(requests)
        if self.failures:
            raise self.failures.pop(0)
        super().fetch_many(requests, resume_payloads=resume_payloads)


def _pull(tmp_path, request, plan, plan_id, transport, sleeps=None):
    return pull_plan(
        request=request, plan_dir=plan, transport=transport,
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id,
        sleeper=(lambda value: sleeps.append(value)) if sleeps is not None else (lambda _: None),
    )


def _script(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def test_bounded_run_kills_hung_child_and_reports_transient():
    with pytest.raises(TransientTransportError, match="time bound"):
        _run_bounded(_script("import time; time.sleep(60)"), timeout=0.2, max_bytes=1024)


def test_bounded_run_stops_a_child_exceeding_its_output_bound():
    with pytest.raises(TransportError, match="output bound") as raised:
        _run_bounded(
            _script("import sys\nwhile True: sys.stdout.write('x' * 65536)"),
            timeout=30, max_bytes=1024,
        )
    assert not isinstance(raised.value, TransientTransportError)


def test_bounded_run_returns_status_and_both_streams():
    assert _run_bounded(
        _script("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"),
        timeout=30, max_bytes=1024,
    ) == (3, "out\n", "err\n")


def test_windows_list_files_is_read_only_and_ignores_unfetchable_names(monkeypatch):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    commands = []

    def run(value, **_kwargs):
        commands.append(value)
        return 0, (
            "12 ALPHA-EQ/2026-01-01_2026-01-02.json.gz\r\n"
            "9 ALPHA-EQ/.tmp-upload\n"
            "9 ../outside/2026-01-01_2026-01-02.json.gz\n"
        ), ""

    monkeypatch.setattr("stock_gplm.m24.transport._run_bounded", run)
    assert transport.list_files() == {"ALPHA-EQ/2026-01-01_2026-01-02.json.gz": 12}
    assert commands[0][-10:] == [
        "find", "/approved", "-mindepth", "2", "-maxdepth", "2", "-type", "f",
        "-printf", "'%s %P\\n'",
    ]
    assert not {"-delete", "-exec", "-execdir", "-fprint", "-fprintf"} & set(commands[0])


@pytest.mark.parametrize(
    "result, error, message",
    (
        ((1, "", "denied"), TransientTransportError, "enumeration failed"),
        ((0, "", "Host key verification failed"), TransportError, "reported an error"),
        ((0, "", ""), TransportError, "zero publications"),
        ((0, "not-a-size ALPHA-EQ/x\n", ""), TransportError, "malformed"),
    ),
)
def test_windows_list_files_fails_closed(monkeypatch, result, error, message):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    monkeypatch.setattr("stock_gplm.m24.transport._run_bounded", lambda *_a, **_k: result)
    with pytest.raises(error, match=message):
        transport.list_files()


def test_windows_batch_uses_only_read_verbs_and_detects_omitted_files(monkeypatch, tmp_path):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    batches: list[list[str]] = []
    monkeypatch.setattr(transport, "_windows_path", lambda path: str(path))
    monkeypatch.setattr(transport, "_run_batch", lambda commands: batches.append(commands) or "")
    resumed = tmp_path / "2026-01-01_2026-01-02.json.gz"
    resumed.write_bytes(b"partial")
    with pytest.raises(TransportError, match="omitted"):
        transport.fetch_many((
            ("ALPHA-EQ/2026-01-01_2026-01-02.json.gz", resumed),
            ("ALPHA-EQ/2026-01-01_2026-01-02.meta.json", tmp_path / "absent.meta"),
        ), resume_payloads=True)
    verbs = [command.split()[0] for command in batches[0]]
    assert verbs == ["reget", "get"]


def test_windows_batch_rejects_path_injection_before_any_session(monkeypatch, tmp_path):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    monkeypatch.setattr(
        transport, "_run_batch",
        lambda _commands: pytest.fail("no SFTP session may start for an unsafe path"),
    )
    with pytest.raises(TransportError, match="unsupported remote"):
        transport.fetch_many((('ALPHA-EQ/x"\nrm "/approved', tmp_path / "x"),))


def test_windows_batch_splits_at_the_session_bound(monkeypatch, tmp_path):
    transport = WindowsOpenSSHTransport(TransportConfigV1(remote_root="/approved"))
    sizes: list[int] = []
    monkeypatch.setattr(transport, "_windows_path", lambda path: str(path))
    monkeypatch.setattr(transport, "_run_batch", lambda commands: sizes.append(len(commands)) or "")
    target = tmp_path / "file"
    target.write_bytes(b"x")
    transport.fetch_many(
        (("ALPHA-EQ/2026-01-01_2026-01-02.meta.json", target),) * (MAX_BATCH_FETCH_FILES + 1)
    )
    assert sizes == [MAX_BATCH_FETCH_FILES, 1]


def _discover(tmp_path, master, remote, name="catalogs", request=None, **kwargs):
    return discover_catalog(
        request=request or _request(tmp_path, master, tmp_path / "raw"),
        master_path=master, transport=kwargs.pop("transport", LocalFixtureTransport(remote)),
        output_root=tmp_path / name, require_current_universe=False, sleeper=lambda _: None,
        **kwargs,
    )


def _source(tmp_path):
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA-EQ", "BETA-EQ"])
    remote = tmp_path / "remote"
    remote.mkdir()
    return master, remote, _publish(remote, "ALPHA-EQ", _windows()[0])


def _meta(payload: Path) -> Path:
    return payload.with_name(payload.name.removesuffix(".json.gz") + ".meta.json")


@pytest.mark.parametrize("attack", ["missing", "symlink", "oversized", "payload_missing", "payload_short"])
def test_discovery_rejects_bad_triplets_without_fetching_them(tmp_path, attack):
    master, remote, payload = _source(tmp_path)
    metadata = _meta(payload)
    if attack == "missing":
        metadata.unlink()
    elif attack == "symlink":
        secret = tmp_path / "secret.json"
        secret.write_text("{}")
        metadata.unlink()
        metadata.symlink_to(secret)
    elif attack == "oversized":
        metadata.write_bytes(b" " * (64 * 1024 + 1))
    elif attack == "payload_missing":
        payload.unlink()
    else:
        payload.write_bytes(payload.read_bytes()[:-1])
    spy = _SpyTransport(remote)
    catalog = _discover(tmp_path, master, remote, transport=spy)
    assert _jsonl(catalog / "source-catalog.jsonl") == []
    assert _jsonl(catalog / "exceptions.jsonl")[0]["source_classification"] == "SOURCE_REJECTED"
    if attack != "payload_short":
        assert all(request == () for request in spy.batches)


def test_exceptional_windows_are_excluded_and_counted_not_dropped(tmp_path):
    master, remote, _ = _source(tmp_path)
    _publish(remote, "ALPHA-EQ", CollectorWindow("exceptional", "2026-02-01", "2026-02-07"))
    catalog = _discover(tmp_path, master, remote)
    manifest = json.loads((catalog / "catalog-manifest.json").read_text())
    assert (manifest["triplets"], manifest["rejected"]) == (1, 1)
    exception = _jsonl(catalog / "exceptions.jsonl")[0]
    assert exception["marker"] == "ALPHA-EQ/2026-02-01_2026-02-07.json.gz.done"
    assert "pinned collector windows" in exception["reason"]


def test_marker_outside_the_pinned_universe_is_rejected(tmp_path):
    master, remote, _ = _source(tmp_path)
    _publish(remote, "OUTSIDE-EQ", _windows()[0])

    class RootWideTransport(LocalFixtureTransport):
        def list_markers(self, symbols):
            return super().list_markers((*symbols, "OUTSIDE-EQ"))

    catalog = _discover(tmp_path, master, remote, transport=RootWideTransport(remote))
    assert "pinned EQ universe" in _jsonl(catalog / "exceptions.jsonl")[0]["reason"]


def test_byte_cap_breach_stops_discovery_instead_of_rejecting_rows(tmp_path):
    master, remote, _ = _source(tmp_path)
    base = _request(tmp_path, master, tmp_path / "raw")
    limited = replace(base, limits=replace(base.limits, max_total_bytes=1))
    with pytest.raises(RuntimeError, match="approved run limit"):
        _discover(tmp_path, master, remote, request=limited)
    assert not (tmp_path / "catalogs").exists()


def test_discovery_retries_transient_batches_then_stops(tmp_path):
    master, remote, _ = _source(tmp_path)
    recovering = _SpyTransport(remote, failures=[TransientTransportError("drop")])
    assert _jsonl(_discover(tmp_path, master, remote, transport=recovering) / "source-catalog.jsonl")
    assert len(recovering.batches) == 2
    failing = _SpyTransport(remote, failures=[TransientTransportError("drop")] * 3)
    with pytest.raises(TransientTransportError):
        _discover(tmp_path, master, remote, name="catalogs-2", transport=failing)
    assert len(failing.batches) == 3


def test_permanent_batch_failure_is_not_retried(tmp_path):
    _, remote, _, _, request, _, plan, plan_id = _setup(tmp_path)
    spy = _SpyTransport(remote, failures=[TransportError("host key changed")])
    with pytest.raises(TransportError, match="host key"):
        _pull(tmp_path, request, plan, plan_id, spy)
    assert len(spy.batches) == 1


def test_pull_uses_one_bounded_session_per_batch(tmp_path, monkeypatch):
    master, remote, _ = _source(tmp_path)
    _publish(remote, "ALPHA-EQ", _windows()[1])
    _publish(remote, "BETA-EQ", _windows()[0])
    request = _request(tmp_path, master, tmp_path / "raw")
    catalog = _discover(tmp_path, master, remote, request=request)
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "plans")
    plan_id = json.loads((plan / "plan-manifest.json").read_text())["plan_id"]
    monkeypatch.setattr("stock_gplm.m24.sync.PULL_BATCH_TRIPLETS", 2)
    spy = _SpyTransport(remote)
    run = _pull(tmp_path, request, plan, plan_id, spy)
    assert [len(batch) for batch in spy.batches] == [10, 5]
    assert all(len(batch) <= MAX_BATCH_FETCH_FILES for batch in spy.batches)
    first = [relative for relative, _ in spy.batches[0][:5]]
    assert first[0] == first[3] and first[1] == first[4] and first[2].endswith(".json.gz")
    assert len(_jsonl(run / "transfer-results.jsonl")) == 3


def test_pull_refuses_a_plan_with_conflicts_before_any_session(tmp_path):
    _, remote, _, destination, request, catalog, _, _ = _setup(tmp_path)
    symbol = destination / "ALPHA-EQ"
    symbol.mkdir(parents=True)
    conflict = symbol / f"{_windows()[0].start_date}_{_windows()[0].end_date}.json.gz"
    conflict.write_bytes(b"wrong")
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "conflict-plan")
    plan_id = json.loads((plan / "plan-manifest.json").read_text())["plan_id"]
    spy = _SpyTransport(remote)
    with pytest.raises(ValueError, match="destination conflicts"):
        _pull(tmp_path, request, plan, plan_id, spy)
    assert spy.batches == [] and conflict.read_bytes() == b"wrong"


def test_destination_appearing_after_planning_is_never_overwritten(tmp_path):
    _, remote, _, destination, request, _, plan, plan_id = _setup(tmp_path)
    symbol = destination / "ALPHA-EQ"
    symbol.mkdir(parents=True)
    raced = symbol / f"{_windows()[0].start_date}_{_windows()[0].end_date}.json.gz"
    raced.write_bytes(b"raced")
    with pytest.raises(FileExistsError, match="immutable destination conflict"):
        _pull(tmp_path, request, plan, plan_id, _SpyTransport(remote))
    assert raced.read_bytes() == b"raced"
    assert not list(symbol.glob("*.done"))


@pytest.mark.parametrize("attack", ["size", "same_size_marker", "symlink", "missing"])
def test_pull_rejects_source_tampering_and_publishes_nothing(tmp_path, attack):
    _, remote, payload, destination, request, _, plan, plan_id = _setup(tmp_path)
    marker = Path(f"{payload}.done")
    if attack == "size":
        payload.write_bytes(payload.read_bytes() + b"x")
    elif attack == "same_size_marker":
        content = marker.read_bytes()
        marker.write_bytes(content.replace(b"COMPLETE", b"COMPLETF"))
    elif attack == "symlink":
        moved = tmp_path / "moved.json"
        _meta(payload).rename(moved)
        _meta(payload).symlink_to(moved)
    else:
        marker.unlink()
    with pytest.raises(TransportError, match="changed"):
        _pull(tmp_path, request, plan, plan_id, _SpyTransport(remote))
    assert not list(destination.rglob("*.json.gz*"))


def test_source_change_between_payload_and_recheck_is_rejected(tmp_path):
    _, remote, payload, destination, request, _, plan, plan_id = _setup(tmp_path)

    class ChangingTransport(_SpyTransport):
        def fetch(self, relative_path, destination_path, *, resume):
            size = super().fetch(relative_path, destination_path, resume=resume)
            if relative_path.endswith(".json.gz"):
                _meta(payload).write_bytes(b" " * _meta(payload).stat().st_size)
            return size

    with pytest.raises(TransportError, match="changed during transfer"):
        _pull(tmp_path, request, plan, plan_id, ChangingTransport(remote))
    assert not list(destination.rglob("*.json.gz*"))


def test_corrupt_publication_passing_hashes_is_not_published(tmp_path):
    master, remote, payload = _source(tmp_path)
    garbage = b"not gzip at all"
    payload.write_bytes(garbage)
    digest = hashlib.sha256(garbage).hexdigest()
    _meta(payload).write_text(json.dumps({"records": 1, "sha256_compressed": digest}))
    Path(f"{payload}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": payload.name, "sha256": digest,
        "bytes": len(garbage), "records": 1,
    }))
    request = _request(tmp_path, master, tmp_path / "raw")
    catalog = _discover(tmp_path, master, remote, request=request)
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "plans")
    plan_id = json.loads((plan / "plan-manifest.json").read_text())["plan_id"]
    with pytest.raises(ValueError, match="M1 publication validation"):
        _pull(tmp_path, request, plan, plan_id, _SpyTransport(remote))
    assert not list((tmp_path / "raw").rglob("*.json.gz*"))


def test_resumed_run_reverifies_and_keeps_transfer_classification(tmp_path):
    _, remote, _, destination, request, _, plan, plan_id = _setup(tmp_path)
    original = stock_sync._write_jsonl
    calls = {"count": 0}

    def crash(path, rows):
        calls["count"] += 1
        raise RuntimeError("synthetic crash before audit")

    stock_sync._write_jsonl = crash
    try:
        with pytest.raises(RuntimeError, match="before audit"):
            _pull(tmp_path, request, plan, plan_id, _SpyTransport(remote))
    finally:
        stock_sync._write_jsonl = original
    assert calls["count"] == 1
    resumed = _SpyTransport(remote)
    published = next((destination / "ALPHA-EQ").glob("*.json.gz"))
    published.chmod(0o644)
    good = published.read_bytes()
    published.write_bytes(good + b"tampered")
    with pytest.raises(TransportError, match="no longer matches"):
        _pull(tmp_path, request, plan, plan_id, resumed)
    assert not (tmp_path / "audit" / f"run-{plan_id}").exists()
    published.write_bytes(good)
    run = _pull(tmp_path, request, plan, plan_id, resumed)
    assert resumed.batches == []
    row = _jsonl(run / "transfer-results.jsonl")[0]
    assert row["transfer_classification"] == "VERIFIED_TRANSFER"
    assert row["content_classification"] == "NOT_EVALUATED"


def test_pull_reserves_staged_and_published_copies_on_one_filesystem(tmp_path, monkeypatch):
    _, remote, _, _, request, _, plan, plan_id = _setup(tmp_path)
    manifest_bytes = json.loads((plan / "plan-manifest.json").read_text())["transfer_bytes"]
    usage = shutil.disk_usage(tmp_path)
    limits = replace(request.limits, minimum_free_bytes=usage.free - manifest_bytes - 1024)
    monkeypatch.setattr(
        "stock_gplm.m24.sync.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=limits.minimum_free_bytes + manifest_bytes + 1),
    )
    limited = replace(request, limits=limits)
    plan_limited = build_plan(request=limited, catalog_dir=_rebind(tmp_path, limited), output_root=tmp_path / "limited")
    plan_id = json.loads((plan_limited / "plan-manifest.json").read_text())["plan_id"]
    spy = _SpyTransport(remote)
    with pytest.raises(ValueError, match="disk space"):
        _pull(tmp_path, limited, plan_limited, plan_id, spy)
    assert spy.batches == []


def _rebind(tmp_path, request):
    master = tmp_path / "NSE_symbols.txt.zip"
    return _discover(tmp_path, master, tmp_path / "remote", name="rebound", request=request)
