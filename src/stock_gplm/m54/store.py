"""Atomic, integrity-checked persistence for discovery runs."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean(value: Any) -> Any:
    """Replace non-finite floats with None so JSON stays strict."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(_clean(value), sort_keys=True, indent=2, allow_nan=False) + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, value: Any) -> str:
    text = dumps(value)
    write_text_atomic(path, text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_checkpoint(path: Path, value: Mapping[str, Any]) -> None:
    """A checkpoint carries its own payload hash so a torn or edited file is detected on resume."""
    payload = _clean(dict(value))
    write_json_atomic(path, {"payload_sha256": canonical_hash(payload), "payload": payload})


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    if not Path(path).is_file():
        return None
    raw = read_json(path)
    if canonical_hash(raw.get("payload")) != raw.get("payload_sha256"):
        raise ValueError(f"checkpoint {path} failed its integrity check; delete it to recompute that stage")
    return raw["payload"]
