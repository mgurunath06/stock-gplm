"""Secure loading of provider credentials from a private ``.env`` file.

Search order (first existing file wins):

1. ``$GPLM_ENV_FILE`` if set;
2. ``.env`` at the root of the current Git worktree;
3. ``.env`` at the root of the *main* worktree, found through the ``.git`` file of a
   linked worktree (``gitdir: <main>/.git/worktrees/<name>``). This lets a separate
   worktree use the single private file in the primary checkout.

The file must be a regular file (not a symlink) owned by the current user with mode
``0600`` or stricter (POSIX only; Windows relies on NTFS ACLs); otherwise loading is refused. Only ``GPLM_*`` names are
loaded, values are never evaluated, interpolated, logged or returned. Existing
environment variables win unless ``override=True``.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import MutableMapping

MAX_BYTES = 64 * 1024
_NAME = re.compile(r"^GPLM_[A-Z0-9_]{1,64}$")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class InsecureEnvFile(PermissionError):
    """The credentials file is readable by others, not owned by the user, or not a regular file."""


def _worktree_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return None


def _main_worktree_root(root: Path) -> Path | None:
    git = root / ".git"
    if not git.is_file():
        return None
    text = git.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()
    # <main>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return None


def candidate_paths(start: Path | None = None, environ: MutableMapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    out: list[Path] = []
    if env.get("GPLM_ENV_FILE"):
        out.append(Path(env["GPLM_ENV_FILE"]).expanduser())
    root = _worktree_root((start or Path.cwd()).resolve())
    if root is None:
        root = _worktree_root(Path(__file__).resolve())
    if root is not None:
        out.append(root / ".env")
        main = _main_worktree_root(root)
        if main is not None:
            out.append(main / ".env")
    return out


def locate(start: Path | None = None, environ: MutableMapping[str, str] | None = None) -> Path | None:
    for p in candidate_paths(start, environ):
        if p.exists() or p.is_symlink():
            return p
    return None


def check_permissions(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InsecureEnvFile(f"{path} must be a regular file, not a symlink or special file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise InsecureEnvFile(f"{path} is not owned by the current user")
    # Windows has no POSIX mode bits (files always report 0o666/0o444); access is governed by
    # NTFS ACLs of the user's own drive/profile, so the mode check only applies on POSIX.
    if os.name != "nt" and info.st_mode & 0o077:
        raise InsecureEnvFile(f"{path} has mode {oct(info.st_mode & 0o777)}; run: chmod 600 {path}")
    if info.st_size > MAX_BYTES:
        raise InsecureEnvFile(f"{path} exceeds {MAX_BYTES} bytes")


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        name, value = m.group(1), m.group(2).strip()
        if not _NAME.match(name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def load(path: Path | None = None, environ: MutableMapping[str, str] | None = None, *,
         override: bool = False, start: Path | None = None) -> dict[str, object]:
    """Load GPLM_* variables into ``environ``. Returns metadata only: path and the names loaded."""
    env = os.environ if environ is None else environ
    target = Path(path) if path is not None else locate(start, env)
    if target is None:
        return {"path": None, "loaded": [], "skipped_existing": []}
    check_permissions(target)
    values = parse(target.read_text(encoding="utf-8"))
    loaded, skipped = [], []
    for name, value in sorted(values.items()):
        if not value:
            continue
        if name in env and not override:
            skipped.append(name)
            continue
        env[name] = value
        loaded.append(name)
    return {"path": str(target), "loaded": loaded, "skipped_existing": skipped}
