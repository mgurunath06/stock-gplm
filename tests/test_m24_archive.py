"""Hostile-archive tests; every archive here is synthetic."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from stock_gplm.m24.archive import (
    ArchiveRejected, ArchiveTransport, archive_evidence, reconcile_archive, stage_archive,
)
from stock_gplm.m24.sync import build_plan, discover_catalog, pull_plan
from stock_gplm.m24.models import CollectorWindow
from stock_gplm.m24.transport import LocalFixtureTransport
from tests.test_m24_sync import _jsonl, _master, _publish, _request, _windows


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add(archive: tarfile.TarFile, name: str, data: bytes = b"{}", **fields) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in fields.items():
        setattr(info, key, value)
    archive.addfile(info, io.BytesIO(data))


def _tar_tree(path: Path, root: Path, extra=None, skip: frozenset[str] = frozenset()) -> Path:
    with tarfile.open(path, "w") as archive:
        archive.add(root, arcname="NSE", recursive=False)
        for item in sorted(root.rglob("*")):
            relative = item.relative_to(root).as_posix()
            if relative in skip:
                continue
            archive.add(item, arcname=f"NSE/{relative}", recursive=False)
        if extra:
            extra(archive)
    return path


def _scenario(tmp_path: Path):
    """A remote tree with one standard and one exceptional publication, discovered."""
    master = _master(tmp_path / "NSE_symbols.txt.zip", ["ALPHA-EQ", "BETA-EQ"])
    remote = tmp_path / "remote"
    remote.mkdir()
    _publish(remote, "ALPHA-EQ", _windows()[0])
    _publish(remote, "BETA-EQ", _windows()[1])
    _publish(remote, "ALPHA-EQ", CollectorWindow("exceptional", "2026-02-01", "2026-02-07"))
    destination = tmp_path / "raw"
    request = _request(tmp_path, master, destination)
    catalog = discover_catalog(
        request=request, master_path=master, transport=LocalFixtureTransport(remote),
        output_root=tmp_path / "catalogs", require_current_universe=False,
    )
    return request, remote, catalog, destination


def _stage(tmp_path: Path, request, archive: Path) -> Path:
    return stage_archive(
        archive_path=archive, expected_sha256=_sha(archive), request=request,
        staging_root=tmp_path / "staging",
    )


def _reject(tmp_path: Path, request, archive: Path, message: str) -> None:
    with pytest.raises(ArchiveRejected, match=message):
        _stage(tmp_path, request, archive)
    staging = tmp_path / "staging"
    assert not staging.exists() or not list(staging.iterdir())


NAME = "NSE/ALPHA-EQ/2026-01-01_2026-01-01.meta.json"


@pytest.mark.parametrize("name", [
    "../evil.json", "NSE/../../evil.json", "/etc/passwd", "NSE/ALPHA-EQ/../../evil.meta.json",
    "OTHER/ALPHA-EQ/2026-01-01_2026-01-01.meta.json", "NSE/./ALPHA-EQ/2026-01-01_2026-01-01.meta.json",
    "NSE\\ALPHA-EQ\\x.meta.json", "NSE/ALPHA-EQ/x\n.meta.json",
])
def test_traversal_and_foreign_paths_reject_the_whole_archive(tmp_path, name):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        _add(archive, name)
    _reject(tmp_path, request, path, "unsafe or unexpected")
    assert not (tmp_path / "evil.json").exists()


@pytest.mark.parametrize("kind", [
    tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
])
def test_links_and_special_files_are_rejected(tmp_path, kind):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo(NAME)
        info.type = kind
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    _reject(tmp_path, request, path, "not a regular file")


@pytest.mark.parametrize("names, message", [
    ([NAME, NAME], "duplicate"),
    (["NSE/ALPHA-EQ/2026-01-01_2026-01-01.meta.json", "NSE/ALPHA-EQ/./2026-01-01_2026-01-01.meta.json"], "unsafe"),
    (["NSE/ALPHA-EQ/notes.txt"], "unexpected archive file"),
    (["NSE/ALPHA-EQ/run.sh"], "unexpected archive file"),
    (["NSE/lower-eq/2026-01-01_2026-01-01.meta.json"], "unexpected archive file"),
    (["NSE/ALPHA-EQ/deep/2026-01-01_2026-01-01.meta.json"], "unexpected archive file"),
])
def test_duplicates_and_unexpected_content_are_rejected(tmp_path, names, message):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        for name in names:
            _add(archive, name)
    _reject(tmp_path, request, path, message)


def test_unexpected_directory_is_rejected(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("NSE/hidden")
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    _reject(tmp_path, request, path, "unexpected archive directory")


def test_privileged_mode_is_rejected(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        _add(archive, NAME, mode=0o644 | stat.S_ISUID)
    _reject(tmp_path, request, path, "privileged")


def test_oversized_members_and_totals_are_rejected_before_writing(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        _add(archive, NAME, b" " * (64 * 1024 + 1))
    _reject(tmp_path, request, path, "exceeds its limit")
    limited = replace(request, limits=replace(request.limits, max_total_bytes=4))
    path = tmp_path / "bad2.tar"
    with tarfile.open(path, "w") as archive:
        _add(archive, NAME, b"12345")
    _reject(tmp_path, limited, path, "approved byte limit")


def test_member_count_limit_is_enforced(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    limited = replace(request, limits=replace(request.limits, max_triplets=1))
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        for index in range(10):
            _add(archive, f"NSE/ALPHA-EQ/2026-01-{index + 10}_2026-01-{index + 10}.meta.json")
    _reject(tmp_path, limited, path, "more members")


def test_compressed_corrupt_and_empty_archives_are_rejected(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    plain = tmp_path / "plain.tar"
    with tarfile.open(plain, "w") as archive:
        _add(archive, NAME)
    compressed = tmp_path / "bomb.tar.gz"
    compressed.write_bytes(gzip.compress(plain.read_bytes()))
    _reject(tmp_path, request, compressed, "not a readable")
    corrupt = tmp_path / "corrupt.tar"
    corrupt.write_bytes(b"this is not a tar archive" * 100)
    _reject(tmp_path, request, corrupt, "not a readable")
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w"):
        pass
    _reject(tmp_path, request, empty, "no publication files|not a readable")


def test_checksum_mismatch_stops_before_reading_members(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    plain = tmp_path / "plain.tar"
    with tarfile.open(plain, "w") as archive:
        _add(archive, NAME)
    with pytest.raises(ArchiveRejected, match="does not match"):
        stage_archive(
            archive_path=plain, expected_sha256="0" * 64, request=request,
            staging_root=tmp_path / "staging",
        )
    with pytest.raises(ArchiveRejected, match="lowercase"):
        stage_archive(
            archive_path=plain, expected_sha256="ABC", request=request,
            staging_root=tmp_path / "staging",
        )


def test_staging_inside_raw_is_refused_and_a_stage_is_never_overwritten(tmp_path):
    request, remote, _, destination = _scenario(tmp_path)
    archive = _tar_tree(tmp_path / "good.tar", remote)
    with pytest.raises(ValueError, match="outside the raw"):
        stage_archive(
            archive_path=archive, expected_sha256=_sha(archive), request=request,
            staging_root=destination / "staging",
        )
    _stage(tmp_path, request, archive)
    with pytest.raises(FileExistsError):
        _stage(tmp_path, request, archive)


def test_failed_extraction_leaves_no_partial_stage(tmp_path, monkeypatch):
    request, remote, _, _ = _scenario(tmp_path)
    archive = _tar_tree(tmp_path / "good.tar", remote)
    real = hashlib.sha256
    calls = {"count": 0}

    def failing(*args):
        calls["count"] += 1
        if calls["count"] > 3:
            raise OSError("synthetic disk failure")
        return real(*args)

    monkeypatch.setattr("stock_gplm.m24.archive.hashlib.sha256", failing)
    with pytest.raises(OSError, match="synthetic"):
        _stage(tmp_path, request, archive)
    assert list((tmp_path / "staging").iterdir()) == []


def test_good_archive_stages_read_only_and_reconciles_with_exclusions(tmp_path):
    request, remote, catalog, _ = _scenario(tmp_path)
    archive = _tar_tree(tmp_path / "good.tar", remote)
    stage = _stage(tmp_path, request, archive)
    staged = [item for item in (stage / "NSE").rglob("*") if item.is_file()]
    assert len(staged) == 9
    assert all(item.stat().st_mode & 0o222 == 0 for item in staged)
    report_dir = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    report = json.loads((report_dir / "archive-reconciliation.json").read_text())
    assert report["total_triplets"] == 3
    assert report["counts"]["STANDARD"]["triplets"] == 2
    assert report["counts"]["EXCEPTIONAL_EXCLUDED"]["triplets"] == 1
    assert report["catalog_files_matched_by_sha256"] == 6
    assert report["exceptional_markers"] == ["ALPHA-EQ/2026-02-01_2026-02-07.json.gz.done"]


@pytest.mark.parametrize("change", ["missing", "extra", "payload", "marker", "exceptional_missing"])
def test_archive_differing_from_discovery_is_rejected(tmp_path, change):
    request, remote, catalog, _ = _scenario(tmp_path)
    window = _windows()[0]
    stem = f"ALPHA-EQ/{window.start_date}_{window.end_date}"
    skip: frozenset[str] = frozenset()
    if change == "missing":
        skip = frozenset({f"{stem}.meta.json"})
    elif change == "exceptional_missing":
        skip = frozenset({"ALPHA-EQ/2026-02-01_2026-02-07.meta.json"})
    elif change == "extra":
        _publish(remote, "BETA-EQ", _windows()[2])
    elif change == "payload":
        (remote / f"{stem}.json.gz").write_bytes(b"tampered")
    else:
        target = remote / f"{stem}.json.gz.done"
        target.write_bytes(target.read_bytes() + b" ")
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote, skip=skip))
    with pytest.raises(ArchiveRejected, match="differ"):
        reconcile_archive(
            stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
        )
    assert not (tmp_path / "reports").exists()


def test_staged_file_changed_after_staging_is_rejected(tmp_path):
    request, remote, catalog, _ = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    victim = stage / "NSE" / "BETA-EQ" / f"{_windows()[1].start_date}_{_windows()[1].end_date}.meta.json"
    victim.chmod(0o600)
    victim.write_bytes(b"{}")
    with pytest.raises(ArchiveRejected, match="stage manifest"):
        reconcile_archive(
            stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
        )


def _plan(request, catalog, tmp_path):
    plan = build_plan(request=request, catalog_dir=catalog, output_root=tmp_path / "plans")
    return plan, json.loads((plan / "plan-manifest.json").read_text())["plan_id"]


def test_import_publishes_only_catalog_triplets_and_records_evidence(tmp_path):
    request, remote, catalog, destination = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    report = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    plan, plan_id = _plan(request, catalog, tmp_path)
    run = pull_plan(
        request=request, plan_dir=plan, transport=ArchiveTransport(stage),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, source_evidence=archive_evidence(stage, report),
    )
    published = sorted(item.name for item in destination.rglob("*") if item.is_file() and not item.name.startswith("."))
    assert len(published) == 6 and not [name for name in published if "2026-02-01" in name]
    for item in destination.rglob("*.json.gz"):
        relative = item.relative_to(destination)
        assert item.read_bytes() == (remote / relative).read_bytes()
    manifest = json.loads((run / "run-manifest.json").read_text())
    final = manifest["final_invocation"]
    assert final["source"]["source"] == "LOCAL_ARCHIVE"
    assert final["source"]["provenance"] == "ARCHIVE_BYTES_MATCH_INDEPENDENT_DISCOVERY_CATALOG"
    assert (final["checkpointed_before_invocation"], final["transferred_in_invocation"]) == (0, 2)
    assert {row["transfer_classification"] for row in _jsonl(run / "transfer-results.jsonl")} == {"VERIFIED_TRANSFER"}


def test_import_resumes_an_interrupted_sftp_run_without_repeating_it(tmp_path, monkeypatch):
    request, remote, catalog, destination = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    report = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    plan, plan_id = _plan(request, catalog, tmp_path)

    calls = {"count": 0}

    def crash(name: str) -> None:
        if name.endswith(".done"):
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pull_plan(
            request=request, plan_dir=plan, transport=LocalFixtureTransport(remote),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id, after_publish=crash,
        )
    first = next(iter(destination.rglob("*.json.gz.done")))
    before = first.stat().st_mtime_ns
    run = pull_plan(
        request=request, plan_dir=plan, transport=ArchiveTransport(stage),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, source_evidence=archive_evidence(stage, report),
    )
    final = json.loads((run / "run-manifest.json").read_text())["final_invocation"]
    assert (final["checkpointed_before_invocation"], final["transferred_in_invocation"]) == (1, 1)
    assert first.stat().st_mtime_ns == before
    assert len(_jsonl(run / "transfer-results.jsonl")) == 2


def test_evidence_refuses_a_stage_that_differs_from_its_reconciliation(tmp_path):
    request, remote, catalog, _ = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    report = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    manifest = stage / "stage-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ArchiveRejected, match="does not match its reconciliation"):
        archive_evidence(stage, report)


@pytest.mark.parametrize("keep", [0.25, 0.5, 0.9])
def test_truncated_archive_never_yields_a_usable_stage(tmp_path, keep):
    request, remote, catalog, _ = _scenario(tmp_path)
    archive = _tar_tree(tmp_path / "full.tar", remote)
    raw = archive.read_bytes()
    cut = tmp_path / "cut.tar"
    cut.write_bytes(raw[: int(len(raw) * keep)])
    try:
        stage = _stage(tmp_path, request, cut)
    except ArchiveRejected:
        return
    # A cut on a member boundary reads as a shorter tar, and a cut in the end padding loses
    # nothing, so anything staged must either fail reconciliation or be complete.
    try:
        reconcile_archive(
            stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
        )
    except ArchiveRejected as exc:
        assert "differ" in str(exc)
        return
    for item in remote.rglob("*"):
        if item.is_file():
            assert (stage / "NSE" / item.relative_to(remote)).read_bytes() == item.read_bytes()


def test_cut_on_a_member_boundary_is_caught_by_reconciliation(tmp_path):
    request, remote, catalog, _ = _scenario(tmp_path)
    full = _tar_tree(tmp_path / "full.tar", remote).read_bytes()
    with tarfile.open(tmp_path / "full.tar") as archive:
        members = archive.getmembers()
    cut = tmp_path / "cut.tar"
    cut.write_bytes(full[: members[-1].offset])
    stage = _stage(tmp_path, request, cut)
    with pytest.raises(ArchiveRejected, match="differ"):
        reconcile_archive(
            stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
        )


def test_cut_inside_member_data_is_rejected_at_staging(tmp_path):
    request, _, _, _ = _scenario(tmp_path)
    full = tmp_path / "full.tar"
    with tarfile.open(full, "w") as archive:
        _add(archive, NAME, b"x" * 4000)
    cut = tmp_path / "cut.tar"
    cut.write_bytes(full.read_bytes()[:2048])
    _reject(tmp_path, request, cut, "not a readable|size is inconsistent")


@pytest.mark.parametrize("corruption", [b"not json", b'{"status":"IN_PROGRESS"}', b""])
def test_corrupt_exceptional_marker_impeaches_the_archive(tmp_path, corruption):
    request, remote, catalog, _ = _scenario(tmp_path)
    (remote / "ALPHA-EQ" / "2026-02-01_2026-02-07.json.gz.done").write_bytes(corruption)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    with pytest.raises(ArchiveRejected, match="M1 publication validation"):
        reconcile_archive(
            stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
        )


def test_corrupt_standard_marker_is_rejected_by_tar_only_discovery(tmp_path):
    request, remote, _, _ = _scenario(tmp_path)
    window = _windows()[0]
    (remote / "ALPHA-EQ" / f"{window.start_date}_{window.end_date}.json.gz.done").write_bytes(b"not json")
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    master = tmp_path / "NSE_symbols.txt.zip"
    catalog = discover_catalog(
        request=request, master_path=master, transport=ArchiveTransport(stage),
        output_root=tmp_path / "tar-catalog", require_current_universe=False,
    )
    assert len(_jsonl(catalog / "source-catalog.jsonl")) == 1
    assert len(_jsonl(catalog / "exceptions.jsonl")) == 2


def test_tar_discovery_reproduces_the_independent_catalog_id_and_sees_foreign_symbols(tmp_path):
    request, remote, catalog, _ = _scenario(tmp_path)
    master = tmp_path / "NSE_symbols.txt.zip"
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    again = discover_catalog(
        request=request, master_path=master, transport=ArchiveTransport(stage),
        output_root=tmp_path / "tar-catalog", require_current_universe=False,
    )
    assert again.name == catalog.name
    _publish(remote, "OUTSIDE-EQ", _windows()[0])
    other = stage_archive(
        archive_path=_tar_tree(tmp_path / "foreign.tar", remote),
        expected_sha256=_sha(tmp_path / "foreign.tar"), request=request,
        staging_root=tmp_path / "staging-2",
    )
    foreign = discover_catalog(
        request=request, master_path=master, transport=ArchiveTransport(other),
        output_root=tmp_path / "tar-catalog-2", require_current_universe=False,
    )
    assert any("pinned EQ universe" in item["reason"] for item in _jsonl(foreign / "exceptions.jsonl"))


def test_import_recovers_a_crash_between_payload_and_marker_publication(tmp_path):
    request, remote, catalog, destination = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    report = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    plan, plan_id = _plan(request, catalog, tmp_path)
    order: list[str] = []

    def crash(name: str) -> None:
        order.append(name)
        if name.endswith(".meta.json"):
            raise RuntimeError("synthetic publication crash")

    arguments = dict(
        request=request, plan_dir=plan, transport=ArchiveTransport(stage),
        checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
        execute_plan_id=plan_id, source_evidence=archive_evidence(stage, report),
    )
    with pytest.raises(RuntimeError, match="publication crash"):
        pull_plan(**arguments, after_publish=crash)
    assert order[0].endswith(".json.gz") and order[1].endswith(".meta.json")
    assert not list(destination.rglob("*.done"))
    run = pull_plan(**arguments)
    assert len(list(destination.rglob("*.done"))) == 2
    assert not list(destination.rglob(".m24-*"))
    assert (run / "run-manifest.json").exists()


def test_import_never_overwrites_a_conflicting_destination(tmp_path):
    request, remote, catalog, destination = _scenario(tmp_path)
    stage = _stage(tmp_path, request, _tar_tree(tmp_path / "t.tar", remote))
    report = reconcile_archive(
        stage_dir=stage, catalog_dir=catalog, request=request, output_root=tmp_path / "reports",
    )
    window = _windows()[0]
    symbol = destination / "ALPHA-EQ"
    symbol.mkdir(parents=True)
    conflict = symbol / f"{window.start_date}_{window.end_date}.meta.json"
    conflict.write_bytes(b"local")
    plan, plan_id = _plan(request, catalog, tmp_path)
    with pytest.raises(ValueError, match="destination conflicts"):
        pull_plan(
            request=request, plan_dir=plan, transport=ArchiveTransport(stage),
            checkpoint_root=tmp_path / "checkpoints", audit_root=tmp_path / "audit",
            execute_plan_id=plan_id, source_evidence=archive_evidence(stage, report),
        )
    assert conflict.read_bytes() == b"local"
