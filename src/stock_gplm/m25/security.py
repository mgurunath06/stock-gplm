"""Crash-safe cache, exclusive lock, and redacted local audit primitives."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import tempfile
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from .models import MAX_CACHE_BYTES, SessionRecordV1

_SENSITIVE = ("token", "password", "secret", "code", "checksum", "authorization", "url")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _ensure_private_directory(path: Path) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("state directory ownership or permissions are unsafe")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_regular(path: Path, maximum: int) -> os.stat_result:
    if path.is_symlink():
        raise ValueError("state file must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("state file ownership or type is unsafe")
    if info.st_mode & 0o077 or info.st_size > maximum:
        raise ValueError("state file permissions or size are unsafe")
    return info


class SessionCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> SessionRecordV1 | None:
        if self.path.is_symlink():
            raise ValueError("session cache must not be a symlink")
        if not self.path.exists():
            return None
        _secure_regular(self.path, MAX_CACHE_BYTES)
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = os.read(descriptor, MAX_CACHE_BYTES + 1)
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        if isinstance(value, dict) and value.get("schema_version") == 1:
            return SessionRecordV1.from_dict(value)
        return SessionRecordV1.from_legacy_dict(value)

    def save(self, record: SessionRecordV1) -> None:
        parent = self.path.parent
        _ensure_private_directory(parent)
        if self.path.is_symlink():
            raise ValueError("session cache must not be a symlink")
        if self.path.exists():
            _secure_regular(self.path, MAX_CACHE_BYTES)
        payload = (json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".shoonya-session-", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(parent)
        finally:
            temporary.unlink(missing_ok=True)


class ExclusiveAuthLock(AbstractContextManager["ExclusiveAuthLock"]):
    def __init__(
        self, path: Path, timeout_seconds: int, *, sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.sleeper = sleeper
        self.monotonic = monotonic
        self._stream = None

    def __enter__(self) -> "ExclusiveAuthLock":
        _ensure_private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError("authentication lock must not be a symlink")
        descriptor = os.open(
            self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
        )
        stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise ValueError("authentication lock must be a regular file")
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if self.monotonic() >= deadline:
                    stream.close()
                    raise TimeoutError("another Shoonya authentication process holds the lock")
                self.sleeper(0.1)
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid(), "acquired_at_epoch": time.time()}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        _fsync_directory(self.path.parent)
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class AuditLogger:
    def __init__(self, audit_path: Path, status_path: Path) -> None:
        self.audit_path = Path(audit_path)
        self.status_path = Path(status_path)

    @staticmethod
    def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in fields.items():
            lowered = key.lower()
            if any(fragment in lowered for fragment in _SENSITIVE):
                safe[key] = "<redacted>"
            elif isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
            else:
                safe[key] = str(type(value).__name__)
        return safe

    def event(self, name: str, *, run_id: str, **fields: Any) -> None:
        if not name.isupper() or len(name) > 64:
            raise ValueError("audit event name must be bounded uppercase text")
        _ensure_private_directory(self.audit_path.parent)
        payload = {
            "schema_version": 1, "timestamp_epoch": time.time(), "event": name,
            "run_id": run_id, **self._safe_fields(fields),
        }
        raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(raw) > 4096:
            raise ValueError("audit event exceeds 4096 bytes")
        descriptor = os.open(
            self.audit_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("audit log ownership, type, or permissions are unsafe")
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def status(self, outcome: str, *, run_id: str, **fields: Any) -> None:
        _ensure_private_directory(self.status_path.parent)
        payload = {
            "schema_version": 1, "timestamp_epoch": time.time(), "outcome": outcome,
            "run_id": run_id, **self._safe_fields(fields),
        }
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".shoonya-status-", dir=self.status_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_path)
            _fsync_directory(self.status_path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def new_run_id() -> str:
    return secrets.token_hex(16)
