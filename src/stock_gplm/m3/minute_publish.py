"""Audited, immutable import of a read-only minute snapshot into its own raw root.

The tar is untrusted. Every member is checked against independently captured source
hashes and every triplet against its publication contract before one byte is
published. Files appear read-only by exclusive hard link with the marker last; an
existing file that differs stops the import before any write. Content quality is
assessed later and separately, so a poor candle file is preserved, not refused.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

from .minute import (
    MAX_COMPRESSED_BYTES, MAX_SIDECAR_BYTES, MinuteSchemaError, is_session_date, verify_publication,
)
from .readiness import _canonical_hash, _stream_sha256, _write_json

IMPORT_VERSION = 1
LOCK_NAME = ".stock-gplm-m34.lock"
DAILY_LOCK_NAME = ".stock-gplm-m24.lock"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100_000
SUFFIXES = (".json.gz.done", ".json.gz", ".meta.json")
PUBLISH_ORDER = (".json.gz", ".meta.json", ".json.gz.done")
_MEMBER = re.compile(r"^NSE/([A-Z0-9&._-]+-EQ)/(\d{4}-\d{2}-\d{2})(\.json\.gz\.done|\.json\.gz|\.meta\.json)$")
_DIRECTORY = re.compile(r"^NSE(?:/[A-Z0-9&._-]+-EQ)?/?$")
_SOURCE_LINE = re.compile(r"^([0-9a-f]{64})  (NSE/\S+)$")


def read_source_hashes(path: Path) -> dict[str, str]:
    """Parse ``sha256sum`` output captured from the source before the transfer."""
    result: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _SOURCE_LINE.fullmatch(line)
        if match is None or match.group(2) in result:
            raise ValueError("source hash list has an invalid or repeated line")
        result[match.group(2)] = match.group(1)
    if not result:
        raise ValueError("source hash list is empty")
    return result


def read_snapshot(archive: Path) -> dict[str, bytes]:
    """Read an uncompressed tar of regular files and plain directories, or refuse it."""
    if Path(archive).stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("snapshot exceeds its bound")
    files: dict[str, bytes] = {}
    with tarfile.open(archive, "r:") as stream:
        for count, member in enumerate(stream):
            if count >= MAX_MEMBERS:
                raise ValueError("snapshot has too many members")
            name = member.name
            if member.isdir():
                if _DIRECTORY.fullmatch(name) is None:
                    raise ValueError(f"snapshot has an unexpected directory: {name}")
                continue
            if not member.isreg() or member.issparse():
                raise ValueError(f"snapshot member is not a regular file: {name}")
            if _MEMBER.fullmatch(name) is None or not is_session_date(_MEMBER.fullmatch(name).group(2)):
                raise ValueError(f"snapshot has an unexpected file: {name}")
            limit = MAX_COMPRESSED_BYTES if name.endswith(".json.gz") else MAX_SIDECAR_BYTES
            if member.size > limit or member.mode & 0o7000:
                raise ValueError(f"snapshot member breaks a size or mode limit: {name}")
            if name in files:
                raise ValueError(f"snapshot repeats a member: {name}")
            reader = stream.extractfile(member)
            if reader is None:
                raise ValueError(f"snapshot member is unreadable: {name}")
            files[name] = reader.read()
    return files


def _plan(destination: Path, payloads: dict[str, bytes]) -> dict[str, str]:
    actions: dict[str, str] = {}
    for suffix in PUBLISH_ORDER:
        target = destination.parent / f"{destination.name}{suffix}"
        data = payloads[suffix]
        if target.is_symlink():
            raise ValueError(f"destination is a symlink: {target}")
        if target.exists():
            if target.read_bytes() != data:
                raise FileExistsError(f"immutable destination differs: {target}")
            actions[suffix] = "SKIP_IDENTICAL"
        else:
            actions[suffix] = "PUBLISH"
    return actions


def _publish_file(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o444)
        os.link(temporary, target)
    finally:
        os.unlink(temporary)


def import_snapshot(
    *, archive: Path, archive_sha256: str, source_hashes: Path, master: Path, raw_root: Path,
    audit_root: Path, source_label: str,
) -> Path:
    """Publish verified triplets and write an audit that can never replace another."""
    archive, source_hashes = Path(archive).resolve(), Path(source_hashes).resolve()
    raw_root, audit_root = Path(raw_root).resolve(), Path(audit_root).resolve()
    if audit_root == raw_root or raw_root in audit_root.parents:
        raise ValueError("audit root must be outside the immutable raw tree")
    if (raw_root / DAILY_LOCK_NAME).exists():
        raise ValueError("minute raw root must be separate from the daily publication tree")
    if not source_label.strip():
        raise ValueError("source label is required")
    if _stream_sha256(archive) != archive_sha256:
        raise ValueError("snapshot does not match the supplied SHA-256")
    expected = read_source_hashes(source_hashes)
    files = read_snapshot(archive)
    if set(files) != set(expected):
        raise ValueError(
            "snapshot and source hash list disagree: "
            f"missing={sorted(set(expected) - set(files))[:5]} extra={sorted(set(files) - set(expected))[:5]}"
        )
    for name, data in files.items():
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise ValueError(f"snapshot member does not match its source hash: {name}")
    tokens = {item["symbol"]: item["token"] for item in read_shoonya_nse_eq_inventory_v1(master)}

    triplets: dict[tuple[str, str], dict[str, bytes]] = {}
    for name, data in files.items():
        symbol, day, suffix = _MEMBER.fullmatch(name).groups()  # type: ignore[union-attr]
        triplets.setdefault((symbol, day), {})[suffix] = data
    binding = {
        "contract": "stock-gplm.m34-minute-import.v1", "version": IMPORT_VERSION,
        "archive_sha256": archive_sha256, "source_hashes_sha256": _stream_sha256(source_hashes),
        "master_sha256": _stream_sha256(Path(master)), "source_label": source_label,
    }
    run_id = _canonical_hash(binding)
    final = audit_root / f"import-{run_id}"
    if final.exists():
        raise FileExistsError(f"import audit already exists: {run_id}")

    raw_root.mkdir(parents=True, exist_ok=True)
    with (raw_root / LOCK_NAME).open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        accepted: list[dict[str, Any]] = []
        exceptions: list[dict[str, str]] = []
        plans: dict[tuple[str, str], dict[str, str]] = {}
        for (symbol, day), parts in sorted(triplets.items()):
            exception = None
            try:
                if set(parts) != set(SUFFIXES):
                    raise MinuteSchemaError("triplet is incomplete")
                publication = verify_publication(
                    parts[".json.gz"], parts[".meta.json"], parts[".json.gz.done"], symbol, day,
                )
                if symbol not in tokens:
                    raise MinuteSchemaError("symbol is absent from the current master")
                if tokens[symbol] != publication.token:
                    raise MinuteSchemaError("metadata token differs from the current master")
            except (KeyError, MinuteSchemaError, ValueError) as exc:
                exception = str(exc)
            if exception is not None:
                exceptions.append({"symbol": symbol, "session_date": day, "reason": exception})
                continue
            plans[(symbol, day)] = _plan(raw_root / symbol / day, parts)
            accepted.append({
                "symbol": symbol, "session_date": day, "sha256": publication.sha256,
                "records": publication.records, "bytes": publication.bytes,
                "meta_variant": publication.meta_variant, "fetched_at_utc": publication.fetched_at_utc,
            })
        for (symbol, day), actions in plans.items():
            for suffix in PUBLISH_ORDER:
                if actions[suffix] == "PUBLISH":
                    _publish_file(raw_root / symbol / f"{day}{suffix}", triplets[(symbol, day)][suffix])
        published = sum(value == "PUBLISH" for actions in plans.values() for value in actions.values())
        for (symbol, day) in plans:
            for suffix in PUBLISH_ORDER:
                target = raw_root / symbol / f"{day}{suffix}"
                if target.read_bytes() != triplets[(symbol, day)][suffix]:
                    raise RuntimeError(f"published file changed: {target}")
    audit_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".import-{run_id}.partial-", dir=audit_root))
    try:
        _write_json(partial / "import-audit.json", {
            **binding, "run_id": run_id, "triplets_in_snapshot": len(triplets),
            "triplets_verified": len(accepted), "files_published": published,
            "files_already_identical": len(accepted) * len(PUBLISH_ORDER) - published,
            "exceptions": exceptions, "publications": accepted, "raw_root": str(raw_root),
            "source_files_modified": False, "content_quality": "NOT_EVALUATED",
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


def audit_summary(audit: Path) -> dict[str, Any]:
    data = json.loads((Path(audit) / "import-audit.json").read_text(encoding="utf-8"))
    return {key: data[key] for key in (
        "run_id", "triplets_in_snapshot", "triplets_verified", "files_published",
        "files_already_identical", "exceptions",
    )}
