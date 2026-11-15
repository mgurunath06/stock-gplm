"""M2.3 deterministic, read-only inventory of local historical publications."""
from __future__ import annotations

import hashlib
from stock_gplm import locking as fcntl
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, TextIO

from stock_gplm.ingestion import validate_daily

from .models import CorporateActionCatalog, HorizonRequest, NSETradingCalendar
from .reference_data import (
    ImportedCorporateActionsV1,
    ImportedHistoricalSymbolMappingsV1,
    ImportedListingHistoryV1,
    ImportedProviderUnavailabilityV1,
)

MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
REPORT_SCHEMA_VERSION = 1


def _active_m24_lock(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


@dataclass(frozen=True)
class InventoryRequestV1:
    horizons: tuple[HorizonRequest, ...]
    schema_version: int = 1

    @classmethod
    def load(cls, path: Path) -> "InventoryRequestV1":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "horizons"}:
            raise ValueError("inventory request must contain schema_version,horizons")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported inventory-request schema_version")
        if not isinstance(value["horizons"], list) or not value["horizons"]:
            raise ValueError("inventory request requires at least one horizon")
        horizons = tuple(HorizonRequest.from_dict(item) for item in value["horizons"])
        names = [item.name for item in horizons]
        if len(names) != len(set(names)):
            raise ValueError("inventory request contains duplicate horizon names")
        return cls(horizons)


def _json_line(stream: TextIO, value: dict[str, Any]) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    stream.write("\n")
    stream.flush()


def _sha256(path: Path, limit: int | None = None) -> str:
    if limit is not None and path.stat().st_size > limit:
        raise ValueError(f"input exceeds configured checksum limit: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _days(start: str, end: str) -> Iterable[date]:
    cursor = date.fromisoformat(start)
    final = date.fromisoformat(end)
    while cursor <= final:
        yield cursor
        cursor += timedelta(days=1)


def _membership(record: Any | None, day: date) -> str:
    if record is None:
        return "UNKNOWN"
    listed = date.fromisoformat(record.listing_date)
    if day < listed:
        return "PRE_LISTING"
    if record.delisting_date is not None:
        return "DELISTED" if day > date.fromisoformat(record.delisting_date) else "LISTED"
    if (
        record.membership_verified_through is not None
        and day <= date.fromisoformat(record.membership_verified_through)
    ):
        return "LISTED"
    return "UNKNOWN"


def _artifact_files(raw_root: Path) -> list[Path]:
    files: list[Path] = []
    for child in sorted(raw_root.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            continue
        if child.is_dir():
            for item in sorted(child.iterdir(), key=lambda entry: entry.name):
                if item.is_file() and not item.is_symlink():
                    files.append(item)
        elif child.is_file():
            files.append(child)
    return files


def _raw_structure(raw_root: Path) -> list[dict[str, str]]:
    """Bind ignored names and symlinks without following them."""
    structure: list[dict[str, str]] = []
    for child in sorted(raw_root.iterdir(), key=lambda item: item.name):
        child_type = "symlink" if child.is_symlink() else "directory" if child.is_dir() else "file"
        structure.append({"path": child.name, "type": child_type})
        if child_type == "directory":
            for item in sorted(child.iterdir(), key=lambda entry: entry.name):
                item_type = "symlink" if item.is_symlink() else "directory" if item.is_dir() else "file"
                structure.append({"path": _relative(item, raw_root), "type": item_type})
    return structure


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _artifact_limit(path: Path) -> int:
    return MAX_SEGMENT_BYTES if path.name.endswith(".json.gz") else MAX_SIDECAR_BYTES


def _segment_range(report: Any) -> tuple[date, date] | None:
    if report.segment_start is None or report.segment_end is None:
        return None
    return date.fromisoformat(report.segment_start), date.fromisoformat(report.segment_end)


def _contains(bounds: tuple[date, date] | None, day: date) -> bool:
    return bounds is not None and bounds[0] <= day <= bounds[1]


def _candle_signature(candle: Any) -> tuple[Any, ...]:
    return (
        candle.source_timestamp_utc, candle.open, candle.high, candle.low,
        candle.close, candle.volume,
    )


def _reference_required(membership: str, calendar_status: str) -> tuple[str, ...]:
    missing: list[str] = []
    if membership == "UNKNOWN":
        missing.append("AUTHORITATIVE_LISTING_HISTORY_REQUIRED")
    if calendar_status == "UNKNOWN":
        missing.append("AUTHORITATIVE_CALENDAR_REQUIRED")
    return tuple(missing)


def _historical_state(
    *, day: date, observed: bool, membership: str, calendar_status: str,
    outages: tuple[Any, ...],
) -> tuple[str, tuple[str, ...]]:
    reference_blockers = _reference_required(membership, calendar_status)
    if observed:
        if membership == "LISTED" and calendar_status in {"TRADING_SESSION", "SPECIAL_SESSION"}:
            return "OBSERVED", ()
        return "AUTHORITATIVE_REFERENCE_REQUIRED", reference_blockers or (
            f"OBSERVED_WITH_MEMBERSHIP_{membership}",
            f"OBSERVED_WITH_CALENDAR_{calendar_status}",
        )
    if membership == "PRE_LISTING":
        return "PRE_LISTING", ()
    if calendar_status == "MARKET_CLOSED":
        return "MARKET_CLOSED", ()
    if membership == "LISTED" and calendar_status in {"TRADING_SESSION", "SPECIAL_SESSION"}:
        outage = next((item for item in outages if item.contains(day)), None)
        if outage is not None:
            return "PROVIDER_UNAVAILABLE", ("AUTHORITATIVE_PROVIDER_OUTAGE",)
        return "MISSING_EXPECTED_SESSION", ("MISSING_EXPECTED_SESSION",)
    return "AUTHORITATIVE_REFERENCE_REQUIRED", reference_blockers or (
        f"MEMBERSHIP_{membership}", f"CALENDAR_{calendar_status}",
    )


def _write_interval(stream: TextIO, current: dict[str, Any] | None) -> None:
    if current is not None:
        _json_line(stream, current)


def run_inventory_v1(
    *,
    master_path: Path,
    master_sha256: str,
    eq_inventory: tuple[dict[str, str], ...],
    raw_root: Path,
    request_path: Path,
    output_root: Path,
    expected_eq_count: int,
    calendar: NSETradingCalendar | None = None,
    listing_history: ImportedListingHistoryV1 | None = None,
    mappings: ImportedHistoricalSymbolMappingsV1 | None = None,
    actions: ImportedCorporateActionsV1 | None = None,
    unavailability: ImportedProviderUnavailabilityV1 | None = None,
    reference_files: tuple[Path, ...] = (),
) -> Path:
    """Build one immutable run without retaining full-universe report rows in memory."""
    master_path = Path(master_path).resolve()
    raw_root = Path(raw_root).resolve()
    request_path = Path(request_path).resolve()
    output_root = Path(output_root).resolve()
    sync_lock = raw_root / ".stock-gplm-m24.lock"
    if _active_m24_lock(sync_lock):
        raise RuntimeError("M2.3 inventory refused an active M2.4 publication lock")
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("raw root must be a real directory")
    if output_root == raw_root or raw_root in output_root.parents:
        raise ValueError("output root must be outside the raw tree")
    if len(eq_inventory) != expected_eq_count:
        raise ValueError(
            f"EQ universe count {len(eq_inventory)} does not equal explicit expected count {expected_eq_count}"
        )
    symbols = [item["symbol"] for item in eq_inventory]
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("EQ inventory must be sorted and unique")
    request_sha256 = _sha256(request_path, MAX_SIDECAR_BYTES)
    request = InventoryRequestV1.load(request_path)
    if _sha256(master_path, MAX_SEGMENT_BYTES) != master_sha256:
        raise ValueError("supplied master checksum does not match master bytes")
    calendar = calendar or NSETradingCalendar(days=())
    listing_by_symbol = {
        item.canonical_symbol: item for item in (listing_history.records if listing_history else ())
    }
    mapping_symbols = {
        item.canonical_symbol for item in (mappings.records if mappings else ())
    }
    action_items = actions.catalogs if actions else ()
    catalogs = {item.symbol: item for item in action_items}
    outages = unavailability.outages if unavailability else ()
    universe = set(symbols)
    for label, referenced in (
        ("listing history", set(listing_by_symbol)),
        ("historical mappings", mapping_symbols),
        ("corporate actions", set(catalogs)),
    ):
        unknown = sorted(referenced - universe)
        if unknown:
            raise ValueError(f"{label} reference symbols outside current EQ universe: {', '.join(unknown)}")
    if len(catalogs) != len(action_items):
        raise ValueError("corporate-action catalogs contain duplicate symbols")

    files = _artifact_files(raw_root)
    raw_structure_hash = _canonical_hash(_raw_structure(raw_root))
    input_entries: list[dict[str, Any]] = [
        {"role": "instrument_master", "path": master_path.name, "sha256": master_sha256},
        {"role": "inventory_request", "path": request_path.name, "sha256": request_sha256},
    ]
    input_entries.append({
        "role": "raw_structure", "path": ".",
        "sha256": raw_structure_hash,
    })
    source_checks: list[tuple[Path, str]] = [(master_path, master_sha256)]
    source_checks.append((request_path, request_sha256))
    for path in sorted((Path(item).resolve() for item in reference_files), key=str):
        digest = _sha256(path, MAX_SEGMENT_BYTES)
        input_entries.append({"role": "reference", "path": path.name, "sha256": digest})
        source_checks.append((path, digest))
    for path in files:
        digest = _sha256(path, _artifact_limit(path))
        input_entries.append({"role": "raw_artifact", "path": _relative(path, raw_root), "sha256": digest})
        source_checks.append((path, digest))
    input_entries.sort(key=lambda item: (item["role"], item["path"], item["sha256"]))
    run_binding = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "contract": "stock-gplm.m23-inventory-run.v1",
        "expected_eq_count": expected_eq_count,
        "inputs": input_entries,
    }
    run_id = _canonical_hash(run_binding)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise FileExistsError(f"inventory run already exists: {run_id}")
    output_root.mkdir(parents=True, exist_ok=True)
    partial_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    counts: dict[str, int] = {
        "symbols": 0, "zero_artifact_symbols": 0, "segments": 0,
        "intervals": 0, "exceptions": 0, "symbol_horizons": 0,
        "publication_accept": 0, "publication_incomplete": 0, "publication_reject": 0,
        "candle_accept": 0, "candle_not_evaluated": 0,
        "candle_quarantine": 0, "candle_reject": 0,
        "historical_accept": 0, "historical_incomplete": 0,
    }
    try:
        with (
            (partial_dir / "segments.jsonl").open("x", encoding="utf-8") as segment_stream,
            (partial_dir / "coverage-intervals.jsonl").open("x", encoding="utf-8") as interval_stream,
            (partial_dir / "symbol-coverage.jsonl").open("x", encoding="utf-8") as symbol_stream,
            (partial_dir / "exceptions.jsonl").open("x", encoding="utf-8") as exception_stream,
        ):
            known = set(symbols)
            for child in sorted(raw_root.iterdir(), key=lambda item: item.name):
                if child.is_symlink():
                    _json_line(exception_stream, {
                        "schema_version": 1, "code": "SYMLINK_IGNORED",
                        "path": child.name, "symbol": None,
                    })
                    counts["exceptions"] += 1
                elif child.is_dir() and child.name not in known:
                    _json_line(exception_stream, {
                        "schema_version": 1, "code": "SYMBOL_NOT_IN_CURRENT_EQ_UNIVERSE",
                        "path": child.name, "symbol": child.name,
                    })
                    counts["exceptions"] += 1
                elif child.is_file():
                    _json_line(exception_stream, {
                        "schema_version": 1, "code": "ARTIFACT_OUTSIDE_SYMBOL_DIRECTORY",
                        "path": child.name, "symbol": None,
                    })
                    counts["exceptions"] += 1

            for master_row in eq_inventory:
                symbol = master_row["symbol"]
                counts["symbols"] += 1
                symbol_dir = raw_root / symbol
                children = [] if not symbol_dir.is_dir() or symbol_dir.is_symlink() else sorted(
                    symbol_dir.iterdir(), key=lambda item: item.name
                )
                entries = [item for item in children if item.is_file() and not item.is_symlink()]
                for item in children:
                    if item.is_symlink():
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": "SYMLINK_IGNORED",
                            "path": _relative(item, raw_root), "symbol": symbol,
                        })
                        counts["exceptions"] += 1
                    elif item.is_dir():
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": "NESTED_DIRECTORY_IGNORED",
                            "path": _relative(item, raw_root), "symbol": symbol,
                        })
                        counts["exceptions"] += 1
                candidates = [item for item in entries if item.name.endswith(".json.gz")]
                if not entries:
                    counts["zero_artifact_symbols"] += 1
                candidate_names = {item.name for item in candidates}
                expected_sidecars = {
                    item.name.removesuffix(".json.gz") + ".meta.json" for item in candidates
                } | {item.name + ".done" for item in candidates}
                for entry in entries:
                    if entry.name.endswith(".meta.json") or entry.name.endswith(".json.gz.done"):
                        if entry.name not in expected_sidecars:
                            _json_line(exception_stream, {
                                "schema_version": 1, "code": "ORPHAN_SIDECAR",
                                "path": _relative(entry, raw_root), "symbol": symbol,
                            })
                            counts["exceptions"] += 1
                    elif entry.name not in candidate_names:
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": "UNSUPPORTED_ARTIFACT",
                            "path": _relative(entry, raw_root), "symbol": symbol,
                        })
                        counts["exceptions"] += 1

                segment_results: list[tuple[Path, Any, tuple[date, date] | None]] = []
                by_date: dict[str, list[tuple[str, Any]]] = {}
                for candidate in candidates:
                    report = validate_daily(candidate)
                    bounds = _segment_range(report)
                    segment_results.append((candidate, report, bounds))
                    for candle in report.content_quality.normalized_candles:
                        by_date.setdefault(candle.trading_date, []).append(
                            (_relative(candidate, raw_root), candle)
                        )
                    record = {
                        "schema_version": 1,
                        "symbol": symbol,
                        "path": _relative(candidate, raw_root),
                        "requested_start": report.segment_start,
                        "requested_end": report.segment_end,
                        "publication_gate": "ACCEPT" if report.publication_integrity.valid else "REJECT",
                        "candle_content_gate": (
                            "ACCEPT" if report.content_quality.valid
                            else "QUARANTINE" if report.publication_integrity.valid
                            else "NOT_EVALUATED"
                        ),
                        "disposition": report.disposition,
                        "sha256": report.publication_integrity.sha256,
                        "records": report.content_quality.records_seen,
                        "normalized_dates": [item.trading_date for item in report.content_quality.normalized_candles],
                        "issues": [
                            asdict(item) for item in (
                                report.publication_integrity.issues + report.content_quality.issues
                            )
                        ],
                    }
                    _json_line(segment_stream, record)
                    counts["segments"] += 1
                    if record["publication_gate"] == "REJECT":
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": "PUBLICATION_REJECTED",
                            "path": record["path"], "symbol": symbol,
                        })
                        counts["exceptions"] += 1
                    elif record["candle_content_gate"] == "QUARANTINE":
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": "CANDLE_CONTENT_QUARANTINED",
                            "path": record["path"], "symbol": symbol,
                        })
                        counts["exceptions"] += 1

                duplicate_dates: set[str] = set()
                contradictory_dates: set[str] = set()
                for trading_day, candles in sorted(by_date.items()):
                    if len(candles) > 1:
                        signatures = {_candle_signature(item) for _, item in candles}
                        code = "DUPLICATE_TRADING_DATE" if len(signatures) == 1 else "CONTRADICTORY_OHLCV"
                        (duplicate_dates if len(signatures) == 1 else contradictory_dates).add(trading_day)
                        _json_line(exception_stream, {
                            "schema_version": 1, "code": code, "date": trading_day,
                            "paths": [path for path, _ in candles], "symbol": symbol,
                        })
                        counts["exceptions"] += 1

                for index, left in enumerate(segment_results):
                    for right in segment_results[index + 1:]:
                        if (
                            left[2] is not None and right[2] is not None
                            and max(left[2][0], right[2][0]) <= min(left[2][1], right[2][1])
                        ):
                            _json_line(exception_stream, {
                                "schema_version": 1, "code": "SEGMENT_RANGE_OVERLAP",
                                "paths": [_relative(left[0], raw_root), _relative(right[0], raw_root)],
                                "symbol": symbol,
                            })
                            counts["exceptions"] += 1

                listing_record = listing_by_symbol.get(symbol)
                action_catalog: CorporateActionCatalog | None = catalogs.get(symbol)
                for horizon in request.horizons:
                    blockers: set[str] = set()
                    if any(
                        bounds is None and not report.publication_integrity.valid
                        for _, report, bounds in segment_results
                    ):
                        blockers.add("UNBOUNDED_REJECTED_ARTIFACT")
                    publication_counts: dict[str, int] = {}
                    candle_counts: dict[str, int] = {}
                    historical_counts: dict[str, int] = {}
                    current_interval: dict[str, Any] | None = None
                    for day in _days(horizon.start_date, horizon.end_date):
                        verified = [
                            item for item in segment_results
                            if item[1].publication_integrity.valid and _contains(item[2], day)
                        ]
                        rejected = [
                            item for item in segment_results
                            if not item[1].publication_integrity.valid and _contains(item[2], day)
                        ]
                        if len(verified) > 1:
                            publication = "OVERLAPPING_VERIFIED_PUBLICATIONS"
                            blockers.add("SEGMENT_RANGE_OVERLAP")
                        elif verified:
                            publication = "VERIFIED_PUBLISHED"
                        elif rejected:
                            publication = "REJECTED_OR_UNPUBLISHED"
                            blockers.add("PUBLICATION_REJECTED")
                        else:
                            publication = "MISSING_SEGMENT"
                            blockers.add("MISSING_SEGMENT")
                        entries_for_day = by_date.get(day.isoformat(), [])
                        if day.isoformat() in contradictory_dates:
                            candle_state = "CONTRADICTORY"
                            blockers.add("CONTRADICTORY_OHLCV")
                        elif day.isoformat() in duplicate_dates:
                            candle_state = "DUPLICATE"
                            blockers.add("DUPLICATE_TRADING_DATE")
                        elif entries_for_day:
                            candle_state = "VALIDATED_OBSERVED"
                        elif verified and any(not item[1].content_quality.valid for item in verified):
                            candle_state = "QUARANTINED"
                            blockers.add("CANDLE_CONTENT_QUARANTINED")
                        else:
                            candle_state = "NO_VALIDATED_CANDLE"
                        membership = _membership(listing_record, day)
                        calendar_status = calendar.status_on(day)
                        historical, day_blockers = _historical_state(
                            day=day, observed=bool(entries_for_day), membership=membership,
                            calendar_status=calendar_status, outages=outages,
                        )
                        blockers.update(day_blockers)
                        publication_counts[publication] = publication_counts.get(publication, 0) + 1
                        candle_counts[candle_state] = candle_counts.get(candle_state, 0) + 1
                        historical_counts[historical] = historical_counts.get(historical, 0) + 1
                        state = (publication, candle_state, historical, membership, calendar_status, day_blockers)
                        if current_interval is not None and current_interval.pop("_state") == state:
                            current_interval["end_date"] = day.isoformat()
                            current_interval["_state"] = state
                        else:
                            if current_interval is not None:
                                current_interval.pop("_state", None)
                                _write_interval(interval_stream, current_interval)
                                counts["intervals"] += 1
                            current_interval = {
                                "schema_version": 1, "symbol": symbol, "horizon": horizon.name,
                                "start_date": day.isoformat(), "end_date": day.isoformat(),
                                "publication_status": publication,
                                "candle_content_status": candle_state,
                                "historical_status": historical,
                                "membership_status": membership,
                                "calendar_status": calendar_status,
                                "blockers": list(day_blockers), "_state": state,
                            }
                    if current_interval is not None:
                        current_interval.pop("_state", None)
                        _write_interval(interval_stream, current_interval)
                        counts["intervals"] += 1
                    if listing_record is None:
                        blockers.add("AUTHORITATIVE_LISTING_HISTORY_REQUIRED")
                    alias_complete = bool(
                        mappings is not None
                        and mappings.manifest.coverage_status == "COMPLETE"
                        and date.fromisoformat(mappings.manifest.effective_start)
                        <= date.fromisoformat(horizon.start_date)
                        and date.fromisoformat(mappings.manifest.effective_end)
                        >= date.fromisoformat(horizon.end_date)
                    )
                    if not alias_complete:
                        blockers.add("AUTHORITATIVE_IDENTITY_HISTORY_REQUIRED")
                    start = date.fromisoformat(horizon.start_date)
                    end = date.fromisoformat(horizon.end_date)
                    if action_catalog is None:
                        blockers.add("CORPORATE_ACTION_COVERAGE_UNKNOWN")
                    elif not action_catalog.covers(start, end):
                        blockers.add("CORPORATE_ACTION_COVERAGE_INCOMPLETE")
                    unresolved_actions = [] if action_catalog is None else [
                        item for item in action_catalog.actions
                        if start <= date.fromisoformat(item.effective_date) <= end
                        and item.adjustment_status in {"UNKNOWN", "UNADJUSTED", "POLICY_REQUIRED"}
                    ]
                    if unresolved_actions:
                        blockers.add("CORPORATE_ACTION_ADJUSTMENT_UNRESOLVED")
                    publication_gate = (
                        "REJECT" if {
                            "PUBLICATION_REJECTED", "UNBOUNDED_REJECTED_ARTIFACT"
                        } & blockers
                        else "INCOMPLETE" if publication_counts.get("MISSING_SEGMENT", 0)
                        or publication_counts.get("OVERLAPPING_VERIFIED_PUBLICATIONS", 0)
                        else "ACCEPT"
                    )
                    candle_gate = (
                        "REJECT" if candle_counts.get("CONTRADICTORY", 0)
                        else "QUARANTINE" if any(
                            candle_counts.get(key, 0)
                            for key in ("QUARANTINED", "DUPLICATE")
                        ) else "NOT_EVALUATED" if not any(
                            report.publication_integrity.valid
                            for _, report, _ in segment_results
                        ) else "ACCEPT"
                    )
                    historical_gate = "ACCEPT" if not blockers else "INCOMPLETE"
                    counts[f"publication_{publication_gate.lower()}"] += 1
                    counts[f"candle_{candle_gate.lower()}"] += 1
                    counts[f"historical_{historical_gate.lower()}"] += 1
                    _json_line(symbol_stream, {
                        "schema_version": 1, "symbol": symbol,
                        "base_symbol": master_row["base_symbol"], "token": master_row["token"],
                        "horizon": asdict(horizon),
                        "artifact_count": len(entries), "segment_count": len(candidates),
                        "publication_gate": publication_gate,
                        "candle_content_gate": candle_gate,
                        "historical_completeness_gate": historical_gate,
                        "training_readiness_gate": "NOT_READY",
                        "publication_counts": publication_counts,
                        "candle_content_counts": candle_counts,
                        "historical_counts": historical_counts,
                        "blockers": sorted(blockers),
                    })
                    counts["symbol_horizons"] += 1

        for path, initial in source_checks:
            if _sha256(path, _artifact_limit(path) if raw_root in path.parents else MAX_SEGMENT_BYTES) != initial:
                raise RuntimeError(f"input changed during inventory run: {path}")
        if _canonical_hash(_raw_structure(raw_root)) != raw_structure_hash:
            raise RuntimeError("raw directory structure changed during inventory run")
        if _active_m24_lock(sync_lock):
            raise RuntimeError("M2.4 publication started during the M2.3 inventory run")
        if counts["symbols"] != expected_eq_count:
            raise RuntimeError("symbol reconciliation did not consume the complete EQ universe")
        output_hashes = {
            name: _sha256(partial_dir / name)
            for name in (
                "segments.jsonl", "coverage-intervals.jsonl",
                "symbol-coverage.jsonl", "exceptions.jsonl",
            )
        }
        summary = {
            "schema_version": 1,
            "contract": "stock-gplm.m23-inventory-summary.v1",
            "run_id": run_id,
            "universe": {
                "expected_eq_symbols": expected_eq_count,
                "reconciled_eq_symbols": counts["symbols"],
                "zero_artifact_symbols": counts["zero_artifact_symbols"],
                "master_sha256": master_sha256,
            },
            "counts": counts,
            "gates": {
                "publication": (
                    "REJECT" if counts["publication_reject"] else
                    "INCOMPLETE" if counts["publication_incomplete"] else "ACCEPT"
                ),
                "candle_content": (
                    "REJECT" if counts["candle_reject"] else
                    "QUARANTINE" if counts["candle_quarantine"] else
                    "NOT_EVALUATED" if counts["candle_not_evaluated"] else "ACCEPT"
                ),
                "historical_completeness": (
                    "INCOMPLETE" if counts["historical_incomplete"] else "ACCEPT"
                ),
                "training_readiness": "NOT_READY",
            },
        }
        (partial_dir / "inventory-summary.json").write_text(
            json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        output_hashes["inventory-summary.json"] = _sha256(partial_dir / "inventory-summary.json")
        manifest = {
            **run_binding,
            "run_id": run_id,
            "deterministic": True,
            "immutable": True,
            "outputs": output_hashes,
        }
        (partial_dir / "run-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(partial_dir, final_dir)
        return final_dir
    except Exception:
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise
