"""Read-only acceptance workflow for one published Shoonya daily triplet."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from stock_gplm.ingestion import validate_daily

Decision = Literal["ACCEPT", "QUARANTINE", "REJECT"]


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    exists: bool
    bytes: int | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AcceptanceReport:
    schema_version: int
    evaluated_at_utc: str
    source: str
    decision: Decision
    reasons: list[str]
    source_unchanged_during_run: bool | None
    triplet_before: list[FileFingerprint]
    triplet_after: list[FileFingerprint]
    validation: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def publication_triplet(source: Path) -> tuple[Path, Path, Path]:
    """Return data, metadata, and completion-manifest paths without reading them."""
    source = Path(source)
    base = source.name.removesuffix(".json.gz")
    return (
        source,
        source.with_name(f"{base}.meta.json"),
        source.with_name(f"{source.name}.done"),
    )


def _fingerprint(path: Path) -> FileFingerprint:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as exc:
        return FileFingerprint(path=str(path), exists=False, error=str(exc))
    return FileFingerprint(
        path=str(path), exists=True, bytes=byte_count, sha256=digest.hexdigest()
    )


def _fingerprint_triplet(source: Path) -> list[FileFingerprint]:
    return [_fingerprint(path) for path in publication_triplet(source)]


def _issue_reasons(validation_report) -> list[str]:
    if not validation_report.publication_integrity.valid:
        return [
            f"{issue.code}: {issue.message}"
            for issue in validation_report.publication_integrity.issues
        ]
    return [
        f"{issue.code} at record {issue.record}: {issue.message}"
        for issue in validation_report.content_quality.issues
    ]


def run_acceptance(
    source: Path, *, evaluated_at: datetime | None = None
) -> AcceptanceReport:
    """Fingerprint, validate, and re-fingerprint one immutable source triplet."""
    source = Path(source)
    before = _fingerprint_triplet(source)
    validation_report = validate_daily(source)
    after = _fingerprint_triplet(source)
    all_present = all(item.exists for item in before) and all(item.exists for item in after)
    unchanged = before == after if all_present else None

    if not all_present:
        decision: Decision = "REJECT"
        missing = sorted(
            {item.path for item in before + after if not item.exists}
        )
        reasons = [f"source_triplet_incomplete: missing or unreadable {path}" for path in missing]
    elif unchanged is False:
        decision = "REJECT"
        reasons = ["source_changed_during_acceptance: triplet fingerprints changed"]
    elif not validation_report.publication_integrity.valid:
        decision = "REJECT"
        reasons = _issue_reasons(validation_report)
    elif not validation_report.ready_for_normalization:
        decision = "QUARANTINE"
        reasons = _issue_reasons(validation_report)
        if not reasons:
            reasons = [f"validation disposition: {validation_report.disposition}"]
    else:
        decision = "ACCEPT"
        reasons = [
            "publication integrity and ShoonyaDailyV1 content-quality checks passed"
        ]

    instant = evaluated_at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return AcceptanceReport(
        schema_version=1,
        evaluated_at_utc=instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        source=str(source),
        decision=decision,
        reasons=reasons,
        source_unchanged_during_run=unchanged,
        triplet_before=before,
        triplet_after=after,
        validation=validation_report.to_dict(),
    )


def render_summary(report: AcceptanceReport) -> str:
    """Render a stable, concise summary for a human reviewer."""
    validation = report.validation
    publication = validation["publication_integrity"]
    content = validation["content_quality"]
    assert isinstance(publication, dict) and isinstance(content, dict)
    records = publication.get("records")
    lines = [
        f"Decision: {report.decision}",
        f"Source: {report.source}",
        f"Triplet unchanged: {report.source_unchanged_during_run}",
        f"Publication integrity: {publication.get('status')}",
        f"Content quality: {content.get('status')}",
        f"Schema: {content.get('schema')}",
        f"Records: {records}",
        f"Input chronology: {content.get('chronology')}",
    ]
    lines.extend(f"Reason: {reason}" for reason in report.reasons)
    return "\n".join(lines) + "\n"


def write_acceptance_artifacts(
    report: AcceptanceReport, output_dir: Path
) -> tuple[Path, Path]:
    """Write new report files outside the raw segment directory; never overwrite."""
    output_dir = Path(output_dir).resolve()
    raw_dir = Path(report.source).resolve().parent
    if output_dir == raw_dir or raw_dir in output_dir.parents:
        raise ValueError("output directory must be outside the raw segment directory")

    source = Path(report.source)
    segment = source.name.removesuffix(".json.gz")
    prefix = f"{source.parent.name}_{segment}.acceptance"
    json_path = output_dir / f"{prefix}.json"
    summary_path = output_dir / f"{prefix}.txt"
    if json_path.exists() or summary_path.exists():
        raise FileExistsError("acceptance artifacts already exist; refusing to overwrite")

    output_dir.mkdir(parents=True, exist_ok=True)
    with json_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report.to_dict(), indent=2, allow_nan=False) + "\n")
    with summary_path.open("x", encoding="utf-8") as stream:
        stream.write(render_summary(report))
    return json_path, summary_path
