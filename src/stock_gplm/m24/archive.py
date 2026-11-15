"""Audited import of a locally supplied M2.4 publication archive.

The archive is untrusted input. Nothing here uses tarfile extraction: every member
is checked first, then written by name into a private staging directory. The
archive checksum proves byte integrity only. Provenance comes from matching every
file against a catalog discovered independently, and the ordinary plan, checkpoint
and publish path still governs what reaches the raw tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from stock_gplm.ingestion import validate_daily

from .models import SyncRequestV1
from .sync import _canonical_hash, _read_jsonl, _reports_outside_raw, _sha256, _write_json, _write_jsonl
from .transport import LocalFixtureTransport, validate_relative_path

ARCHIVE_ROOT = "NSE"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
STAGE_MANIFEST = "stage-manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_DIRECTORY = re.compile(r"^[A-Z0-9&.-]+-EQ$")


class ArchiveRejected(ValueError):
    """The archive violates the import contract; nothing from it may be used."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    relative: str
    size: int
    is_sidecar: bool


def _check_member(
    member: tarfile.TarInfo, seen: set[str], request: SyncRequestV1,
) -> ArchiveMember | None:
    name = member.name
    if name in seen:
        raise ArchiveRejected(f"duplicate archive member: {name!r}")
    seen.add(name)
    if member.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise ArchiveRejected(f"archive member has a privileged mode: {name!r}")
    parts = PurePosixPath(name).parts
    if (
        name.startswith("/") or "\\" in name or ".." in name.split("/") or "." in name.split("/")
        or any(ord(character) < 32 for character in name) or not parts or parts[0] != ARCHIVE_ROOT
    ):
        raise ArchiveRejected(f"unsafe or unexpected archive path: {name!r}")
    if member.isdir():
        if len(parts) > 2 or (len(parts) == 2 and _SYMBOL_DIRECTORY.fullmatch(parts[1]) is None):
            raise ArchiveRejected(f"unexpected archive directory: {name!r}")
        return None
    # isreg() also covers sparse members, whose stored bytes differ from their size.
    if not member.isreg() or member.issparse():
        raise ArchiveRejected(f"archive member is not a regular file: {name!r}")
    relative = "/".join(parts[1:])
    try:
        validate_relative_path(relative)
    except ValueError as exc:
        raise ArchiveRejected(f"unexpected archive file: {name!r}") from exc
    except RuntimeError as exc:
        raise ArchiveRejected(f"unexpected archive file: {name!r}") from exc
    is_sidecar = not relative.endswith(".json.gz")
    limit = request.limits.max_sidecar_bytes if is_sidecar else request.limits.max_payload_bytes
    if member.size > limit:
        raise ArchiveRejected(f"archive member exceeds its limit: {name!r}")
    return ArchiveMember(name, relative, member.size, is_sidecar)


def _inventory(stream: BinaryIO, request: SyncRequestV1) -> list[ArchiveMember]:
    """Validate all headers before a single byte is written."""
    limit = request.limits.max_triplets * 3 + request.expected_eq_symbols + 1
    members: list[ArchiveMember] = []
    seen: set[str] = set()
    folded: set[str] = set()
    total = 0
    count = 0
    stream.seek(0)
    try:
        # "r:" accepts only an uncompressed archive, so no decompression bomb is possible.
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            for member in archive:
                count += 1
                if count > limit:
                    raise ArchiveRejected("archive has more members than the approved run allows")
                checked = _check_member(member, seen, request)
                if checked is None:
                    continue
                key = checked.relative.casefold()
                if key in folded:
                    raise ArchiveRejected(f"archive paths collide by case: {member.name!r}")
                folded.add(key)
                total += checked.size
                if total > request.limits.max_total_bytes:
                    raise ArchiveRejected("archive content exceeds the approved byte limit")
                members.append(checked)
    except tarfile.TarError as exc:
        raise ArchiveRejected(f"archive is not a readable uncompressed tar: {exc}") from exc
    if not members:
        raise ArchiveRejected("archive contains no publication files")
    return members


def stage_archive(
    *, archive_path: Path, expected_sha256: str, request: SyncRequestV1, staging_root: Path,
) -> Path:
    """Verify an archive and extract it read-only to a new private directory."""
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ArchiveRejected("expected archive SHA-256 must be lowercase hexadecimal")
    staging_base = Path(staging_root).resolve()
    _reports_outside_raw(staging_base, Path(request.destination_root))
    final = staging_base / f"archive-{expected_sha256}"
    if final.exists():
        raise FileExistsError("archive stage already exists; refusing to overwrite")
    # Hashing and reading one open descriptor keeps a replaced path from being used.
    with Path(archive_path).open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
            raise ArchiveRejected("archive is not a regular file within its size limit")
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ArchiveRejected("archive SHA-256 does not match the supplied checksum")
        members = _inventory(stream, request)
        needed = sum(item.size for item in members)
        staging_base.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(staging_base).free - needed < request.limits.minimum_free_bytes:
            raise ArchiveRejected("insufficient disk space and configured reserve for staging")
        partial = Path(tempfile.mkdtemp(prefix=".archive-partial-", dir=staging_base))
        try:
            files: dict[str, dict[str, Any]] = {}
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                for item in members:
                    target = partial.joinpath(ARCHIVE_ROOT, *PurePosixPath(item.relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(item.name)
                    if source is None:
                        raise ArchiveRejected(f"archive member has no content: {item.name!r}")
                    data = source.read(item.size + 1)
                    if len(data) != item.size:
                        raise ArchiveRejected(f"archive member size is inconsistent: {item.name!r}")
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                         getattr(os, "O_NOFOLLOW", 0), 0o400)
                    with os.fdopen(descriptor, "wb") as outgoing:
                        outgoing.write(data)
                    files[item.relative] = {
                        "bytes": item.size, "sha256": hashlib.sha256(data).hexdigest(),
                    }
            _write_json(partial / STAGE_MANIFEST, {
                "schema_version": 1, "contract": "stock-gplm.m24-archive-stage.v1",
                "archive_file": Path(archive_path).name, "archive_sha256": expected_sha256,
                "archive_bytes": info.st_size, "files": files,
            })
            os.rename(partial, final)
        except BaseException:
            if os.name == "nt":
                for item in partial.rglob("*"):
                    try:
                        os.chmod(item, 0o700 if item.is_dir() else 0o600)
                    except OSError:
                        pass
            shutil.rmtree(partial, ignore_errors=True)
            raise
    return final


def _triplet_names(marker: str) -> tuple[str, str, str]:
    payload = marker.removesuffix(".done")
    return payload, payload.removesuffix(".json.gz") + ".meta.json", marker


def reconcile_archive(
    *, stage_dir: Path, catalog_dir: Path, request: SyncRequestV1, output_root: Path,
) -> Path:
    """Require the stage to equal catalog plus catalog exceptions, byte for byte."""
    stage_dir = Path(stage_dir)
    _reports_outside_raw(output_root, Path(request.destination_root))
    catalog_manifest = json.loads((Path(catalog_dir) / "catalog-manifest.json").read_text(encoding="utf-8"))
    catalog_path = Path(catalog_dir) / "source-catalog.jsonl"
    exceptions_path = Path(catalog_dir) / "exceptions.jsonl"
    if (
        catalog_manifest["request_sha256"] != request.digest()
        or catalog_manifest["outputs"]["source-catalog.jsonl"] != _sha256(catalog_path)
        or catalog_manifest["outputs"]["exceptions.jsonl"] != _sha256(exceptions_path)
    ):
        raise ArchiveRejected("source catalog checksum or request binding mismatch")
    stage = json.loads((stage_dir / STAGE_MANIFEST).read_text(encoding="utf-8"))
    files: dict[str, dict[str, Any]] = stage["files"]
    root = stage_dir / ARCHIVE_ROOT
    rows = _read_jsonl(catalog_path, max_rows=request.limits.max_triplets)
    exceptions = _read_jsonl(exceptions_path, max_rows=request.limits.max_triplets)

    expected: dict[str, str | None] = {}
    results: list[dict[str, Any]] = []
    for row in rows:
        for part in ("payload", "metadata", "marker"):
            expected[row[part]["path"]] = row[part]["sha256"]
    # Exceptional publications have no catalog hash; their marker and M1 are the check.
    excluded = [item["marker"] for item in exceptions]
    for marker in excluded:
        for path in _triplet_names(marker):
            expected[path] = None
    missing = sorted(set(expected) - set(files))
    unexpected = sorted(set(files) - set(expected))
    if missing or unexpected:
        raise ArchiveRejected(
            f"archive differs from the catalog: {len(missing)} missing, {len(unexpected)} unexpected; "
            f"first: {(missing + unexpected)[0]}"
        )
    mismatched = sorted(
        path for path, digest in expected.items()
        if digest is not None and files[path]["sha256"] != digest
    )
    if mismatched:
        raise ArchiveRejected(f"{len(mismatched)} archive files differ from the catalog; first: {mismatched[0]}")
    for path, entry in files.items():
        on_disk = root.joinpath(*PurePosixPath(path).parts)
        if on_disk.is_symlink() or not on_disk.is_file() or _sha256(on_disk) != entry["sha256"]:
            raise ArchiveRejected(f"staged file differs from the stage manifest: {path}")

    groups = (
        [("STANDARD", row["marker"]["path"]) for row in rows]
        + [("EXCEPTIONAL_EXCLUDED", marker) for marker in excluded]
    )
    counts: dict[str, dict[str, int]] = {}
    for group, marker in sorted(groups, key=lambda item: item[1]):
        payload = _triplet_names(marker)[0]
        report = validate_daily(root.joinpath(*PurePosixPath(payload).parts))
        publication = "ACCEPT" if report.publication_integrity.valid else "REJECT"
        content = "ACCEPT" if report.content_quality.valid else "QUARANTINE"
        tally = counts.setdefault(group, {})
        tally["triplets"] = tally.get("triplets", 0) + 1
        tally[f"publication_{publication}"] = tally.get(f"publication_{publication}", 0) + 1
        tally[f"content_{content}"] = tally.get(f"content_{content}", 0) + 1
        results.append({
            "schema_version": 1, "marker": marker, "group": group,
            "payload_sha256": files[payload]["sha256"],
            "publication_classification": publication, "content_classification": content,
            "records": report.content_quality.records_seen,
            "issues": [issue.code for issue in report.publication_integrity.issues + report.content_quality.issues][:20],
        })
    # An excluded publication is not published, but corruption still impeaches the archive.
    rejected = sum(tally.get("publication_REJECT", 0) for tally in counts.values())
    if rejected:
        raise ArchiveRejected(f"{rejected} triplets failed M1 publication validation")
    binding = {
        "archive_sha256": stage["archive_sha256"],
        "catalog_sha256": _sha256(catalog_path), "exceptions_sha256": _sha256(exceptions_path),
    }
    final = Path(output_root).resolve() / f"archive-reconciliation-{_canonical_hash(binding)}"
    if final.exists():
        raise FileExistsError("archive reconciliation already exists")
    final.mkdir(parents=True)
    _write_jsonl(final / "archive-triplets.jsonl", results)
    _write_json(final / "archive-reconciliation.json", {
        "schema_version": 1, "contract": "stock-gplm.m24-archive-reconciliation.v1", **binding,
        "stage_dir": str(stage_dir.resolve()),
        "stage_manifest_sha256": _sha256(stage_dir / STAGE_MANIFEST),
        "files": len(files), "total_triplets": len(groups), "counts": counts,
        "catalog_files_matched_by_sha256": sum(item is not None for item in expected.values()),
        "exceptional_markers": excluded,
        "provenance": "ARCHIVE_BYTES_MATCH_INDEPENDENT_DISCOVERY_CATALOG",
        "output_sha256": _sha256(final / "archive-triplets.jsonl"),
    })
    return final


class ArchiveTransport(LocalFixtureTransport):
    """Read a reconciled stage through the ordinary plan and publish path."""

    def __init__(self, stage_dir: Path) -> None:
        super().__init__(Path(stage_dir) / ARCHIVE_ROOT)

    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]:
        # Root-wide like the remote enumeration, so a foreign symbol is seen and rejected.
        return tuple(sorted(item for item in self.list_files() if item.endswith(".json.gz.done")))


def archive_evidence(stage_dir: Path, reconciliation_dir: Path) -> dict[str, Any]:
    """Bind a stage to a passing reconciliation before it may be used as a source."""
    stage_dir = Path(stage_dir)
    report = json.loads((Path(reconciliation_dir) / "archive-reconciliation.json").read_text(encoding="utf-8"))
    if report["stage_manifest_sha256"] != _sha256(stage_dir / STAGE_MANIFEST):
        raise ArchiveRejected("archive stage does not match its reconciliation")
    return {
        "source": "LOCAL_ARCHIVE", "archive_sha256": report["archive_sha256"],
        "archive_reconciliation_sha256": _sha256(Path(reconciliation_dir) / "archive-reconciliation.json"),
        "provenance": report["provenance"],
    }
