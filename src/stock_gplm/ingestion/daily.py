"""Read-only validation for Oracle-published Shoonya daily segments."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from .shoonya_daily_v1 import Candle, ShoonyaDailyV1, UnsupportedShoonyaDailySchema

MAX_COMPRESSED = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_SIDECAR = 64 * 1024

_SEGMENT_NAME = re.compile(
    r"^(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.json\.gz$"
)

Status = Literal["PASS", "FAIL", "NOT_RUN"]


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    record: int | None = None


@dataclass
class PublicationIntegrity:
    status: Status = "FAIL"
    sha256: str | None = None
    compressed_bytes: int | None = None
    records: int | None = None
    metadata_quality_status: str | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.status == "PASS" and not self.issues


@dataclass
class ContentQuality:
    status: Status = "NOT_RUN"
    schema: str = "unrecognized"
    schema_confirmed_from_real_sample: bool = False
    timestamp_policy: str | None = None
    records_seen: int = 0
    chronology: str = "undetermined"
    normalized_candles: list[Candle] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.status == "PASS" and not self.issues


@dataclass
class QualityReport:
    source: str
    symbol: str | None = None
    segment_start: str | None = None
    segment_end: str | None = None
    publication_integrity: PublicationIntegrity = field(default_factory=PublicationIntegrity)
    content_quality: ContentQuality = field(default_factory=ContentQuality)

    @property
    def ready_for_normalization(self) -> bool:
        return (
            self.publication_integrity.valid
            and self.content_quality.valid
            and self.content_quality.schema_confirmed_from_real_sample
        )

    @property
    def disposition(self) -> str:
        if not self.publication_integrity.valid:
            return "REJECT_PUBLICATION"
        if not self.content_quality.schema_confirmed_from_real_sample:
            return "QUARANTINE_UNVERIFIED_SCHEMA"
        if not self.content_quality.valid:
            return "QUARANTINE_CONTENT"
        return "ACCEPT"

    def to_dict(self) -> dict[str, Any]:
        result = _json_compatible(asdict(self))
        result.update(
            publication_integrity_valid=self.publication_integrity.valid,
            content_quality_valid=self.content_quality.valid,
            ready_for_normalization=self.ready_for_normalization,
            disposition=self.disposition,
            missing_history=(
                "not assessed; absence is not a zero-volume candle and current "
                "instrument masters do not prove historical membership"
            ),
        )
        return result


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds {limit} bytes")
    return data


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        result[key] = value
    return result


def _strict_json_loads(value: bytes | str) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    value = _strict_json_loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_exact(obj: dict[str, Any], key: str, expected: Any, label: str) -> None:
    actual = obj.get(key)
    if type(actual) is not type(expected) or actual != expected:
        raise ValueError(f"{label}.{key} must equal {expected!r}")


def _segment_dates(path: Path) -> tuple[date, date]:
    match = _SEGMENT_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError("filename must be START_DATE_END_DATE.json.gz")
    start = date.fromisoformat(match.group("start"))
    end = date.fromisoformat(match.group("end"))
    if start > end:
        raise ValueError("filename start date is after end date")
    return start, end


def _meta_path(path: Path) -> Path:
    return path.with_name(f"{path.name.removesuffix('.json.gz')}.meta.json")


def _validate_content(
    rows: list[Any], report: ContentQuality, segment_start: date, segment_end: date
) -> None:
    report.records_seen = len(rows)
    decoded_rows: list[dict[str, str]] = []
    for index, raw_row in enumerate(rows):
        try:
            decoded_rows.append(ShoonyaDailyV1.decode_structure(raw_row))
        except UnsupportedShoonyaDailySchema as exc:
            report.issues.append(Issue("unsupported_schema", str(exc), index))
    if report.issues:
        report.status = "FAIL"
        return

    report.schema = ShoonyaDailyV1.schema_name
    report.schema_confirmed_from_real_sample = True
    report.timestamp_policy = ShoonyaDailyV1.timestamp_policy
    seen_dates: set[date] = set()
    ordered_dates: list[date] = []
    candidates: list[Candle] = []
    for index, row in enumerate(decoded_rows):
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
        report.issues.append(Issue("chronology", "session dates change direction"))
    report.status = "FAIL" if report.issues else "PASS"
    if report.status == "PASS":
        report.normalized_candles = sorted(candidates, key=lambda candle: candle.trading_date)


def validate_daily(path: Path) -> QualityReport:
    """Validate one local segment and its sidecars without writing any input."""
    path = Path(path)
    report = QualityReport(source=str(path), symbol=path.parent.name or None)
    rows: list[Any]
    try:
        start, end = _segment_dates(path)
        report.segment_start = start.isoformat()
        report.segment_end = end.isoformat()
        raw = _read(path, MAX_COMPRESSED)
        digest = hashlib.sha256(raw).hexdigest()
        byte_count = len(raw)
        report.publication_integrity.sha256 = digest
        report.publication_integrity.compressed_bytes = byte_count
        metadata = _json_object(_read(_meta_path(path), MAX_SIDECAR), "metadata")
        manifest = _json_object(_read(Path(f"{path}.done"), MAX_SIDECAR), "manifest")
        _required_exact(manifest, "status", "COMPLETE", "manifest")
        _required_exact(manifest, "file", path.name, "manifest")
        _required_exact(manifest, "sha256", digest, "manifest")
        _required_exact(manifest, "bytes", byte_count, "manifest")
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            expanded = stream.read(MAX_EXPANDED + 1)
        if len(expanded) > MAX_EXPANDED:
            raise ValueError(f"decompressed payload exceeds {MAX_EXPANDED} bytes")
        rows = _strict_json_loads(expanded)
        if not isinstance(rows, list) or not rows:
            raise ValueError("decompressed payload must be a nonempty JSON list")
        if len(rows) > MAX_RECORDS:
            raise ValueError(f"payload exceeds {MAX_RECORDS} records")
        _required_exact(manifest, "records", len(rows), "manifest")
        if "records" in metadata:
            _required_exact(metadata, "records", len(rows), "metadata")
        if "sha256_compressed" in metadata:
            _required_exact(metadata, "sha256_compressed", digest, "metadata")
        quality_status = metadata.get("quality_status")
        if quality_status is not None and not isinstance(quality_status, str):
            raise ValueError("metadata.quality_status must be a string when present")
        report.publication_integrity.records = len(rows)
        report.publication_integrity.metadata_quality_status = quality_status
        report.publication_integrity.status = "PASS"
    except (
        json.JSONDecodeError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        EOFError,
        zlib.error,
    ) as exc:
        report.publication_integrity.issues.append(Issue("publication_integrity", str(exc)))
        return report
    _validate_content(rows, report.content_quality, start, end)
    return report
