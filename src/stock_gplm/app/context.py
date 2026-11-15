"""Shared run context: settings, the audit database, the writer lock and run bookkeeping."""
from __future__ import annotations

import hashlib
import json
import traceback
from pathlib import Path
from typing import Any, Mapping

from . import ACCESS_BASIS, CLASSIFICATION, __version__
from .db import AuditDB, LockBusy, ProcessLock
from .progress import Progress
from .settings import Settings, load

_CODE_DIRS = ("app", "m54")


def code_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for d in _CODE_DIRS:
        for p in sorted((root / d).rglob("*.py")):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    for extra in ("ingestion/daily.py", "m3/minute.py"):
        p = root / extra
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


class App:
    def __init__(self, config: str | Path | None = None, home: str | Path | None = None, *, readonly: bool = False,
                 overrides: Mapping[str, Any] | None = None, quiet: bool = False):
        self.s: Settings = load(config, home, overrides)
        self.readonly = readonly
        if not readonly:
            self.s.ensure()
        self.db = AuditDB(self.s.db_path, self.s.events_dir, readonly=readonly)
        self.lock: ProcessLock | None = None
        self.progress = Progress(None if readonly else self.s.progress_path, quiet=quiet)
        self.code_sha = code_sha256()
        self.run_id: str | None = None

    def start(self, kind: str, command: str, args: Mapping[str, Any], device: str | None = None) -> str:
        if self.readonly:
            raise RuntimeError("read-only context cannot start a run")
        self.lock = ProcessLock(self.s.home / "gplm.lock").acquire(f"{kind}: {command}")
        self.run_id = self.db.start_run(kind, command, dict(args), device=device, code_sha=self.code_sha,
                                        config_sha=self.s.sha256)
        if self.lock.stale_takeover:
            self.db.event("STALE_LOCK_REPLACED", {"previous_owner": self.lock.stale_takeover}, level="WARN")
        # Canonical JSON of the effective configuration; its SHA-256 equals runs.config_sha.
        self.db.snapshot_config(self.s.path, json.dumps(self.s.raw, sort_keys=True))
        self.db.event("ACCESS_POLICY", {"basis": ACCESS_BASIS, "detail": self.s.section("access"),
                                        "classification": CLASSIFICATION, "app_version": __version__})
        return self.run_id

    def finish(self, status: str, summary: Mapping[str, Any]) -> None:
        try:
            if self.run_id:
                self.db.finish_run(status, dict(summary))
        finally:
            if self.lock:
                self.lock.release()

    def fail(self, exc: BaseException) -> None:
        if self.run_id:
            self.db.event("ERROR", {"type": type(exc).__name__, "message": str(exc),
                                    "traceback": traceback.format_exc(limit=8)}, level="ERROR")
        self.finish("FAILED", {"error": f"{type(exc).__name__}: {exc}"})

    def close(self) -> None:
        if self.lock and self.lock.held:
            self.lock.release()
        self.db.close()


def json_out(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


__all__ = ["App", "LockBusy", "code_sha256", "json_out"]
