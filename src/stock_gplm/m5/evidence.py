"""Read-only loaders for the reference evidence M5 eligibility depends on.

Every loader reuses an existing project contract instead of defining a new one:

* locked holdout: the hash-bound M3.2 ``split-plan.json`` (``stock-gplm.m32-research-run.v1``);
* trading calendar: an M2.5-R ``stock-gplm.m25r-registered-calendar.v1`` document,
  verified here by format (canonical-hash identity and explicit status for every
  date) without importing M2.5/M2.5-R code;
* listing history, symbol mappings, corporate-action coverage: the M2.1
  manifest-verified importers in :mod:`stock_gplm.m2.reference_data`;
* current identity: the M2.2-accepted Shoonya master;
* ETF exclusion: the M3.7 heuristic contract, which is labelled heuristic evidence.

Nothing here downloads, writes or repairs anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1
from stock_gplm.m2.reference_data import (
    ImportedCorporateActionsV1,
    ImportedHistoricalSymbolMappingsV1,
    ImportedListingHistoryV1,
    import_corporate_actions_v1,
    import_historical_symbol_mappings_v1,
    import_listing_history_v1,
)
from stock_gplm.m39b import CorporateActionEvent

from .models import canonical_hash

MAX_JSON_BYTES = 8 * 1024 * 1024
CALENDAR_CONTRACT = "stock-gplm.m25r-registered-calendar.v1"
CALENDAR_MANIFEST_CONTRACT = "stock-gplm.m25r-calendar-registration-manifest.v1"
M32_CONTRACT = "stock-gplm.m32-research-run.v1"
ETF_HEURISTIC_CONTRACT = "stock-gplm.m37-etf-symbol-heuristic.v1"
EVIDENCE_PATHS_CONTRACT = "stock-gplm.m5-evidence-paths.v1"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MANIFEST_FIELDS = frozenset({
    "document_file", "document_sha256", "document_media_type", "records_file", "records_sha256",
    "source_reference", "license_reference", "publication_date", "acquired_at_utc", "effective_start",
    "effective_end", "exchange", "market", "timezone_name", "acquisition_method", "usage_basis",
    "coverage_status", "contract", "schema_version",
})


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant is not allowed: {value}")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, maximum: int = MAX_JSON_BYTES) -> tuple[Any, str]:
    """Strictly parse a bounded regular file; return the value and its SHA-256."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evidence must be a regular non-symlink file: {path}")
    raw = path.read_bytes() if path.stat().st_size <= maximum else None
    if raw is None:
        raise ValueError(f"evidence file exceeds {maximum} bytes: {path}")
    value = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique)
    return value, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class HoldoutBoundary:
    """Earliest locked-holdout session across every horizon of a verified M3.2 plan."""

    cutoff: str
    m32_run_id: str
    split_plan_sha256: str


def load_holdout_boundary(m32_run: Path) -> HoldoutBoundary:
    manifest, _ = read_json(Path(m32_run) / "run-manifest.json")
    if not isinstance(manifest, dict) or manifest.get("contract") != M32_CONTRACT:
        raise ValueError("M3.2 run manifest has an unsupported contract")
    expected = (manifest.get("outputs") or {}).get("split-plan.json")
    plan, digest = read_json(Path(m32_run) / "split-plan.json")
    if not isinstance(expected, str) or digest != expected:
        raise ValueError("M3.2 split plan does not match its run-manifest hash")
    firsts = [item["locked_holdout"]["first_as_of"] for item in plan["plans"] if item.get("locked_holdout")]
    if not firsts or len(firsts) != len(plan["plans"]):
        raise ValueError("every M3.2 horizon must declare a locked holdout")
    cutoff = min(date.fromisoformat(value) for value in firsts)
    return HoldoutBoundary(cutoff.isoformat(), str(manifest["run_id"]), digest)


@dataclass(frozen=True)
class RegisteredCalendar:
    registration_id: str
    effective_start: str
    effective_end: str
    statuses: dict[str, str]
    source_sha256: str

    def covers(self, start: str, end: str) -> bool:
        return self.effective_start <= start and end <= self.effective_end

    def trading_sessions(self, start: str, end: str) -> list[str]:
        return [day for day, status in sorted(self.statuses.items())
                if start <= day <= end and status in {"TRADING_SESSION", "SPECIAL_SESSION"}]


def load_registered_calendar(path: Path) -> RegisteredCalendar:
    """Verify an M2.5-R calendar registration document by its published contract."""
    value, digest = read_json(path)
    if not isinstance(value, dict) or set(value) != {"schema_version", "contract", "registration_id", "manifest", "sessions"}:
        raise ValueError("registered calendar has missing or unsupported fields")
    if value["schema_version"] != 1 or value["contract"] != CALENDAR_CONTRACT:
        raise ValueError("unsupported registered calendar contract")
    manifest = value["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("calendar manifest has missing or unsupported fields")
    fixed = {"contract": CALENDAR_MANIFEST_CONTRACT, "schema_version": 1, "exchange": "NSE", "market": "EQUITY",
             "timezone_name": "Asia/Kolkata", "acquisition_method": "MANUAL_BROWSER_DOWNLOAD",
             "usage_basis": "USER_CONFIRMED_AUTHORIZED_RESEARCH_USE", "coverage_status": "COMPLETE"}
    for key, expected in fixed.items():
        if manifest[key] != expected:
            raise ValueError(f"calendar manifest {key} must be {expected!r}")
    if not all(isinstance(manifest[k], str) and _HEX.fullmatch(manifest[k]) for k in ("document_sha256", "records_sha256")):
        raise ValueError("calendar manifest hashes must be lowercase SHA-256")
    source = urlparse(str(manifest["source_reference"]))
    if source.scheme != "https" or source.hostname not in {"nseindia.com", "www.nseindia.com"}:
        raise ValueError("calendar source_reference must be an NSE HTTPS URL")
    binding = {key: value[key] for key in ("schema_version", "contract", "manifest", "sessions")}
    if value["registration_id"] != canonical_hash(binding):
        raise ValueError("registered calendar identifier does not match its content")
    statuses: dict[str, str] = {}
    for item in value["sessions"]:
        if not isinstance(item, dict) or set(item) != {"exchange", "date", "status", "session_open", "session_close", "label"}:
            raise ValueError("calendar session has missing or unsupported fields")
        if item["exchange"] != "NSE" or item["status"] not in {"TRADING_SESSION", "SPECIAL_SESSION", "MARKET_CLOSED"}:
            raise ValueError("calendar session status is unsupported or ambiguous")
        is_open = item["status"] != "MARKET_CLOSED"
        times = (item["session_open"], item["session_close"])
        if is_open != all(isinstance(t, str) and _CLOCK.fullmatch(t) for t in times) or (not is_open and any(times)):
            raise ValueError("calendar session times disagree with its status")
        statuses[date.fromisoformat(item["date"]).isoformat()] = item["status"]
    start = date.fromisoformat(manifest["effective_start"])
    end = date.fromisoformat(manifest["effective_end"])
    expected = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if [item["date"] for item in value["sessions"]] != expected:
        raise ValueError("registered calendar must classify every date in its range exactly once, in order")
    return RegisteredCalendar(value["registration_id"], start.isoformat(), end.isoformat(), statuses, digest)


@dataclass(frozen=True)
class EtfHeuristic:
    patterns: tuple[re.Pattern[str], ...]
    exclusions: frozenset[str]
    source_sha256: str

    def is_etf(self, symbol: str) -> bool:
        base = symbol.rsplit("-", 1)[0]
        return base not in self.exclusions and any(pattern.search(base) for pattern in self.patterns)


def load_etf_heuristic(path: Path) -> EtfHeuristic:
    value, digest = read_json(path)
    if not isinstance(value, dict) or value.get("contract") != ETF_HEURISTIC_CONTRACT:
        raise ValueError("unsupported ETF heuristic contract")
    return EtfHeuristic(tuple(re.compile(p) for p in value["etf_patterns"]),
                        frozenset(value.get("company_exclusions", [])), digest)


@dataclass(frozen=True)
class ReferenceEvidence:
    """Every evidence source available to the adapter; ``None`` means not supplied."""

    holdout: HoldoutBoundary | None
    etf_heuristic: EtfHeuristic
    calendar: RegisteredCalendar | None = None
    listing: ImportedListingHistoryV1 | None = None
    aliases: ImportedHistoricalSymbolMappingsV1 | None = None
    corporate_actions: ImportedCorporateActionsV1 | None = None
    adjustment_events: tuple[CorporateActionEvent, ...] = ()
    master_tokens: dict[str, str] | None = None
    source_hashes: dict[str, str] = field(default_factory=dict)
    # Evidenced price basis of the provider series. No file contract establishes it
    # yet, so it stays None (unverified) for real data. Shoonya history mixes
    # back-adjusted and unadjusted corporate actions (see docs/m5-eligibility-audit.md).
    provider_price_basis: str | None = None


def _pair(paths: dict[str, Any], key: str, base: Path) -> tuple[Path, Path] | None:
    item = paths.get(key)
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) != {"payload", "manifest"}:
        raise ValueError(f"{key} must name exactly payload and manifest")
    return base / item["payload"], base / item["manifest"]


def load_reference_evidence(config_path: Path) -> ReferenceEvidence:
    """Load a ``stock-gplm.m5-evidence-paths.v1`` file; relative paths resolve from its directory."""
    paths, _ = read_json(config_path, 64 * 1024)
    allowed = {"contract", "m32_run", "etf_heuristic", "calendar_registration", "listing_history",
               "symbol_mappings", "corporate_actions", "instrument_master"}
    if not isinstance(paths, dict) or paths.get("contract") != EVIDENCE_PATHS_CONTRACT or set(paths) - allowed:
        raise ValueError("unsupported M5 evidence-paths document")
    base = Path(config_path).resolve().parent
    hashes: dict[str, str] = {}
    holdout = load_holdout_boundary(base / paths["m32_run"]) if paths.get("m32_run") else None
    etf = load_etf_heuristic(base / paths["etf_heuristic"])
    hashes["etf_heuristic"] = etf.source_sha256
    calendar = None
    if paths.get("calendar_registration"):
        calendar = load_registered_calendar(base / paths["calendar_registration"])
        hashes["calendar_registration"] = calendar.source_sha256
    imported: dict[str, Any] = {}
    for key, importer in (("listing_history", import_listing_history_v1),
                          ("symbol_mappings", import_historical_symbol_mappings_v1),
                          ("corporate_actions", import_corporate_actions_v1)):
        pair = _pair(paths, key, base)
        if pair is not None:
            imported[key] = importer(*pair)
            hashes[key] = imported[key].manifest.sha256
    tokens = None
    if paths.get("instrument_master"):
        master = base / paths["instrument_master"]
        tokens = {item["symbol"]: item["token"] for item in read_shoonya_nse_eq_inventory_v1(master)}
        hashes["instrument_master"] = file_sha256(master)
    if holdout is not None:
        hashes["m32_split_plan"] = holdout.split_plan_sha256
    return ReferenceEvidence(holdout, etf, calendar, imported.get("listing_history"),
                             imported.get("symbol_mappings"), imported.get("corporate_actions"),
                             (), tokens, hashes)
