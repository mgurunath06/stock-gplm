"""Versioned adapter and quality checks for observed Shoonya one-minute bars.

``ShoonyaMinuteV1`` describes what the 2026-11-01 Oracle snapshot contains: a gzip
JSON array, newest bar first, of objects with twelve string fields. The meaning of
``intv`` as per-bar volume and ``v`` as cumulative volume is measured, not provider
documented, so ``v`` and ``intvwap`` are checked but never used as features.
Flagged values are kept unrepaired; a broken invariant rejects the whole segment.
A negative ``intv`` is genuine observed provider output whose meaning is unknown, so
it parses but quarantines the segment: visible, reconciled, and excluded from features.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from stock_gplm.ingestion.daily import _strict_json_loads

SCHEMA_NAME = "ShoonyaMinuteV1"
FIELDS = frozenset({
    "stat", "time", "ssboe", "into", "inth", "intl", "intc", "intvwap", "intv", "intoi", "v", "oi",
})
IST = timezone(timedelta(hours=5, minutes=30))
MAX_COMPRESSED_BYTES = 1024 * 1024
MAX_DECOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
MAX_RECORDS = 2000
VOLUME_SPIKE_MULTIPLE = 20
LARGE_BAR_MOVE = Decimal("0.05")

MARKER_FIELDS = frozenset({"status", "file", "bytes", "records", "sha256", "quality_status", "published_at_utc"})
META_JOBS_FIELDS = frozenset({
    "source", "symbol", "token", "session_date", "session_type", "quality_status", "expected_start",
    "expected_end", "sha256_compressed", "records", "fetched_at_utc",
})
META_LEGACY_FIELDS = frozenset({
    "symbol", "exchange", "instrument_token", "session_date", "interval_minutes", "retrieved_at_utc",
    "total_records", "unique_timestamps", "regular_session_unique_timestamps",
    "expected_regular_session_minutes", "off_session_timestamps", "duplicate_timestamps",
    "missing_regular_minutes", "zero_volume_candles", "qa_status", "format", "compressed_sha256",
})

_TIME = re.compile(r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}$")
_FIXED_POINT = re.compile(r"^\d+(?:\.\d+)?$")
_SIGNED_WHOLE = re.compile(r"^-?\d+$")
_CLOCK = re.compile(r"^\d{2}:\d{2}$")
# Flags that describe a bar without calling for review.
INFORMATIONAL_FLAGS = frozenset({"FLAT_OHLC"})


def is_session_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class MinuteSchemaError(ValueError):
    """A record, segment or sidecar is not the observed V1 contract."""


@dataclass(frozen=True)
class SessionPolicy:
    """First and last regular bar labels; only a supplied policy defines a session."""

    first_bar: str
    last_bar: str
    status: str
    provenance: str
    valid_through: str | None = None

    def __post_init__(self) -> None:
        if self.valid_through is not None and not is_session_date(self.valid_through):
            raise ValueError("valid_through must be an ISO date")
        if not (_CLOCK.fullmatch(self.first_bar) and _CLOCK.fullmatch(self.last_bar)):
            raise ValueError("session bars must be HH:MM")
        if self.first_bar >= self.last_bar:
            raise ValueError("session bars must be ascending")
        if self.status not in {"AUTHORITATIVE", "OBSERVED_NOT_AUTHORITATIVE"}:
            raise ValueError("unsupported session policy status")
        if not self.provenance.strip():
            raise ValueError("session policy requires provenance")

    @classmethod
    def load(cls, path: Path) -> "SessionPolicy":
        data = _strict_json_loads(Path(path).read_bytes())
        expected = {
            "schema_version", "exchange", "timezone", "regular_first_bar", "regular_last_bar",
            "status", "provenance", "valid_through",
        }
        if not isinstance(data, dict) or set(data) != expected or data["schema_version"] != 1:
            raise ValueError("unsupported session policy file")
        if data["exchange"] != "NSE" or data["timezone"] != "Asia/Kolkata":
            raise ValueError("session policy must describe NSE in Asia/Kolkata")
        return cls(
            data["regular_first_bar"], data["regular_last_bar"], data["status"], data["provenance"],
            data["valid_through"],
        )

    @property
    def regular_minutes(self) -> tuple[str, ...]:
        hour, minute = map(int, self.first_bar.split(":"))
        last_hour, last_minute = map(int, self.last_bar.split(":"))
        return tuple(
            f"{value // 60:02d}:{value % 60:02d}"
            for value in range(hour * 60 + minute, last_hour * 60 + last_minute + 1)
        )


@dataclass(frozen=True)
class MinuteBar:
    """One validated bar. The label is IST; ``start_utc`` comes from ``ssboe``."""

    start_ist: datetime
    start_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cumulative_volume: int
    interval_vwap: Decimal
    flags: tuple[str, ...] = ()

    @property
    def clock(self) -> str:
        return self.start_ist.strftime("%H:%M")


@dataclass
class SegmentReport:
    """Content decision for one symbol-session; never a statement about publication."""

    symbol: str
    session_date: str
    decision: str = "ACCEPT"
    records: int = 0
    regular_bars: int = 0
    off_session_bars: int = 0
    missing_regular_minutes: tuple[str, ...] = ()
    flag_counts: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    bars: tuple[MinuteBar, ...] = ()

    def reject(self, message: str) -> None:
        self.decision = "REJECT"
        self.issues.append(message)


def _number(value: str, name: str, *, positive: bool) -> Decimal:
    if _FIXED_POINT.fullmatch(value) is None:
        raise MinuteSchemaError(f"{name} must be an unsigned fixed-point string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise MinuteSchemaError(f"{name} is not a decimal") from exc
    if positive and number <= 0:
        raise MinuteSchemaError(f"{name} must be positive")
    return number


def _whole(value: str, name: str) -> int:
    number = _number(value, name, positive=False)
    if number != number.to_integral_value():
        raise MinuteSchemaError(f"{name} must be a whole number")
    return int(number)


def parse_record(record: Any) -> MinuteBar:
    """Validate one provider object; no value is coerced or repaired."""
    if not isinstance(record, dict) or frozenset(record) != FIELDS:
        raise MinuteSchemaError("record does not have the exact V1 fields")
    if any(not isinstance(value, str) for value in record.values()):
        raise MinuteSchemaError("all V1 values must be strings")
    if record["stat"] != "Ok":
        raise MinuteSchemaError("record stat is not Ok")
    if _TIME.fullmatch(record["time"]) is None or not record["ssboe"].isdigit():
        raise MinuteSchemaError("time or ssboe has an unexpected format")
    try:
        local = datetime.strptime(record["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
        start_utc = datetime.fromtimestamp(int(record["ssboe"]), timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise MinuteSchemaError("time or ssboe is not a valid instant") from exc
    if local != start_utc:
        raise MinuteSchemaError("time read as IST disagrees with ssboe")
    if local.second:
        raise MinuteSchemaError("bar does not fall on a whole minute")
    opening = _number(record["into"], "into", positive=True)
    high = _number(record["inth"], "inth", positive=True)
    low = _number(record["intl"], "intl", positive=True)
    close = _number(record["intc"], "intc", positive=True)
    if not low <= min(opening, close) <= max(opening, close) <= high:
        raise MinuteSchemaError("OHLC bounds are inconsistent")
    if _SIGNED_WHOLE.fullmatch(record["intv"]) is None:
        raise MinuteSchemaError("intv must be a whole number")
    _whole(record["intoi"], "intoi")
    _whole(record["oi"], "oi")
    return MinuteBar(
        local, start_utc, opening, high, low, close, int(record["intv"]),
        _whole(record["v"], "v"), _number(record["intvwap"], "intvwap", positive=False),
    )


def decompress_payload(payload: bytes) -> bytes:
    if len(payload) > MAX_COMPRESSED_BYTES:
        raise MinuteSchemaError("compressed payload exceeds its bound")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            data = stream.read(MAX_DECOMPRESSED_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise MinuteSchemaError("payload is not valid gzip") from exc
    if len(data) > MAX_DECOMPRESSED_BYTES:
        raise MinuteSchemaError("decompressed payload exceeds its bound")
    return data


def _flag_bars(bars: list[MinuteBar], policy: SessionPolicy) -> list[MinuteBar]:
    regular = set(policy.regular_minutes)
    active = [item.volume for item in bars if item.clock in regular and item.volume > 0]
    typical = median(active) if active else 0
    result: list[MinuteBar] = []
    for index, bar in enumerate(bars):
        flags: list[str] = []
        if bar.clock not in regular:
            flags.append("OFF_SESSION")
        if bar.volume == 0:
            flags.append("ZERO_VOLUME")
        elif bar.volume < 0:
            flags.append("NEGATIVE_VOLUME")
        if bar.open == bar.high == bar.low == bar.close:
            flags.append("FLAT_OHLC")
        if typical and bar.volume >= VOLUME_SPIKE_MULTIPLE * typical:
            flags.append("VOLUME_SPIKE")
        if bar.interval_vwap and not bar.low <= bar.interval_vwap <= bar.high:
            flags.append("VWAP_OUTSIDE_RANGE")
        if index == 0:
            if bar.cumulative_volume < bar.volume:
                flags.append("CUMULATIVE_VOLUME_BREAK")
        else:
            previous = bars[index - 1]
            if bar.cumulative_volume - previous.cumulative_volume != bar.volume:
                flags.append("CUMULATIVE_VOLUME_BREAK")
            if abs(bar.close / previous.close - 1) >= LARGE_BAR_MOVE:
                flags.append("LARGE_BAR_MOVE")
        result.append(MinuteBar(
            bar.start_ist, bar.start_utc, bar.open, bar.high, bar.low, bar.close, bar.volume,
            bar.cumulative_volume, bar.interval_vwap, tuple(flags),
        ))
    return result


def validate_segment(payload: bytes, symbol: str, session_date: str, policy: SessionPolicy) -> SegmentReport:
    """Return an ascending validated session, or a REJECT that names every failure."""
    report = SegmentReport(symbol, session_date)
    try:
        records = _strict_json_loads(decompress_payload(payload))
    except (MinuteSchemaError, ValueError) as exc:
        report.reject(f"UNREADABLE_PAYLOAD: {exc}")
        return report
    if not isinstance(records, list) or not records:
        report.reject("PAYLOAD_NOT_A_NONEMPTY_ARRAY")
        return report
    if len(records) > MAX_RECORDS:
        report.reject("TOO_MANY_RECORDS")
        return report
    report.records = len(records)
    parsed: list[MinuteBar] = []
    for index, record in enumerate(records):
        try:
            parsed.append(parse_record(record))
        except MinuteSchemaError as exc:
            report.reject(f"INVALID_RECORD[{index}]: {exc}")
    if report.decision == "REJECT":
        return report
    stamps = [item.start_utc for item in parsed]
    if len(set(stamps)) != len(stamps):
        report.reject("DUPLICATE_BAR_TIMESTAMP")
    elif any(left <= right for left, right in zip(stamps, stamps[1:])):
        report.reject("RECORDS_NOT_STRICTLY_DESCENDING")
    if any(item.start_ist.date().isoformat() != session_date for item in parsed):
        report.reject("BAR_OUTSIDE_SEGMENT_DATE")
    if report.decision == "REJECT":
        return report
    bars = _flag_bars(sorted(parsed, key=lambda item: item.start_utc), policy)
    observed = {item.clock for item in bars}
    report.bars = tuple(bars)
    report.off_session_bars = sum("OFF_SESSION" in item.flags for item in bars)
    report.regular_bars = len(bars) - report.off_session_bars
    report.missing_regular_minutes = tuple(item for item in policy.regular_minutes if item not in observed)
    counts: dict[str, int] = {}
    for bar in bars:
        for flag in bar.flags:
            counts[flag] = counts.get(flag, 0) + 1
    report.flag_counts = dict(sorted(counts.items()))
    if report.missing_regular_minutes or set(counts) - INFORMATIONAL_FLAGS:
        report.decision = "REVIEW"
    if "NEGATIVE_VOLUME" in counts:
        report.decision = "QUARANTINE"
        report.issues.append("NEGATIVE_INTERVAL_VOLUME_MEANING_UNKNOWN")
    if policy.valid_through is not None and session_date > policy.valid_through:
        report.decision = "QUARANTINE"
        report.issues.append("SESSION_POLICY_NOT_ESTABLISHED_FOR_DATE")
    return report


def _object(data: bytes, label: str) -> dict[str, Any]:
    if len(data) > MAX_SIDECAR_BYTES:
        raise MinuteSchemaError(f"{label} exceeds its bound")
    try:
        value = _strict_json_loads(data)
    except ValueError as exc:
        raise MinuteSchemaError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise MinuteSchemaError(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class Publication:
    meta_variant: str
    token: str
    sha256: str
    records: int
    bytes: int
    fetched_at_utc: str
    marker_quality_status: str


def verify_publication(payload: bytes, meta: bytes, marker: bytes, symbol: str, session_date: str) -> Publication:
    """Prove a triplet is internally consistent; this says nothing about its candles."""
    done = _object(marker, "marker")
    if frozenset(done) != MARKER_FIELDS or done["status"] != "COMPLETE":
        raise MinuteSchemaError("marker is not a COMPLETE publication")
    digest = hashlib.sha256(payload).hexdigest()
    if done["file"] != f"{session_date}.json.gz":
        raise MinuteSchemaError("marker names another file")
    if done["sha256"] != digest or done["bytes"] != len(payload):
        raise MinuteSchemaError("payload does not match its marker")
    records = json.loads(decompress_payload(payload))
    if not isinstance(records, list) or done["records"] != len(records):
        raise MinuteSchemaError("record count does not match the marker")
    sidecar = _object(meta, "metadata")
    fields = frozenset(sidecar)
    if fields == META_JOBS_FIELDS:
        variant, token, sha, count, fetched = (
            "GPLM_JOBS_V1", sidecar["token"], sidecar["sha256_compressed"], sidecar["records"],
            sidecar["fetched_at_utc"],
        )
        if sidecar["source"] != "shoonya":
            raise MinuteSchemaError("metadata names another source")
    elif fields == META_LEGACY_FIELDS:
        variant, token, sha, count, fetched = (
            "COLLECTOR_V01", sidecar["instrument_token"], sidecar["compressed_sha256"],
            sidecar["total_records"], sidecar["retrieved_at_utc"],
        )
        if sidecar["exchange"] != "NSE" or sidecar["interval_minutes"] != 1:
            raise MinuteSchemaError("metadata names another exchange or interval")
    else:
        raise MinuteSchemaError("metadata is neither observed variant")
    if sidecar["symbol"] != symbol or sidecar["session_date"] != session_date:
        raise MinuteSchemaError("metadata names another symbol or session")
    if sha != digest or count != len(records):
        raise MinuteSchemaError("payload does not match its metadata")
    return Publication(variant, str(token), digest, len(records), len(payload), fetched, done["quality_status"])
