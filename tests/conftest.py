"""Synthetic Oracle publications based on the sanitized RELIANCE-EQ record."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from stock_gplm.m5 import authority as _m5_authority


@pytest.fixture(autouse=True)
def _isolate_administrative_trust_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Never let a real /etc/stock-gplm trust root on the test host influence a test.

    Points the anchor at an empty directory: no trust root is installed, so
    authority stays absent unless a test installs a synthetic one explicitly.
    """
    empty = tmp_path_factory.mktemp("no-admin-trust-root")
    monkeypatch.setattr(_m5_authority, "_ANCHOR", _m5_authority.TrustAnchor(empty, 0, empty))


@pytest.fixture
def row() -> dict[str, str]:
    return {
        "time": "07-OCT-2026",
        "into": "1212.00",
        "inth": "1220.80",
        "intl": "1203.50",
        "intc": "1207.70",
        "intv": "11880731.00",
        "ssboe": "1791331200",
    }


@pytest.fixture
def publish(tmp_path: Path):
    def create(
        rows: list[Any], *, serialize_rows: bool = True,
        metadata_changes: dict[str, Any] | None = None,
        manifest_changes: dict[str, Any] | None = None,
        payload: bytes | None = None,
    ) -> Path:
        directory = tmp_path / "data/daily/shoonya/NSE/RELIANCE-EQ"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "2026-10-01_2026-10-07.json.gz"
        value = [json.dumps(item) for item in rows] if serialize_rows else rows
        raw = gzip.compress(payload if payload is not None else json.dumps(value).encode(), mtime=0)
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        metadata = {
            "source": "shoonya", "symbol": "RELIANCE-EQ", "from": "2026-10-01",
            "to": "2026-10-07", "quality_status": "UNVALIDATED_RAW",
            "records": len(rows), "sha256_compressed": digest,
            "fetched_at_utc": "2026-10-07T18:00:00Z",
        }
        metadata.update(metadata_changes or {})
        path.with_name("2026-10-01_2026-10-07.meta.json").write_text(json.dumps(metadata))
        manifest = {
            "status": "COMPLETE", "file": path.name, "sha256": digest,
            "bytes": len(raw), "records": len(rows),
        }
        manifest.update(manifest_changes or {})
        path.with_name(f"{path.name}.done").write_text(json.dumps(manifest))
        return path
    return create
