"""M2.4 discovery, planning, resumable transfer, publication and reconciliation."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import stat
import tempfile
import time
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from stock_gplm.ingestion import validate_daily
from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

from .models import SyncRequestV1
from .transport import (
    MAX_BATCH_FETCH_FILES, ReadOnlyTransport, TransientTransportError, TransportError,
    validate_relative_path,
)

SYNC_LOCK = ".stock-gplm-m24.lock"
FILES_PER_TRIPLET_PULL = 5
PULL_BATCH_TRIPLETS = MAX_BATCH_FETCH_FILES // FILES_PER_TRIPLET_PULL
PULL_BATCH_BYTES = 64 * 1024 * 1024


def _active_sync_lock(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return True
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def _reports_outside_raw(output: Path, raw: Path) -> None:
    resolved = Path(output).resolve()
    raw_resolved = Path(raw).resolve()
    if resolved == raw_resolved or raw_resolved in resolved.parents:
        raise ValueError("M2.4 reports and checkpoints must be outside the raw destination")


def _sha256(path: Path, limit: int | None = None) -> str:
    if limit is not None and path.stat().st_size > limit:
        raise ValueError(f"file exceeds configured limit: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_jsonl(path: Path, *, max_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number > max_rows:
                raise ValueError("JSONL input exceeds configured row limit")
            if len(line) > 256 * 1024:
                raise ValueError("JSONL line exceeds 256 KiB")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("JSONL record must be an object")
            rows.append(value)
    return rows


def _fetch_with_retries(
    transport: ReadOnlyTransport, requests: tuple[tuple[str, Path], ...],
    request: SyncRequestV1, sleeper: Callable[[float], None], *, resume_payloads: bool,
) -> None:
    """Retry one bounded batch on transient failures; anything else stops the run."""
    attempts = 0
    while True:
        try:
            transport.fetch_many(requests, resume_payloads=resume_payloads)
            return
        except TransientTransportError:
            attempts += 1
            if attempts >= request.limits.max_attempts:
                raise
            sleeper(min(2 ** attempts, 8))


def _triplet_paths(marker_relative: str) -> tuple[str, str, str]:
    marker_relative = validate_relative_path(marker_relative)
    payload = marker_relative.removesuffix(".done")
    metadata = payload.removesuffix(".json.gz") + ".meta.json"
    return payload, metadata, marker_relative


def _parse_marker(path: Path, payload_name: str, request: SyncRequestV1) -> dict[str, Any]:
    if path.stat().st_size > request.limits.max_sidecar_bytes:
        raise ValueError("completion marker exceeds configured limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("completion marker must be an object")
    for field in ("status", "file", "sha256", "bytes", "records"):
        if field not in value:
            raise ValueError(f"completion marker is missing {field}")
    if value["status"] != "COMPLETE" or value["file"] != payload_name:
        raise ValueError("completion marker is not a complete publication")
    sha = value["sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("completion marker SHA-256 is invalid")
    if type(value["bytes"]) is not int or not 0 < value["bytes"] <= request.limits.max_payload_bytes:
        raise ValueError("completion marker byte count is invalid or oversized")
    if type(value["records"]) is not int or not 0 < value["records"] <= 100_000:
        raise ValueError("completion marker record count is invalid")
    return value


def _validate_master(
    request: SyncRequestV1, master_path: Path, *, require_current_universe: bool
) -> tuple[dict[str, str], ...]:
    if _sha256(master_path, 32 * 1024 * 1024) != request.master_sha256:
        raise ValueError("master bytes do not match the pinned request checksum")
    inventory = read_shoonya_nse_eq_inventory_v1(master_path)
    if len(inventory) != request.expected_eq_symbols:
        raise ValueError("master EQ count does not match the pinned request universe")
    if require_current_universe and request.expected_eq_symbols != 2715:
        raise ValueError("production M2.4 requires the pinned 2715-symbol universe")
    return inventory


def discover_catalog(
    *, request: SyncRequestV1, master_path: Path, transport: ReadOnlyTransport,
    output_root: Path, require_current_universe: bool = True,
    sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    """Discover marker-committed source triplets without downloading payloads."""
    inventory = _validate_master(request, master_path, require_current_universe=require_current_universe)
    _reports_outside_raw(output_root, Path(request.destination_root))
    ordered_symbols = tuple(item["symbol"] for item in inventory)
    symbols = frozenset(ordered_symbols)
    markers = transport.list_markers(ordered_symbols)
    if not markers:
        raise TransportError("remote marker enumeration returned zero publications")
    if len(markers) > request.limits.max_triplets:
        raise ValueError("remote marker count exceeds the approved run limit")
    source_files = transport.list_files()
    rows: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="stock-gplm-m24-discovery-") as temporary:
        temp = Path(temporary)
        candidates: list[dict[str, Any]] = []
        for marker_relative in sorted(markers):
            try:
                payload_relative, metadata_relative, marker_relative = _triplet_paths(marker_relative)
                symbol = PurePosixPath(marker_relative).parts[0]
                if symbol not in symbols:
                    raise ValueError("marker symbol is outside the pinned EQ universe")
                stem = PurePosixPath(payload_relative).name.removesuffix(".json.gz")
                start, separator, end = stem.partition("_")
                daily_allowed = bool(
                    request.daily_sync_start is not None
                    and start == end and start >= request.daily_sync_start
                )
                if not separator or ((start, end) not in request.window_ranges and not daily_allowed):
                    raise ValueError("segment does not match one of the five pinned collector windows")
                key = (symbol, start, end)
                if key in seen:
                    raise ValueError("duplicate symbol/window publication")
                seen.add(key)
                for relative in (marker_relative, metadata_relative):
                    size = source_files.get(relative)
                    if size is None:
                        raise ValueError(f"sidecar is absent or not a regular file: {relative}")
                    if size > request.limits.max_sidecar_bytes:
                        raise ValueError(f"sidecar exceeds configured limit: {relative}")
                if payload_relative not in source_files:
                    raise ValueError(f"payload is absent or not a regular file: {payload_relative}")
                index = len(candidates)
                candidates.append({
                    "symbol": symbol, "start": start, "end": end,
                    "payload_relative": payload_relative,
                    "metadata_relative": metadata_relative,
                    "marker_relative": marker_relative,
                    "marker_path": temp / f"{index}.done",
                    "metadata_path": temp / f"{index}.meta",
                })
            except (OSError, TransportError, TypeError, ValueError) as exc:
                exceptions.append({
                    "schema_version": 1, "marker": marker_relative,
                    "source_classification": "SOURCE_REJECTED", "reason": str(exc),
                })

        downloads = tuple(
            item
            for candidate in candidates
            for item in (
                (candidate["marker_relative"], candidate["marker_path"]),
                (candidate["metadata_relative"], candidate["metadata_path"]),
            )
        )
        for offset in range(0, len(downloads), MAX_BATCH_FETCH_FILES):
            _fetch_with_retries(
                transport, downloads[offset : offset + MAX_BATCH_FETCH_FILES],
                request, sleeper, resume_payloads=False,
            )
        for candidate in candidates:
            marker_relative = candidate["marker_relative"]
            try:
                symbol = candidate["symbol"]
                start = candidate["start"]
                end = candidate["end"]
                payload_relative = candidate["payload_relative"]
                metadata_relative = candidate["metadata_relative"]
                marker_path = candidate["marker_path"]
                metadata_path = candidate["metadata_path"]
                if marker_path.stat().st_size > request.limits.max_sidecar_bytes:
                    raise ValueError("completion marker exceeds configured limit")
                if metadata_path.stat().st_size > request.limits.max_sidecar_bytes:
                    raise ValueError("metadata sidecar exceeds configured limit")
                marker = _parse_marker(marker_path, PurePosixPath(payload_relative).name, request)
                if source_files[payload_relative] != marker["bytes"]:
                    raise ValueError("payload size disagrees with the completion marker")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise ValueError("metadata sidecar must contain an object")
                meta_payload_sha = metadata.get("sha256_compressed")
                if meta_payload_sha is not None and meta_payload_sha != marker["sha256"]:
                    raise ValueError("metadata and marker payload checksums disagree")
                if "records" in metadata and metadata["records"] != marker["records"]:
                    raise ValueError("metadata and marker record counts disagree")
                triplet_id = _canonical_hash({
                    "symbol": symbol, "start": start, "end": end,
                    "payload_sha256": marker["sha256"],
                    "metadata_sha256": _sha256(metadata_path),
                    "marker_sha256": _sha256(marker_path),
                })
                rows.append({
                    "schema_version": 1, "triplet_id": triplet_id, "symbol": symbol,
                    "window": {
                        "start_date": start, "end_date": end,
                        "kind": "COLLECTOR" if (start, end) in request.window_ranges else "DAILY",
                    },
                    "source_classification": "VERIFIED_PUBLISHED_MARKER",
                    "payload": {"path": payload_relative, "bytes": marker["bytes"], "sha256": marker["sha256"]},
                    "metadata": {"path": metadata_relative, "bytes": metadata_path.stat().st_size, "sha256": _sha256(metadata_path)},
                    "marker": {"path": marker_relative, "bytes": marker_path.stat().st_size, "sha256": _sha256(marker_path)},
                    "records": marker["records"],
                })
                total_bytes += marker["bytes"] + marker_path.stat().st_size + metadata_path.stat().st_size
            except (OSError, TransportError, TypeError, ValueError, json.JSONDecodeError) as exc:
                exceptions.append({
                    "schema_version": 1, "marker": marker_relative,
                    "source_classification": "SOURCE_REJECTED", "reason": str(exc),
                })
    if total_bytes > request.limits.max_total_bytes:
        raise RuntimeError("discovered bytes exceed the approved run limit")
    rows.sort(key=lambda item: (item["symbol"], item["window"]["start_date"]))
    binding = {
        "schema_version": 1, "contract": "stock-gplm.m24-source-catalog.v1",
        "request_sha256": request.digest(), "master_sha256": request.master_sha256,
        "records": rows, "exceptions": exceptions,
    }
    catalog_id = _canonical_hash(binding)
    final = Path(output_root).resolve() / f"catalog-{catalog_id}"
    if final.exists():
        raise FileExistsError("source catalog already exists; refusing to overwrite")
    final.mkdir(parents=True, exist_ok=False)
    _write_jsonl(final / "source-catalog.jsonl", rows)
    _write_jsonl(final / "exceptions.jsonl", exceptions)
    _write_json(final / "catalog-manifest.json", {
        **{key: value for key, value in binding.items() if key not in {"records", "exceptions"}},
        "catalog_id": catalog_id, "triplets": len(rows), "rejected": len(exceptions),
        "total_bytes": total_bytes,
        "baseline": {
            "classification": "USER_SUPPLIED_BASELINE",
            "complete_symbols": request.baseline_complete_symbols,
            "partial_symbols": request.baseline_partial_symbols,
        },
        "outputs": {
            "source-catalog.jsonl": _sha256(final / "source-catalog.jsonl"),
            "exceptions.jsonl": _sha256(final / "exceptions.jsonl"),
        },
    })
    return final


def _expected_files(row: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return row["payload"], row["metadata"], row["marker"]


def _destination_state(destination_root: Path, row: dict[str, Any]) -> tuple[str, list[str]]:
    present: list[str] = []
    mismatched: list[str] = []
    for item in _expected_files(row):
        path = destination_root.joinpath(*PurePosixPath(item["path"]).parts)
        if path.exists():
            present.append(item["path"])
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
                mismatched.append(item["path"])
    if mismatched:
        return "DESTINATION_CONFLICT", mismatched
    if len(present) == 3:
        return "SKIP_IDENTICAL", present
    if present:
        return "RECOVER_PUBLICATION", present
    return "TRANSFER", []


def build_plan(
    *, request: SyncRequestV1, catalog_dir: Path, output_root: Path,
) -> Path:
    catalog_path = Path(catalog_dir) / "source-catalog.jsonl"
    catalog_manifest = json.loads((Path(catalog_dir) / "catalog-manifest.json").read_text(encoding="utf-8"))
    if catalog_manifest["request_sha256"] != request.digest() or catalog_manifest["outputs"]["source-catalog.jsonl"] != _sha256(catalog_path):
        raise ValueError("source catalog checksum or request binding mismatch")
    rows = _read_jsonl(catalog_path, max_rows=request.limits.max_triplets)
    destination_input = Path(request.destination_root)
    if destination_input.is_symlink():
        raise ValueError("destination root must not be a symlink")
    destination = destination_input.resolve()
    _reports_outside_raw(output_root, destination)
    if _active_sync_lock(destination / SYNC_LOCK):
        raise RuntimeError("destination has an active M2.4 publication lock")
    planned: list[dict[str, Any]] = []
    transfer_bytes = 0
    for row in rows:
        action, evidence = _destination_state(destination, row)
        if action in {"TRANSFER", "RECOVER_PUBLICATION"}:
            transfer_bytes += sum(item["bytes"] for item in _expected_files(row))
        planned.append({**row, "action": action, "destination_evidence": evidence})
    if transfer_bytes > request.limits.max_total_bytes:
        raise ValueError("planned transfer exceeds max_total_bytes")
    free = shutil.disk_usage(destination if destination.exists() else destination.parent).free
    if free - transfer_bytes < request.limits.minimum_free_bytes:
        raise ValueError("insufficient free space for the plan and configured reserve")
    binding = {
        "schema_version": 1, "contract": "stock-gplm.m24-transfer-plan.v1",
        "request_sha256": request.digest(),
        "catalog_sha256": _sha256(Path(catalog_dir) / "source-catalog.jsonl"),
        "records": planned,
    }
    plan_id = _canonical_hash(binding)
    final = Path(output_root).resolve() / f"plan-{plan_id}"
    if final.exists():
        raise FileExistsError("transfer plan already exists; refusing to overwrite")
    final.mkdir(parents=True, exist_ok=False)
    _write_jsonl(final / "transfer-plan.jsonl", planned)
    counts: dict[str, int] = {}
    for row in planned:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    _write_json(final / "plan-manifest.json", {
        **{key: value for key, value in binding.items() if key != "records"},
        "plan_id": plan_id, "counts": counts, "transfer_bytes": transfer_bytes,
        "output_sha256": _sha256(final / "transfer-plan.jsonl"),
    })
    return final


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_one(source: Path, destination: Path, expected_sha256: str, triplet_id: str) -> None:
    temporary = destination.with_name(f".m24-{triplet_id}-{destination.name}.tmp")
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or _sha256(destination) != expected_sha256:
            raise FileExistsError(f"immutable destination conflict: {destination}")
        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_file() or _sha256(temporary) != expected_sha256:
                raise FileExistsError(f"publication temporary-file conflict: {temporary}")
            temporary.unlink()
        return
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file() or _sha256(temporary) != expected_sha256:
            raise FileExistsError(f"publication temporary-file conflict: {temporary}")
    else:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.chmod(temporary, 0o444)
    try:
        os.link(temporary, destination)
    except FileExistsError:
        if _sha256(destination) != expected_sha256:
            raise
    temporary.unlink(missing_ok=True)
    _fsync_directory(destination.parent)


def publish_verified_triplet(
    *, staged_symbol_dir: Path, destination_root: Path, row: dict[str, Any],
    after_publish: Callable[[str], None] | None = None,
) -> None:
    """Publish complete files exclusively; completion marker is always last."""
    symbol_dir = Path(destination_root) / row["symbol"]
    if symbol_dir.is_symlink():
        raise ValueError("destination symbol directory must not be a symlink")
    symbol_dir.mkdir(parents=True, exist_ok=True)
    ordered = _expected_files(row)
    for item in ordered:
        source = Path(staged_symbol_dir) / PurePosixPath(item["path"]).name
        if source.stat().st_size != item["bytes"] or _sha256(source) != item["sha256"]:
            raise ValueError("staged file changed before publication")
        destination = symbol_dir / source.name
        _publish_one(source, destination, item["sha256"], row["triplet_id"])
        if after_publish is not None:
            after_publish(source.name)


def _atomic_state(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _acquire_lock(destination: Path, run_id: str):
    destination.mkdir(parents=True, exist_ok=True)
    lock = destination / SYNC_LOCK
    descriptor = os.open(
        lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
    )
    stream = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError("M2.4 publication lock is not a regular file")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, RuntimeError) as exc:
        stream.close()
        raise RuntimeError("destination has an active M2.4 publication lock") from exc
    stream.seek(0)
    stream.truncate()
    stream.write(json.dumps({"run_id": run_id, "pid": os.getpid()}) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
    _fsync_directory(destination)
    return lock, stream


def pull_plan(
    *, request: SyncRequestV1, plan_dir: Path, transport: ReadOnlyTransport,
    checkpoint_root: Path, audit_root: Path, execute_plan_id: str,
    sleeper: Callable[[float], None] = time.sleep,
    after_publish: Callable[[str], None] | None = None,
) -> Path:
    """Execute or resume one immutable plan; safe reruns reuse checkpoints."""
    manifest = json.loads((Path(plan_dir) / "plan-manifest.json").read_text(encoding="utf-8"))
    plan_id = manifest["plan_id"]
    if execute_plan_id != plan_id or manifest["request_sha256"] != request.digest():
        raise ValueError("execution authorization does not match this plan and request")
    plan_path = Path(plan_dir) / "transfer-plan.jsonl"
    if _sha256(plan_path) != manifest["output_sha256"]:
        raise ValueError("transfer plan checksum mismatch")
    destination_input = Path(request.destination_root)
    if destination_input.is_symlink():
        raise ValueError("destination root must not be a symlink")
    destination = destination_input.resolve()
    checkpoint_base = Path(checkpoint_root).resolve()
    audit_base = Path(audit_root).resolve()
    if destination == checkpoint_base or destination in checkpoint_base.parents:
        raise ValueError("checkpoint root must be outside the raw destination")
    if destination == audit_base or destination in audit_base.parents:
        raise ValueError("audit root must be outside the raw destination")
    checkpoint_base.mkdir(parents=True, exist_ok=True)
    audit_base.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    rows = _read_jsonl(plan_path, max_rows=request.limits.max_triplets)
    conflicts = sum(row["action"] == "DESTINATION_CONFLICT" for row in rows)
    if conflicts:
        raise ValueError(f"plan contains {conflicts} destination conflicts; refusing to transfer")
    remaining_bytes = sum(
        sum(item["bytes"] for item in _expected_files(row))
        for row in rows if row["action"] in {"TRANSFER", "RECOVER_PUBLICATION"}
    )
    # Staged and published copies coexist when both live on one filesystem.
    same_device = checkpoint_base.stat().st_dev == destination.stat().st_dev
    reserved_bytes = remaining_bytes * 2 if same_device else remaining_bytes
    if shutil.disk_usage(checkpoint_base).free - reserved_bytes < request.limits.minimum_free_bytes:
        raise ValueError("insufficient checkpoint disk space and configured reserve")
    if shutil.disk_usage(destination).free - reserved_bytes < request.limits.minimum_free_bytes:
        raise ValueError("insufficient destination disk space and configured reserve")
    final_audit = audit_base / f"run-{plan_id}"
    if final_audit.exists():
        return final_audit
    checkpoint = checkpoint_base / plan_id
    checkpoint.mkdir(parents=True, exist_ok=True)
    state_path = checkpoint / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists()
        else {"schema_version": 1, "run_id": plan_id, "plan": str(Path(plan_dir).resolve()), "completed": []}
    )
    if state.get("run_id") != plan_id or state.get("plan") != str(Path(plan_dir).resolve()):
        raise ValueError("checkpoint belongs to another plan")
    completed = set(state.get("completed", []))
    pending = [
        row for row in rows
        if row["action"] != "SKIP_IDENTICAL" and row["triplet_id"] not in completed
    ]
    if pending:
        source_files = transport.list_files()
        for row in pending:
            for item in _expected_files(row):
                if source_files.get(item["path"]) != item["bytes"]:
                    raise TransportError(f"source file changed after discovery: {item['path']}")
    _, lock_stream = _acquire_lock(destination, plan_id)
    verified: dict[str, dict[str, Any]] = {}
    batches: list[list[dict[str, Any]]] = []
    batch_bytes = 0
    for row in pending:
        size = sum(item["bytes"] for item in _expected_files(row))
        if (
            not batches or len(batches[-1]) >= PULL_BATCH_TRIPLETS
            or batch_bytes + size > PULL_BATCH_BYTES
        ):
            batches.append([])
            batch_bytes = 0
        batches[-1].append(row)
        batch_bytes += size
    try:
        for batch in batches:
            downloads: list[tuple[str, Path]] = []
            for row in batch:
                stage = checkpoint / "triplets" / row["triplet_id"] / row["symbol"]
                stage.mkdir(parents=True, exist_ok=True)
                payload, metadata, marker = _expected_files(row)
                # Sidecars are read again after the payload to detect source changes.
                downloads.extend((
                    (marker["path"], stage / PurePosixPath(marker["path"]).name),
                    (metadata["path"], stage / PurePosixPath(metadata["path"]).name),
                    (payload["path"], stage / PurePosixPath(payload["path"]).name),
                    (marker["path"], stage / ".marker-recheck"),
                    (metadata["path"], stage / ".metadata-recheck"),
                ))
            _fetch_with_retries(
                transport, tuple(downloads), request, sleeper, resume_payloads=True,
            )
            for row in batch:
                triplet_id = row["triplet_id"]
                triplet_stage = checkpoint / "triplets" / triplet_id / row["symbol"]
                payload, metadata, marker = _expected_files(row)
                marker_stage = triplet_stage / PurePosixPath(marker["path"]).name
                metadata_stage = triplet_stage / PurePosixPath(metadata["path"]).name
                payload_stage = triplet_stage / PurePosixPath(payload["path"]).name
                if _sha256(marker_stage, request.limits.max_sidecar_bytes) != marker["sha256"] or _sha256(metadata_stage, request.limits.max_sidecar_bytes) != metadata["sha256"]:
                    raise TransportError("source sidecar changed after discovery")
                if payload_stage.stat().st_size != payload["bytes"] or _sha256(payload_stage) != payload["sha256"]:
                    raise TransportError("payload byte count or SHA-256 differs from the publication marker")
                if (
                    _sha256(triplet_stage / ".marker-recheck", request.limits.max_sidecar_bytes) != marker["sha256"]
                    or _sha256(triplet_stage / ".metadata-recheck", request.limits.max_sidecar_bytes) != metadata["sha256"]
                ):
                    raise TransportError("source sidecar changed during transfer")
                report = validate_daily(payload_stage)
                if not report.publication_integrity.valid:
                    raise ValueError("staged triplet failed M1 publication validation")
                publish_verified_triplet(
                    staged_symbol_dir=triplet_stage, destination_root=destination,
                    row=row, after_publish=after_publish,
                )
                completed.add(triplet_id)
                state["completed"] = sorted(completed)
                _atomic_state(state_path, state)
                verified[triplet_id] = {
                    "content_classification": "ACCEPT" if report.content_quality.valid else "QUARANTINE",
                }
        results: list[dict[str, Any]] = []
        for row in rows:
            triplet_id = row["triplet_id"]
            transferred = row["action"] != "SKIP_IDENTICAL"
            # A checkpoint or plan claim is not evidence that the bytes are still there.
            if _destination_state(destination, row)[0] != "SKIP_IDENTICAL":
                raise TransportError(f"published triplet no longer matches its plan: {triplet_id}")
            results.append({
                "schema_version": 1, "triplet_id": triplet_id, "symbol": row["symbol"],
                "window": row["window"],
                "source_classification": "VERIFIED_PUBLISHED" if transferred else row["source_classification"],
                "transfer_classification": "VERIFIED_TRANSFER" if transferred else "SKIP_IDENTICAL",
                "content_classification": verified.get(triplet_id, {}).get("content_classification", "NOT_EVALUATED"),
                "historical_classification": "NOT_EVALUATED",
                "training_readiness": "NOT_READY",
                "payload": row["payload"], "metadata": row["metadata"], "marker": row["marker"],
            })
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
        _fsync_directory(destination)
    # The final name is what marks a run finished, so it must appear atomically.
    partial_audit = audit_base / f".run-{plan_id}.partial"
    shutil.rmtree(partial_audit, ignore_errors=True)
    partial_audit.mkdir(parents=True, exist_ok=False)
    _write_jsonl(partial_audit / "transfer-results.jsonl", results)
    counts: dict[str, int] = {}
    for row in results:
        value = row["transfer_classification"]
        counts[value] = counts.get(value, 0) + 1
    _write_json(partial_audit / "run-manifest.json", {
        "schema_version": 1, "contract": "stock-gplm.m24-run.v1", "run_id": plan_id,
        "plan_sha256": _sha256(plan_path), "request_sha256": request.digest(),
        "classifications": {
            "source": "REPORTED_PER_TRIPLET", "transfer": counts,
            "content": "REPORTED_PER_TRIPLET", "historical": "NOT_EVALUATED",
            "training_readiness": "NOT_READY",
        },
        "outputs": {"transfer-results.jsonl": _sha256(partial_audit / "transfer-results.jsonl")},
    })
    os.rename(partial_audit, final_audit)
    _fsync_directory(audit_base)
    return final_audit


def validate_run(*, request: SyncRequestV1, run_dir: Path, output_root: Path) -> Path:
    _reports_outside_raw(output_root, Path(request.destination_root))
    results_path = Path(run_dir) / "transfer-results.jsonl"
    run_manifest = json.loads((Path(run_dir) / "run-manifest.json").read_text(encoding="utf-8"))
    if run_manifest["request_sha256"] != request.digest() or run_manifest["outputs"]["transfer-results.jsonl"] != _sha256(results_path):
        raise ValueError("transfer audit checksum or request binding mismatch")
    rows = _read_jsonl(results_path, max_rows=request.limits.max_triplets)
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["transfer_classification"] not in {"VERIFIED_TRANSFER", "SKIP_IDENTICAL"} or "payload" not in row:
            continue
        payload = row["payload"]
        path = Path(request.destination_root).joinpath(*PurePosixPath(payload["path"]).parts)
        report = validate_daily(path)
        output_rows.append({
            "schema_version": 1, "triplet_id": row["triplet_id"], "symbol": row["symbol"],
            "source_classification": row["source_classification"],
            "transfer_classification": row["transfer_classification"],
            "publication_classification": "ACCEPT" if report.publication_integrity.valid else "REJECT",
            "content_classification": "ACCEPT" if report.content_quality.valid else "QUARANTINE",
            "historical_classification": "NOT_EVALUATED", "training_readiness": "NOT_READY",
            "validation": report.to_dict(),
        })
    run_id = Path(run_dir).name.removeprefix("run-")
    final = Path(output_root).resolve() / f"validation-{run_id}"
    if final.exists():
        raise FileExistsError("validation report already exists")
    final.mkdir(parents=True)
    _write_jsonl(final / "validation-results.jsonl", output_rows)
    _write_json(final / "validation-manifest.json", {
        "schema_version": 1, "run_id": run_id, "records": len(output_rows),
        "output_sha256": _sha256(final / "validation-results.jsonl"),
    })
    return final


def reconcile_run(
    *, request: SyncRequestV1, master_path: Path, catalog_dir: Path,
    output_root: Path, m23_summary: Path | None = None,
    run_dir: Path | None = None, validation_dir: Path | None = None,
    require_current_universe: bool = True,
) -> Path:
    _reports_outside_raw(output_root, Path(request.destination_root))
    inventory = _validate_master(request, master_path, require_current_universe=require_current_universe)
    catalog_path = Path(catalog_dir) / "source-catalog.jsonl"
    catalog_manifest = json.loads((Path(catalog_dir) / "catalog-manifest.json").read_text(encoding="utf-8"))
    if catalog_manifest["request_sha256"] != request.digest() or catalog_manifest["outputs"]["source-catalog.jsonl"] != _sha256(catalog_path):
        raise ValueError("source catalog checksum or request binding mismatch")
    catalog = _read_jsonl(catalog_path, max_rows=request.limits.max_triplets)
    published = {
        (row["symbol"], row["window"]["start_date"], row["window"]["end_date"])
        for row in catalog
    }
    historical = "NOT_EVALUATED"
    if m23_summary is not None:
        value = json.loads(Path(m23_summary).read_text(encoding="utf-8"))
        historical = value.get("gates", {}).get("historical_completeness", "NOT_EVALUATED")
    transferred: dict[tuple[str, str, str], str] = {}
    if run_dir is not None:
        for item in _read_jsonl(Path(run_dir) / "transfer-results.jsonl", max_rows=request.limits.max_triplets):
            window = item.get("window")
            if isinstance(window, dict):
                transferred[(item["symbol"], window["start_date"], window["end_date"])] = item["transfer_classification"]
    content: dict[tuple[str, str, str], str] = {}
    if validation_dir is not None:
        by_triplet = {item["triplet_id"]: item for item in catalog}
        for item in _read_jsonl(Path(validation_dir) / "validation-results.jsonl", max_rows=request.limits.max_triplets):
            source_row = by_triplet.get(item["triplet_id"])
            if source_row is not None:
                window = source_row["window"]
                content[(item["symbol"], window["start_date"], window["end_date"])] = item["content_classification"]
    rows: list[dict[str, Any]] = []
    for instrument in inventory:
        for window in request.collector_windows:
            source = (
                "VERIFIED_PUBLISHED" if (instrument["symbol"], window.start_date, window.end_date) in published
                else "MISSING_PUBLISHED_WINDOW"
            )
            key = (instrument["symbol"], window.start_date, window.end_date)
            rows.append({
                "schema_version": 1, "symbol": instrument["symbol"],
                "window": asdict(window), "source_classification": source,
                "transfer_classification": transferred.get(key, "NOT_EVALUATED"),
                "content_classification": content.get(key, "NOT_EVALUATED"),
                "historical_classification": historical,
                "training_readiness": "NOT_READY",
            })
    binding = {
        "request_sha256": request.digest(), "master_sha256": request.master_sha256,
        "catalog_sha256": _sha256(Path(catalog_dir) / "source-catalog.jsonl"),
        "run_sha256": (
            _sha256(Path(run_dir) / "transfer-results.jsonl") if run_dir is not None else None
        ),
        "validation_sha256": (
            _sha256(Path(validation_dir) / "validation-results.jsonl")
            if validation_dir is not None else None
        ),
    }
    reconciliation_id = _canonical_hash(binding)
    final = Path(output_root).resolve() / f"reconciliation-{reconciliation_id}"
    if final.exists():
        raise FileExistsError("reconciliation already exists")
    final.mkdir(parents=True)
    _write_jsonl(final / "symbol-window-reconciliation.jsonl", rows)
    counts: dict[str, int] = {}
    for row in rows:
        value = row["source_classification"]
        counts[value] = counts.get(value, 0) + 1
    _write_json(final / "reconciliation-summary.json", {
        "schema_version": 1, "reconciliation_id": reconciliation_id,
        "symbols": len(inventory), "windows": len(request.collector_windows),
        "rows": len(rows), "source_counts": counts,
        "baseline": {
            "classification": "USER_SUPPLIED_BASELINE",
            "complete_symbols": request.baseline_complete_symbols,
            "partial_symbols": request.baseline_partial_symbols,
        },
        "historical_classification": historical, "training_readiness": "NOT_READY",
        "output_sha256": _sha256(final / "symbol-window-reconciliation.jsonl"),
    })
    return final
