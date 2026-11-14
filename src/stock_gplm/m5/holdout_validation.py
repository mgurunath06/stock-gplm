"""M5-only validation that does not inspect protected candle values.

Whole-file publication integrity remains bound to the immutable payload and its
sidecars.  For a segment that straddles the locked-holdout boundary, however,
only rows whose date is before the boundary are decoded as ShoonyaDailyV1
candles and content-validated.  Protected rows are retained as opaque source
bytes and can affect only the full-source integrity hashes.

This deliberately does not change :func:`stock_gplm.ingestion.daily.validate_daily`
for its M1/M2 callers.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zlib
from datetime import date
from pathlib import Path
from typing import Any

from stock_gplm.ingestion import daily
from stock_gplm.ingestion.daily import ContentQuality, Issue, QualityReport
from stock_gplm.ingestion.shoonya_daily_v1 import ShoonyaDailyV1, UnsupportedShoonyaDailySchema

_DATE = re.compile(r"^(?P<day>\d{2})-(?P<month>[A-Z]{3})-(?P<year>\d{4})$")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _row_date(row: dict[str, str]) -> date:
    """Read only the session date needed to enforce the isolation boundary."""
    value = row["time"]
    match = _DATE.fullmatch(value)
    if match is None or match["month"] not in _MONTHS:
        raise ValueError("time is not a supported session date")
    return date(int(match["year"]), _MONTHS[match["month"]], int(match["day"]))


def _validate_development_rows(
    rows: list[Any], report: ContentQuality, segment_start: date, segment_end: date, cutoff: date
) -> None:
    """Validate candle content only for structurally dated development rows.

    An undecodable row in a straddling segment is kept opaque.  In strict mode a
    missing development session is independently caught by the registered
    calendar gate; the opaque row never becomes a candle.  No protected date,
    value or validation error is emitted into the decision or ordinary logs.
    """
    development: list[tuple[int, dict[str, str]]] = []
    for index, raw_row in enumerate(rows):
        try:
            decoded = ShoonyaDailyV1.decode_structure(raw_row)
            session_date = _row_date(decoded)
        except (KeyError, TypeError, ValueError, UnsupportedShoonyaDailySchema):
            # The row cannot safely be assigned to the development period.  It
            # remains covered by the payload/marker byte-integrity checks only.
            continue
        if session_date < cutoff:
            development.append((index, decoded))

    report.records_seen = len(development)
    report.schema = ShoonyaDailyV1.schema_name
    report.schema_confirmed_from_real_sample = True
    report.timestamp_policy = ShoonyaDailyV1.timestamp_policy
    seen_dates: set[date] = set()
    ordered_dates: list[date] = []
    candidates = []
    for index, row in development:
        try:
            candle = ShoonyaDailyV1.parse_decoded(row)
            session_date = date.fromisoformat(candle.trading_date)
            ordered_dates.append(session_date)
            if not segment_start <= session_date <= segment_end:
                raise ValueError("session date is outside the filename segment range")
            if session_date in seen_dates:
                report.issues.append(Issue("duplicate_date", session_date.isoformat(), index))
            seen_dates.add(session_date)
            candidates.append(candle)
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            report.issues.append(Issue("invalid_record", str(exc), index))

    signs = {
        1 if later > earlier else -1
        for earlier, later in zip(ordered_dates, ordered_dates[1:])
        if later != earlier
    }
    if signs == {1}:
        report.chronology = "ascending"
    elif signs == {-1}:
        report.chronology = "descending"
    elif len(signs) > 1:
        report.chronology = "mixed"
        report.issues.append(Issue("chronology", "development session dates change direction"))
    report.status = "FAIL" if report.issues else "PASS"
    if report.status == "PASS":
        report.normalized_candles = sorted(candidates, key=lambda candle: candle.trading_date)


def validate_daily_for_development(path: Path, cutoff: str) -> QualityReport:
    """Validate an immutable segment without decoding protected candle values."""
    path = Path(path)
    boundary = date.fromisoformat(cutoff)
    start, end = daily._segment_dates(path)
    if end < boundary:
        return daily.validate_daily(path)

    report = QualityReport(
        source=str(path), symbol=path.parent.name or None,
        segment_start=start.isoformat(), segment_end=end.isoformat(),
    )
    try:
        raw = daily._read(path, daily.MAX_COMPRESSED)
        digest = hashlib.sha256(raw).hexdigest()
        byte_count = len(raw)
        report.publication_integrity.sha256 = digest
        report.publication_integrity.compressed_bytes = byte_count
        metadata = daily._json_object(daily._read(daily._meta_path(path), daily.MAX_SIDECAR), "metadata")
        manifest = daily._json_object(daily._read(Path(f"{path}.done"), daily.MAX_SIDECAR), "manifest")
        daily._required_exact(manifest, "status", "COMPLETE", "manifest")
        daily._required_exact(manifest, "file", path.name, "manifest")
        daily._required_exact(manifest, "sha256", digest, "manifest")
        daily._required_exact(manifest, "bytes", byte_count, "manifest")
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            expanded = stream.read(daily.MAX_EXPANDED + 1)
        if len(expanded) > daily.MAX_EXPANDED:
            raise ValueError(f"decompressed payload exceeds {daily.MAX_EXPANDED} bytes")
        rows = daily._strict_json_loads(expanded)
        if not isinstance(rows, list) or not rows:
            raise ValueError("decompressed payload must be a nonempty JSON list")
        if len(rows) > daily.MAX_RECORDS:
            raise ValueError(f"payload exceeds {daily.MAX_RECORDS} records")
        daily._required_exact(manifest, "records", len(rows), "manifest")
        if "records" in metadata:
            daily._required_exact(metadata, "records", len(rows), "metadata")
        if "sha256_compressed" in metadata:
            daily._required_exact(metadata, "sha256_compressed", digest, "metadata")
        quality_status = metadata.get("quality_status")
        if quality_status is not None and not isinstance(quality_status, str):
            raise ValueError("metadata.quality_status must be a string when present")
        report.publication_integrity.records = len(rows)
        report.publication_integrity.metadata_quality_status = quality_status
        report.publication_integrity.status = "PASS"
    except (json.JSONDecodeError, OSError, OverflowError, TypeError, ValueError, EOFError, zlib.error) as exc:
        report.publication_integrity.issues.append(Issue("publication_integrity", str(exc)))
        return report
    _validate_development_rows(rows, report.content_quality, start, end, boundary)
    return report
