"""Application settings: one JSON file, resolved paths, and environment overrides.

``config/app/gplm.json`` is the default. ``GPLM_CONFIG`` selects another file and ``GPLM_HOME``
moves the state directory (database, caches, models, reports). Relative paths in the file are
resolved against the file's own directory, so the same file works on Linux and Windows.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CONTRACT = "stock-gplm.app-config.v1"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO / "config" / "app" / "gplm.json"


def _resolve(base: Path, value: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. ``raw`` keeps the file content for hashing and auditing."""

    path: Path
    raw: Mapping[str, Any]
    home: Path
    daily_root: Path
    minute_roots: tuple[Path, ...]
    real_raw_tree: Path
    daily_extra_roots: tuple[Path, ...] = ()  # e.g. NSE bhavcopy history (bhavcopy_history.py); older dates only
    index_root: Path | None = None  # official NSE index history (IDX_<NAME>-EQ dirs), pulled from the cloud

    @property
    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True).encode()).hexdigest()

    def section(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(dict(self.raw.get(name, {})))

    # state layout --------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "gplm.sqlite"

    @property
    def events_dir(self) -> Path:
        return self.home / "events"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def reports_dir(self) -> Path:
        return self.home / "reports"

    @property
    def strategies_dir(self) -> Path:
        return self.home / "strategies"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def progress_path(self) -> Path:
        return self.home / "progress.json"

    def ensure(self) -> None:
        for d in (self.home, self.events_dir, self.cache_dir, self.models_dir, self.reports_dir,
                  self.strategies_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
        for r in (self.daily_root, *self.minute_roots, self.real_raw_tree):
            r = r.resolve()
            if self.home.resolve() == r or r in self.home.resolve().parents:
                raise ValueError(f"GPLM home {self.home} must not be inside the raw-data tree {r}")


def load(path: str | Path | None = None, home: str | Path | None = None,
         overrides: Mapping[str, Any] | None = None) -> Settings:
    cfg_path = Path(path or os.environ.get("GPLM_CONFIG") or DEFAULT_CONFIG).resolve()
    raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))  # tolerate a BOM (Windows PowerShell 5.1 writes one)
    if raw.get("contract") != CONTRACT:
        raise ValueError(f"{cfg_path} is not a {CONTRACT} document")
    for key, value in (overrides or {}).items():
        section, _, field = key.partition(".")
        if field:
            raw.setdefault(section, {})[field] = value
        else:
            raw[section] = value
    base = cfg_path.parent
    d = raw["data"]
    explicit = home or os.environ.get("GPLM_HOME")  # command line / environment: relative to the working directory
    home_path = Path(os.path.expanduser(str(explicit))).resolve() if explicit else _resolve(base, raw.get("home", "../../var/gplm"))
    return Settings(
        path=cfg_path, raw=raw, home=home_path,
        daily_root=_resolve(base, d["daily_root"]),
        minute_roots=tuple(_resolve(base, r) for r in d["minute_roots"]),
        real_raw_tree=_resolve(base, d["real_raw_tree"]),
        daily_extra_roots=tuple(_resolve(base, r) for r in d.get("daily_extra_roots", [])),
        index_root=_resolve(base, d["index_root"]) if d.get("index_root") else None,
    )
