"""Read-only loaders for the reference evidence M5 eligibility depends on.

Every loader reuses an existing project contract instead of defining a new one:

* locked holdout: the hash-bound M3.2 ``split-plan.json`` (``stock-gplm.m32-research-run.v1``);
* trading calendar: an M2.5-R ``stock-gplm.m25r-registered-calendar.v1`` document,
  verified here by format (canonical-hash identity and explicit status for every
  date) without importing M2.5/M2.5-R code;
* listing history, symbol mappings, corporate-action coverage: the M2.1
  manifest-verified importers in :mod:`stock_gplm.m2.reference_data`;
* current identity: the M2.2-accepted Shoonya master;
* ETF exclusion: the M3.7 heuristic contract, which is labelled heuristic evidence;
* provider price basis (M5.1): a reviewed ``stock-gplm.m51-provider-price-basis.v1``
  registration; without one the basis stays unverified;
* M3.9-B share events (M5.1): a ``stock-gplm.m51-adjustment-events.v1`` file of
  ``CorporateActionEvent`` records.

:func:`load_reference_evidence` seals the loaded evidence (see :mod:`.issuance`).
Evidence assembled or edited by a caller is never sealed, so it can support
diagnostic research but never ``STRICT_RESEARCH`` eligibility.

Nothing here downloads, writes or repairs anything.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
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
from stock_gplm.m39b import CorporateActionEvent, SourceEvidence

from .issuance import _issues, verify_seal
from .models import canonical_hash
from .authority import (
    ApprovalTrustPolicy,
    EvidenceRegistration,
    load_evidence_registration,
    load_trust_policy,
)

MAX_JSON_BYTES = 8 * 1024 * 1024
CALENDAR_CONTRACT = "stock-gplm.m25r-registered-calendar.v1"
CALENDAR_MANIFEST_CONTRACT = "stock-gplm.m25r-calendar-registration-manifest.v1"
M32_CONTRACT = "stock-gplm.m32-research-run.v1"
ETF_HEURISTIC_CONTRACT = "stock-gplm.m37-etf-symbol-heuristic.v1"
EVIDENCE_PATHS_CONTRACT = "stock-gplm.m5-evidence-paths.v1"
PRICE_BASIS_CONTRACT = "stock-gplm.m51-provider-price-basis.v1"
ADJUSTMENT_EVENTS_CONTRACT = "stock-gplm.m51-adjustment-events.v1"
INSTRUMENT_CLASSIFICATION_CONTRACT = "stock-gplm.m511-instrument-classification.v1"
COST_SCHEDULE_CONTRACT = "stock-gplm.m511-cost-schedule.v1"
PRICE_BASES = frozenset({"RAW_UNADJUSTED", "PROVIDER_BACK_ADJUSTED", "MIXED"})
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
class InstrumentClassification:
    classification_id: str
    effective_start: str
    effective_end: str
    classes: dict[str, str]
    source_sha256: str
    registration: EvidenceRegistration
    authority_approved: bool

    def classification_for(self, symbol: str, start: str, end: str) -> str | None:
        if self.effective_start <= start and end <= self.effective_end:
            return self.classes.get(symbol)
        return None


def load_instrument_classification(
    path: Path, registration_path: Path, policy: ApprovalTrustPolicy | None,
) -> InstrumentClassification:
    value, digest = read_json(path)
    if not isinstance(value, dict) or set(value) != {
        "contract", "schema_version", "classification_id", "effective_start", "effective_end", "records"
    }:
        raise ValueError("instrument classification has missing or unsupported fields")
    if value["contract"] != INSTRUMENT_CLASSIFICATION_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported instrument-classification contract")
    classes: dict[str, str] = {}
    if not isinstance(value["records"], list) or not value["records"]:
        raise ValueError("instrument classification records must be non-empty")
    for record in value["records"]:
        if not isinstance(record, dict) or set(record) != {"symbol", "classification"} or not isinstance(
            record["symbol"], str) or not record["symbol"].endswith("-EQ") or record[
            "classification"] not in {"EQUITY", "ETF"} or record["symbol"] in classes:
            raise ValueError("instrument classification record is invalid or duplicated")
        classes[record["symbol"]] = record["classification"]
    start = date.fromisoformat(value["effective_start"]).isoformat()
    end = date.fromisoformat(value["effective_end"]).isoformat()
    if start > end:
        raise ValueError("instrument-classification effective range is reversed")
    binding = {key: value[key] for key in ("contract", "schema_version", "effective_start", "effective_end", "records")}
    if value["classification_id"] != canonical_hash(binding):
        raise ValueError("instrument-classification identifier does not match its content")
    registration = load_evidence_registration(registration_path)
    if registration.evidence_type != "INSTRUMENT_CLASSIFICATION" or registration.dependencies != {"payload": digest}:
        raise ValueError("instrument-classification registration does not bind the payload exactly")
    return InstrumentClassification(value["classification_id"], start, end, classes, digest, registration,
                                    policy.approves(registration) if policy else False)


@dataclass(frozen=True)
class RegisteredCostSchedule:
    profile_id: str
    effective_start: str
    effective_end: str
    commission_bps: float
    slippage_bps: float
    buy_statutory_bps: float
    sell_statutory_bps: float
    source_sha256: str
    registration: EvidenceRegistration
    authority_approved: bool

    def covers(self, start: str, end: str) -> bool:
        return self.effective_start <= start and end <= self.effective_end

    def components(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in (
            "commission_bps", "slippage_bps", "buy_statutory_bps", "sell_statutory_bps"
        )}


def load_cost_schedule(
    path: Path, registration_path: Path, policy: ApprovalTrustPolicy | None,
) -> RegisteredCostSchedule:
    value, digest = read_json(path)
    fields = {"contract", "schema_version", "profile_id", "effective_start", "effective_end", "components"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("cost schedule has missing or unsupported fields")
    if value["contract"] != COST_SCHEDULE_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported cost-schedule contract")
    names = {"commission_bps", "slippage_bps", "buy_statutory_bps", "sell_statutory_bps"}
    components = value["components"]
    if not isinstance(components, dict) or set(components) != names:
        raise ValueError("cost schedule must state every required component")
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0
           for number in components.values()):
        raise ValueError("cost schedule components must be nonnegative numbers")
    start = date.fromisoformat(value["effective_start"]).isoformat()
    end = date.fromisoformat(value["effective_end"]).isoformat()
    if start > end:
        raise ValueError("cost-schedule effective range is reversed")
    binding = {key: value[key] for key in ("contract", "schema_version", "effective_start", "effective_end", "components")}
    if value["profile_id"] != canonical_hash(binding):
        raise ValueError("cost-schedule profile identifier does not match its content")
    registration = load_evidence_registration(registration_path)
    if registration.evidence_type != "COST_SCHEDULE" or registration.dependencies != {"payload": digest}:
        raise ValueError("cost-schedule registration does not bind the payload exactly")
    return RegisteredCostSchedule(value["profile_id"], start, end,
                                  *(float(components[name]) for name in (
                                      "commission_bps", "slippage_bps", "buy_statutory_bps", "sell_statutory_bps")),
                                  digest, registration, policy.approves(registration) if policy else False)


@dataclass(frozen=True)
class PriceBasisRegistration:
    """A reviewed statement of one provider's price basis for named symbols and dates.

    Only ``RAW_UNADJUSTED`` lets M3.9-B share events be applied. The registration
    is a person's reviewed evidence, hash-bound like the calendar registration;
    the adapter never infers a basis from the candles themselves.
    """

    registration_id: str
    provider: str
    basis: str
    symbols: frozenset[str]
    effective_start: str
    effective_end: str
    source_sha256: str

    def covers(self, symbol: str, start: str, end: str) -> bool:
        return symbol in self.symbols and self.effective_start <= start and end <= self.effective_end


_PRICE_BASIS_FIELDS = frozenset({
    "contract", "schema_version", "registration_id", "provider", "exchange", "basis", "symbols",
    "effective_start", "effective_end", "source_reference", "document_sha256", "acquisition_method",
    "reviewed_at_utc", "review_note",
})


def load_price_basis_registration(path: Path) -> PriceBasisRegistration:
    value, digest = read_json(path)
    if not isinstance(value, dict) or set(value) != _PRICE_BASIS_FIELDS:
        raise ValueError("price-basis registration has missing or unsupported fields")
    if value["contract"] != PRICE_BASIS_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported price-basis registration contract")
    if value["provider"] != "shoonya" or value["exchange"] != "NSE":
        raise ValueError("price-basis registration must name the shoonya NSE provider series")
    if value["basis"] not in PRICE_BASES:
        raise ValueError(f"price basis must be one of {sorted(PRICE_BASES)}")
    if value["acquisition_method"] != "MANUAL_REVIEW":
        raise ValueError("price-basis evidence must come from a manual review")
    if not isinstance(value["document_sha256"], str) or not _HEX.fullmatch(value["document_sha256"]):
        raise ValueError("price-basis document_sha256 must be lowercase SHA-256")
    symbols = value["symbols"]
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) for s in symbols) or len(
            set(symbols)) != len(symbols):
        raise ValueError("price-basis symbols must be a non-empty list of unique strings")
    start = date.fromisoformat(value["effective_start"]).isoformat()
    end = date.fromisoformat(value["effective_end"]).isoformat()
    if start > end:
        raise ValueError("price-basis effective_start is after effective_end")
    binding = {key: value[key] for key in sorted(_PRICE_BASIS_FIELDS - {"registration_id"})}
    if value["registration_id"] != canonical_hash(binding):
        raise ValueError("price-basis registration identifier does not match its content")
    return PriceBasisRegistration(value["registration_id"], value["provider"], value["basis"], frozenset(symbols),
                                  start, end, digest)


_EVENT_FIELDS = frozenset({
    "event_id", "symbol", "event_type", "ex_date", "event_date", "record_date", "announcement_date", "source",
    "validation_state", "validation_reason", "new_shares", "base_shares", "cash_amount_inr",
    "reference_price_inr", "subscription_price_inr", "stated_price_factor",
})


def load_adjustment_events(path: Path) -> tuple[tuple[CorporateActionEvent, ...], str]:
    """Read M3.9-B events in their own ``to_dict`` shape; M3.9-B validates each one."""
    value, digest = read_json(path)
    if not isinstance(value, dict) or set(value) != {"contract", "schema_version", "events"}:
        raise ValueError("adjustment-events file has missing or unsupported fields")
    if value["contract"] != ADJUSTMENT_EVENTS_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported adjustment-events contract")
    if not isinstance(value["events"], list):
        raise ValueError("adjustment events must be a list")
    events: list[CorporateActionEvent] = []
    for index, item in enumerate(value["events"]):
        if not isinstance(item, dict) or set(item) != _EVENT_FIELDS or not isinstance(item["source"], dict):
            raise ValueError(f"adjustment event {index} has missing or unsupported fields")
        events.append(CorporateActionEvent(**(item | {"source": SourceEvidence(**item["source"])})))
    ids = [event.event_id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("adjustment event identifiers must be unique")
    return tuple(events), digest


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
    price_basis_registration: PriceBasisRegistration | None = None
    trust_policy: ApprovalTrustPolicy | None = None
    authority_registration: EvidenceRegistration | None = None
    authority_approved: bool = False
    instrument_classification: InstrumentClassification | None = None
    cost_schedule: RegisteredCostSchedule | None = None
    # M5.1.1 freshness: the evidence-paths file this was loaded from (outside the digest, so
    # identities stay relocation-stable). Strict runs reload it before execution.
    config_path: str | None = field(default=None, compare=False)
    issuance_seal: str = field(default="", compare=False, repr=False)


def _canonical(value: Any) -> Any:
    """Portable JSON form of evidence objects for content hashing."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical(getattr(value, f.name)) for f in fields(value)
                if f.name not in {"issuance_seal", "config_path"}}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, re.Pattern):
        return value.pattern
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def evidence_digest(evidence: ReferenceEvidence) -> str:
    """SHA-256 over every evidence field the adapter can read."""
    return canonical_hash(_canonical(evidence))


def evidence_issued(evidence: ReferenceEvidence) -> bool:
    """True only for evidence loaded from files by this process and unchanged since."""
    return verify_seal("evidence", evidence_digest(evidence), evidence.issuance_seal)


def _pair(paths: dict[str, Any], key: str, base: Path) -> tuple[Path, Path] | None:
    item = paths.get(key)
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) != {"payload", "manifest"}:
        raise ValueError(f"{key} must name exactly payload and manifest")
    return base / item["payload"], base / item["manifest"]


def _registered_pair(paths: dict[str, Any], key: str, base: Path) -> tuple[Path, Path] | None:
    item = paths.get(key)
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) != {"payload", "registration"}:
        raise ValueError(f"{key} must name exactly payload and registration")
    return base / item["payload"], base / item["registration"]


@_issues("evidence")
def load_reference_evidence(
    config_path: Path,
    *,
    _sign: Any = None,
) -> ReferenceEvidence:
    """Load a ``stock-gplm.m5-evidence-paths.v1`` file; relative paths resolve from its directory."""
    paths, _ = read_json(config_path, 64 * 1024)
    allowed = {"contract", "m32_run", "etf_heuristic", "calendar_registration", "listing_history",
               "symbol_mappings", "corporate_actions", "instrument_master", "provider_price_basis",
               "adjustment_events", "evidence_authority_registration", "instrument_classification",
               "cost_schedule"}
    if not isinstance(paths, dict) or paths.get("contract") != EVIDENCE_PATHS_CONTRACT or set(paths) - allowed:
        raise ValueError("unsupported M5 evidence-paths document")
    base = Path(config_path).resolve().parent
    hashes: dict[str, str] = {}
    policy = load_trust_policy()
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
    basis = None
    if paths.get("provider_price_basis"):
        basis = load_price_basis_registration(base / paths["provider_price_basis"])
        hashes["provider_price_basis"] = basis.source_sha256
    events: tuple[CorporateActionEvent, ...] = ()
    if paths.get("adjustment_events"):
        events, hashes["adjustment_events"] = load_adjustment_events(base / paths["adjustment_events"])
    classification = None
    pair = _registered_pair(paths, "instrument_classification", base)
    if pair is not None:
        classification = load_instrument_classification(*pair, policy)
        hashes["instrument_classification"] = classification.source_sha256
        hashes["instrument_classification_registration"] = classification.registration.registration_sha256
    costs = None
    pair = _registered_pair(paths, "cost_schedule", base)
    if pair is not None:
        costs = load_cost_schedule(*pair, policy)
        hashes["cost_schedule"] = costs.source_sha256
        hashes["cost_schedule_registration"] = costs.registration.registration_sha256
    authoritative_dependencies = dict(sorted(hashes.items()))
    authority = None
    authority_approved = False
    if paths.get("evidence_authority_registration"):
        authority = load_evidence_registration(base / paths["evidence_authority_registration"])
        if authority.evidence_type != "REFERENCE_EVIDENCE_BUNDLE":
            raise ValueError("evidence authority registration has the wrong evidence type")
        if authority.dependencies != authoritative_dependencies:
            raise ValueError("evidence authority registration does not bind every loaded dependency exactly")
        authority_approved = policy.approves(authority) if policy else False
        hashes["evidence_authority_registration"] = authority.registration_sha256
    if policy is not None:
        hashes["approval_trust_policy"] = policy.policy_sha256
        hashes["administrative_trust_root"] = policy.trust_root_sha256
    evidence = ReferenceEvidence(holdout, etf, calendar, imported.get("listing_history"),
                                 imported.get("symbol_mappings"), imported.get("corporate_actions"),
                                 events, tokens, hashes, basis.basis if basis else None, basis,
                                 policy, authority, authority_approved, classification, costs)
    return replace(evidence, config_path=str(Path(config_path).resolve()),
                   issuance_seal=_sign(evidence_digest(evidence)))
