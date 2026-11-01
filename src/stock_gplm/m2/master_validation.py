"""Read-only validation for the observed Shoonya NSE instrument-master V1 file."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

CONTRACT = "stock-gplm.shoonya-nse-instrument-master.v1"
EXPECTED_MEMBER = "NSE_symbols.txt"
EXPECTED_HEADER = (
    "Exchange", "Token", "LotSize", "Symbol", "TradingSymbol", "Instrument",
    "TickSize", "",
)
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
IDENTIFIER = re.compile(r"^[A-Z0-9&.-]+$")


def read_shoonya_nse_eq_inventory_v1(path: Path) -> tuple[dict[str, str], ...]:
    """Return the exact EQ identities from a master that passes the V1 validator."""
    report = validate_shoonya_nse_master_v1(
        path, observed_at_utc="1970-01-01T00:00:00Z"
    )
    if report["decision"] != "ACCEPT":
        raise ValueError("instrument master must pass M2.2 validation")
    raw = Path(path).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        text = archive.read(EXPECTED_MEMBER).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return tuple(sorted((
        {
            "symbol": item["TradingSymbol"],
            "base_symbol": item["Symbol"],
            "token": item["Token"],
        }
        for item in reader
        if item["Exchange"] == "NSE" and item["Instrument"] == "EQ"
    ), key=lambda item: item["symbol"]))


def _issue(code: str, line: int | None, field: str, value: str, message: str) -> dict[str, Any]:
    return {"code": code, "line": line, "field": field, "value": value, "message": message}


def validate_shoonya_nse_master_v1(
    path: Path,
    *,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Validate an explicit local master without mutating it or asserting history."""
    path = Path(path)
    before = path.stat()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("instrument-master ZIP exceeds the configured archive limit")
    if observed_at_utc is None:
        observed_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise ValueError("instrument-master ZIP must contain exactly one file")
        member = members[0]
        member_path = PurePosixPath(member.filename)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError("instrument-master ZIP contains an unsafe member path")
        if member.filename != EXPECTED_MEMBER:
            raise ValueError(f"instrument-master member must be {EXPECTED_MEMBER}")
        if member.flag_bits & 1:
            raise ValueError("encrypted instrument-master ZIP is unsupported")
        if member.file_size > MAX_MEMBER_BYTES:
            raise ValueError("instrument-master member exceeds the configured size limit")
        if member.compress_size == 0 or member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
            raise ValueError("instrument-master ZIP compression ratio exceeds the limit")
        member_raw = archive.read(member)

    try:
        text = member_raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("instrument-master member must be UTF-8") from exc
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    if not parsed:
        raise ValueError("instrument-master member is empty")
    header = tuple(parsed[0])
    if header != EXPECTED_HEADER:
        raise ValueError(f"unsupported instrument-master header: {list(header)!r}")

    records: list[dict[str, str]] = []
    invalid: list[dict[str, Any]] = []
    for line, row in enumerate(parsed[1:], 2):
        if len(row) != len(EXPECTED_HEADER):
            invalid.append(_issue("INVALID_ROW_WIDTH", line, "row", str(len(row)), "expected 8 columns"))
            continue
        record = dict(zip(EXPECTED_HEADER[:-1], row[:-1], strict=True))
        records.append(record)
        if row[-1] != "":
            invalid.append(_issue("NONEMPTY_TRAILING_COLUMN", line, "", row[-1], "trailing column must be empty"))
        for field in ("Exchange", "Token", "LotSize", "Symbol", "TradingSymbol", "Instrument", "TickSize"):
            if not record[field]:
                invalid.append(_issue("EMPTY_REQUIRED_FIELD", line, field, record[field], "required value is empty"))
        if record["Exchange"] != "NSE":
            invalid.append(_issue("UNEXPECTED_EXCHANGE", line, "Exchange", record["Exchange"], "expected NSE"))
        if record["Token"] and not record["Token"].isdigit():
            invalid.append(_issue("INVALID_TOKEN", line, "Token", record["Token"], "token must contain digits only"))
        if record["LotSize"]:
            try:
                if int(record["LotSize"]) <= 0 or str(int(record["LotSize"])) != record["LotSize"]:
                    raise ValueError
            except ValueError:
                invalid.append(_issue("INVALID_LOT_SIZE", line, "LotSize", record["LotSize"], "lot size must be a positive integer"))
        if record["TickSize"]:
            try:
                tick = Decimal(record["TickSize"])
                if not tick.is_finite() or tick < 0:
                    raise InvalidOperation
            except InvalidOperation:
                invalid.append(_issue("INVALID_TICK_SIZE", line, "TickSize", record["TickSize"], "tick size must be finite and nonnegative"))
        if record["Instrument"] == "EQ":
            for field in ("Symbol", "TradingSymbol"):
                if record[field] and IDENTIFIER.fullmatch(record[field]) is None:
                    invalid.append(_issue("INVALID_EQ_IDENTIFIER", line, field, record[field], "unsupported EQ identifier characters"))
            expected = f'{record["Symbol"]}-EQ'
            if record["TradingSymbol"] != expected:
                invalid.append(_issue("INVALID_EQ_TRADING_SYMBOL", line, "TradingSymbol", record["TradingSymbol"], f"expected {expected}"))
            try:
                if Decimal(record["TickSize"]) <= 0:
                    invalid.append(_issue("INVALID_EQ_TICK_SIZE", line, "TickSize", record["TickSize"], "EQ tick size must be positive"))
            except InvalidOperation:
                pass

    def duplicates(field: str, subset: list[dict[str, str]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for record in subset:
            grouped[record[field]].append(record)
        return [
            {
                "value": value,
                "occurrences": len(items),
                "trading_symbols": [item["TradingSymbol"] for item in items],
                "instruments": [item["Instrument"] for item in items],
                "tokens": [item["Token"] for item in items],
            }
            for value, items in sorted(grouped.items()) if value and len(items) > 1
        ]

    eq = [item for item in records if item["Exchange"] == "NSE" and item["Instrument"] == "EQ"]
    duplicate_tokens = duplicates("Token", records)
    duplicate_trading = duplicates("TradingSymbol", records)
    duplicate_eq_symbols = duplicates("Symbol", eq)
    base_symbol_reuse = duplicates("Symbol", records)
    issue_counts = Counter(item["code"] for item in invalid)
    decision = "ACCEPT" if not invalid and not duplicate_tokens and not duplicate_trading and not duplicate_eq_symbols else "REJECT"

    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError("instrument-master source changed during validation")

    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "decision": decision,
        "source_integrity": {
            "decision": "ACCEPT",
            "path": str(path),
            "filename": path.name,
            "bytes": len(raw),
            "sha256": digest,
            "source_unchanged_during_validation": True,
        },
        "provenance": {
            "source": "Shoonya NSE instrument master",
            "source_reference": str(path),
            "supplied_by": "user local transfer",
            "publication_date": None,
            "acquisition_timestamp_utc": None,
            "effective_date": None,
            "observed_at_utc": observed_at_utc,
            "filesystem_mtime_utc": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            "filesystem_mtime_is_acquisition_evidence": False,
        },
        "archive": {
            "members": 1,
            "member": member.filename,
            "member_compressed_bytes": member.compress_size,
            "member_uncompressed_bytes": member.file_size,
            "member_crc32": f"{member.CRC:08x}",
            "encrypted": False,
        },
        "observed_schema": {
            "encoding": "UTF-8",
            "format": "CSV in ZIP",
            "header": list(header),
            "trailing_blank_column": True,
            "isin_column_present": "ISIN" in header,
        },
        "counts": {
            "records": len(records),
            "eq_instruments": len(eq),
            "unique_eq_symbols": len({item["Symbol"] for item in eq}),
            "unique_eq_trading_symbols": len({item["TradingSymbol"] for item in eq}),
            "unique_eq_tokens": len({item["Token"] for item in eq}),
            "available_eq_isins": 0,
            "unique_all_base_symbols": len({item["Symbol"] for item in records}),
            "unique_all_trading_symbols": len({item["TradingSymbol"] for item in records}),
            "unique_all_tokens": len({item["Token"] for item in records}),
            "available_all_isins": 0,
        },
        "instrument_type_counts": dict(sorted(Counter(item["Instrument"] for item in records).items())),
        "identifier_quality": {
            "decision": decision,
            "invalid_count": len(invalid),
            "invalid_by_code": dict(sorted(issue_counts.items())),
            "invalid_identifiers": invalid,
            "duplicate_tokens": duplicate_tokens,
            "duplicate_trading_symbols": duplicate_trading,
            "duplicate_eq_symbols": duplicate_eq_symbols,
            "nonunique_base_symbols_across_series": base_symbol_reuse,
        },
        "historical_membership": {
            "decision": "NOT_ESTABLISHED",
            "reason": "current instrument-master membership is not evidence of historical membership",
        },
        "reference_readiness": {
            "decision": "INCOMPLETE",
            "blockers": [
                "PUBLICATION_DATE_UNKNOWN",
                "ACQUISITION_TIMESTAMP_UNKNOWN",
                "EFFECTIVE_DATE_UNKNOWN",
                "ISIN_COLUMN_UNAVAILABLE",
                "HISTORICAL_MEMBERSHIP_NOT_ESTABLISHED",
            ],
        },
    }
