"""Pre-cutoff daily data loading pinned to a manifest-verified M3.2 run.

Only segments whose declared window ends strictly before the locked-holdout cutoff
are opened. Segments that straddle or follow the cutoff are listed, never read.
Integrity and content checks reuse :func:`stock_gplm.ingestion.daily.validate_daily`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingestion.daily import validate_daily

M32_CONTRACT = "stock-gplm.m32-research-run.v1"
_SEGMENT = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class HoldoutPin:
    """The locked-holdout boundary read from a manifest-verified M3.2 split plan."""

    m32_run_id: str
    split_plan_sha256: str
    cutoff: str  # earliest locked-holdout session over every horizon

    def to_dict(self) -> dict[str, str]:
        return {"m32_run_id": self.m32_run_id, "split_plan_sha256": self.split_plan_sha256, "cutoff": self.cutoff}


def pin_holdout(m32_run: Path, expected_run_id: str | None = None,
                expected_split_plan_sha256: str | None = None) -> HoldoutPin:
    """Verify the split plan against its run manifest and return the global cutoff."""
    run = Path(m32_run)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != M32_CONTRACT:
        raise ValueError(f"{run} is not an {M32_CONTRACT} run")
    expected = manifest.get("outputs", {}).get("split-plan.json")
    actual = sha256_file(run / "split-plan.json")
    if expected is None or actual != expected:
        raise ValueError("split-plan.json does not match its M3.2 run manifest")
    if expected_run_id is not None and manifest.get("run_id") != expected_run_id:
        raise ValueError(f"M3.2 run id {manifest.get('run_id')} differs from pinned {expected_run_id}")
    if expected_split_plan_sha256 is not None and actual != expected_split_plan_sha256:
        raise ValueError("split-plan SHA-256 differs from the pinned value")
    plan = json.loads((run / "split-plan.json").read_text(encoding="utf-8"))
    starts = [p["locked_holdout"]["first_as_of"] for p in plan["plans"]]
    if not starts or not all(isinstance(s, str) and len(s) == 10 for s in starts):
        raise ValueError("split plan lacks locked-holdout boundaries")
    return HoldoutPin(str(manifest["run_id"]), actual, min(starts))


@dataclass
class SymbolData:
    symbol: str
    status: str  # ACCEPTED | QUARANTINED | MISSING
    bars: tuple[dict[str, Any], ...] = ()
    segments_opened: list[dict[str, Any]] = field(default_factory=list)
    segments_withheld: list[dict[str, str]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def bars_sha256(self) -> str:
        encoded = json.dumps(self.bars, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "status": self.status, "bars": len(self.bars),
            "first": self.bars[0]["date"] if self.bars else None,
            "last": self.bars[-1]["date"] if self.bars else None,
            "bars_sha256": self.bars_sha256() if self.bars else None,
            "segments_opened": self.segments_opened, "segments_withheld": self.segments_withheld,
            "issues": self.issues,
        }


def load_symbol(raw_root: Path, symbol: str, cutoff: str) -> SymbolData:
    """Load and validate every pre-cutoff segment of one symbol. Never writes."""
    directory = Path(raw_root) / symbol
    result = SymbolData(symbol, "MISSING")
    if not directory.is_dir():
        result.issues.append("symbol directory absent")
        return result
    by_date: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        match = _SEGMENT.fullmatch(path.name)
        if not match:
            continue
        start, end = match.groups()
        if end >= cutoff:  # straddles or follows the locked holdout: never opened
            result.segments_withheld.append({"file": path.name, "window_end": end})
            continue
        report = validate_daily(path)
        record = {"file": path.name, "window": [start, end], "disposition": report.disposition,
                  "sha256": report.publication_integrity.sha256,
                  "records": report.publication_integrity.records}
        result.segments_opened.append(record)
        if report.disposition != "ACCEPT":
            codes = [i.code for i in report.publication_integrity.issues + report.content_quality.issues]
            result.issues.append(f"{path.name}: {report.disposition} {sorted(set(codes))}")
            continue
        for candle in report.content_quality.normalized_candles:
            row = {"date": candle.trading_date, "open": float(candle.open), "high": float(candle.high),
                   "low": float(candle.low), "close": float(candle.close), "volume": float(candle.volume)}
            prior = by_date.get(row["date"])
            if prior is not None and prior != row:
                result.issues.append(f"{row['date']}: overlapping segments disagree")
            by_date[row["date"]] = row
    if any(row["date"] >= cutoff for row in by_date.values()):  # defence in depth
        raise AssertionError("a bar at or after the cutoff was loaded")
    result.bars = tuple(by_date[d] for d in sorted(by_date))
    if result.issues:
        result.status = "QUARANTINED"
    elif not result.bars:
        result.status = "MISSING"
        result.issues.append("no pre-cutoff segments")
    else:
        result.status = "ACCEPTED"
    return result
