"""Fail-closed M5 validation at the locked-holdout boundary (M5.1.2, finding H).

Shoonya daily segments are gzip-compressed JSON arrays. A record's date cannot be
learned without decompressing and parsing the payload, so dates inside a payload
can never be used to decide whether that payload may be decoded. Partitioning is
decided only from date-boundary metadata that is checked *before* the payload is
opened:

1. the immutable publication filename ``START_END.json.gz``; and
2. the independently written ``.meta.json`` sidecar, whose ``from``/``to`` and
   ``symbol`` must equal the filename window and directory exactly.

A segment is opened only when both agree and the complete declared window ends
strictly before the cutoff. Missing, malformed or inconsistent partition
metadata, and any segment whose declared window reaches the cutoff, is rejected
without its payload being read. Straddling segments therefore require physical
republication as separate immutable pre-cutoff and protected files.

Every row of an opened development segment goes through the unchanged
:func:`stock_gplm.ingestion.daily.validate_daily`. A malformed row, or a row dated
outside the declared window, fails the whole segment explicitly; nothing is
skipped. Residual limit: if a publisher mislabels a protected row inside a
pre-cutoff window, that row is decoded while the inconsistency is detected and the
segment is rejected. The isolation guarantee is about payloads declared protected
by validated metadata, not about bytes that were published under a false label.

This does not change :func:`~stock_gplm.ingestion.daily.validate_daily` for its
M1/M2 callers.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from stock_gplm.ingestion import daily
from stock_gplm.ingestion.daily import Issue, QualityReport

PARTITION_POLICY = "DECLARED_WINDOW_STRICTLY_BEFORE_CUTOFF_VERIFIED_BEFORE_OPEN"


def _rejected(path: Path, code: str, message: str, start: date | None = None,
              end: date | None = None) -> QualityReport:
    report = QualityReport(source=str(path), symbol=path.parent.name or None,
                           segment_start=start.isoformat() if start else None,
                           segment_end=end.isoformat() if end else None)
    report.publication_integrity.issues.append(Issue(code, message))
    return report


def partition_window(path: Path) -> tuple[date, date]:
    """Return the validated declared window of ``path`` without reading its payload.

    Raises :class:`ValueError` when the filename or ``.meta.json`` partition metadata
    is missing, malformed or inconsistent.
    """
    path = Path(path)
    start, end = daily._segment_dates(path)
    meta_path = daily._meta_path(path)
    if meta_path.is_symlink() or not meta_path.is_file():
        raise ValueError("partition metadata sidecar is missing")
    metadata = daily._json_object(daily._read(meta_path, daily.MAX_SIDECAR), "metadata")
    for key in ("from", "to"):
        if key not in metadata:
            raise ValueError(f"partition metadata lacks {key!r}")
        if not isinstance(metadata[key], str):
            raise ValueError(f"partition metadata {key!r} must be an ISO date string")
    if (metadata["from"], metadata["to"]) != (start.isoformat(), end.isoformat()):
        raise ValueError("partition metadata window disagrees with the publication filename")
    if metadata.get("symbol") != path.parent.name:
        raise ValueError("partition metadata symbol disagrees with the publication directory")
    return start, end


def validate_daily_for_development(path: Path, cutoff: str) -> QualityReport:
    """Validate only a metadata-verified pre-cutoff segment; reject all others unopened."""
    path = Path(path)
    boundary = date.fromisoformat(cutoff)
    try:
        start, end = partition_window(path)
    except (OSError, ValueError, TypeError) as exc:
        return _rejected(path, "partition_metadata",
                         f"payload was not opened: untrustworthy partition metadata: {exc}")
    if end >= boundary:
        return _rejected(path, "holdout_partition_required",
                         f"segment window reaches locked holdout {boundary.isoformat()}; payload was not opened; "
                         "republish immutable development and protected dates as physically separate segments",
                         start, end)
    return daily.validate_daily(path)
