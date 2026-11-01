"""M2.1 read-only, hash-bound reference-data import contracts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .models import (
    CalendarDay,
    CorporateAction,
    CorporateActionCatalog,
    HistoricalAlias,
    Instrument,
    InstrumentInventory,
    NSETradingCalendar,
    Provenance,
    ProviderUnavailable,
    parse_date,
)

CoverageStatus = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]
SUPPORTED_START = date(2022, 1, 1)
SUPPORTED_END = date(2026, 12, 31)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REFERENCE_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_COMPRESSION_RATIO = 100


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    return value


def _utc_timestamp(value: Any, field_name: str) -> str:
    text = _required_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ReferenceDataManifestV1:
    contract: str
    payload_file: str
    sha256: str
    source: str
    source_reference: str
    publication_date: str
    acquired_at_utc: str
    effective_start: str
    effective_end: str
    coverage_status: CoverageStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported reference manifest schema_version")
        for field_name in (
            "contract", "payload_file", "sha256", "source", "source_reference"
        ):
            _required_string(getattr(self, field_name), f"manifest.{field_name}")
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ValueError("manifest.sha256 must be lowercase SHA-256")
        parse_date(self.publication_date, "manifest.publication_date")
        acquired = datetime.fromisoformat(
            _utc_timestamp(self.acquired_at_utc, "manifest.acquired_at_utc").replace("Z", "+00:00")
        )
        if acquired.date() < date.fromisoformat(self.publication_date):
            raise ValueError("manifest acquisition predates publication")
        start = parse_date(self.effective_start, "manifest.effective_start")
        end = parse_date(self.effective_end, "manifest.effective_end")
        if start > end:
            raise ValueError("manifest effective_start is after effective_end")
        if self.coverage_status not in {"UNKNOWN", "PARTIAL", "COMPLETE"}:
            raise ValueError("unsupported manifest.coverage_status")

    @property
    def provenance(self) -> Provenance:
        return Provenance(
            source=self.source,
            as_of_date=self.publication_date,
            reference=self.source_reference,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReferenceDataManifestV1":
        return cls(
            schema_version=value.get("schema_version"),
            contract=value.get("contract"),
            payload_file=value.get("payload_file"),
            sha256=value.get("sha256"),
            source=value.get("source"),
            source_reference=value.get("source_reference"),
            publication_date=value.get("publication_date"),
            acquired_at_utc=_utc_timestamp(
                value.get("acquired_at_utc"), "manifest.acquired_at_utc"
            ),
            effective_start=value.get("effective_start"),
            effective_end=value.get("effective_end"),
            coverage_status=value.get("coverage_status"),
        )


@dataclass(frozen=True)
class ImportedNSECalendarV1:
    manifest: ReferenceDataManifestV1
    calendar: NSETradingCalendar


@dataclass(frozen=True)
class ImportedShoonyaInstrumentMasterV1:
    manifest: ReferenceDataManifestV1
    inventory: InstrumentInventory
    source_records: int
    nse_records: int


@dataclass(frozen=True)
class HistoricalSymbolMappingRecordV1:
    canonical_symbol: str
    alias_symbol: str
    valid_from: str | None
    valid_to: str | None
    isin: str | None
    provenance: Provenance


@dataclass(frozen=True)
class ImportedHistoricalSymbolMappingsV1:
    manifest: ReferenceDataManifestV1
    records: tuple[HistoricalSymbolMappingRecordV1, ...]


@dataclass(frozen=True)
class ImportedCorporateActionsV1:
    manifest: ReferenceDataManifestV1
    catalogs: tuple[CorporateActionCatalog, ...]


@dataclass(frozen=True)
class ListingHistoryRecordV1:
    canonical_symbol: str
    listing_date: str
    delisting_date: str | None
    membership_verified_through: str | None
    provenance: Provenance


@dataclass(frozen=True)
class ImportedListingHistoryV1:
    manifest: ReferenceDataManifestV1
    records: tuple[ListingHistoryRecordV1, ...]


@dataclass(frozen=True)
class ImportedProviderUnavailabilityV1:
    manifest: ReferenceDataManifestV1
    outages: tuple[ProviderUnavailable, ...]


def load_manifest(path: Path) -> ReferenceDataManifestV1:
    if Path(path).stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("reference manifest exceeds the configured size limit")
    value = _json_loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise ValueError("reference manifest must be a JSON object")
    expected = {
        "schema_version", "contract", "payload_file", "sha256", "source",
        "source_reference", "publication_date", "acquired_at_utc",
        "effective_start", "effective_end", "coverage_status",
    }
    if set(value) != expected:
        raise ValueError("reference manifest has missing or unsupported fields")
    return ReferenceDataManifestV1.from_dict(value)


def _verified_payload(payload_path: Path, manifest_path: Path, contract: str) -> tuple[bytes, ReferenceDataManifestV1]:
    payload_path = Path(payload_path)
    manifest = load_manifest(manifest_path)
    if manifest.contract != contract:
        raise ValueError(f"manifest.contract must equal {contract}")
    if manifest.payload_file != payload_path.name:
        raise ValueError("manifest.payload_file does not match payload filename")
    if payload_path.stat().st_size > MAX_REFERENCE_PAYLOAD_BYTES:
        raise ValueError("reference payload exceeds the configured size limit")
    raw = payload_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.sha256:
        raise ValueError("reference payload SHA-256 mismatch")
    return raw, manifest


def _calendar_records(raw: bytes, suffix: str) -> list[dict[str, Any]]:
    if suffix.lower() == ".csv":
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != ["date", "status", "label"]:
            raise ValueError("calendar CSV columns must be date,status,label")
        return [dict(row) for row in reader]
    if suffix.lower() == ".json":
        value = _json_loads(raw)
        if not isinstance(value, dict) or set(value) != {"records"}:
            raise ValueError("calendar JSON must contain only a records array")
        records = value["records"]
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("calendar records must be JSON objects")
        return records
    raise ValueError("calendar V1 payload must be .csv or .json")


def import_nse_calendar_v1(payload_path: Path, manifest_path: Path) -> ImportedNSECalendarV1:
    """Import explicit 2022-2026 calendar rows; never infer omitted dates."""
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.nse-calendar.v1"
    )
    effective_start = date.fromisoformat(manifest.effective_start)
    effective_end = date.fromisoformat(manifest.effective_end)
    if effective_start < SUPPORTED_START or effective_end > SUPPORTED_END:
        raise ValueError("NSE calendar V1 effective range must be within 2022-2026")
    records = _calendar_records(raw, Path(payload_path).suffix)
    days: list[CalendarDay] = []
    for index, record in enumerate(records):
        if set(record) != {"date", "status", "label"}:
            raise ValueError(f"calendar record {index} has unsupported fields")
        day = parse_date(record["date"], f"calendar record {index} date")
        if not effective_start <= day <= effective_end:
            raise ValueError(f"calendar record {index} is outside manifest effective range")
        label = record.get("label") or None
        days.append(
            CalendarDay(
                date=day.isoformat(),
                status=record["status"],
                label=label,
                provenance=manifest.provenance,
            )
        )
    calendar = NSETradingCalendar(
        days=tuple(days),
        coverage_start=manifest.effective_start,
        coverage_end=manifest.effective_end,
        coverage_complete=manifest.coverage_status == "COMPLETE",
    )
    return ImportedNSECalendarV1(manifest, calendar)


def _instrument_text(raw: bytes, payload_path: Path) -> str:
    if payload_path.suffix.lower() != ".zip":
        return raw.decode("utf-8-sig")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise ValueError("Shoonya master ZIP must contain exactly one file")
        if members[0].file_size > MAX_REFERENCE_PAYLOAD_BYTES:
            raise ValueError("Shoonya master member exceeds the configured size limit")
        if (
            members[0].compress_size == 0
            or members[0].file_size / members[0].compress_size
            > MAX_REFERENCE_COMPRESSION_RATIO
        ):
            raise ValueError("Shoonya master ZIP compression ratio exceeds the limit")
        return archive.read(members[0]).decode("utf-8-sig")


def import_shoonya_instrument_master_v1(
    payload_path: Path, manifest_path: Path
) -> ImportedShoonyaInstrumentMasterV1:
    """Import the current Shoonya master without asserting historical membership."""
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.shoonya-instrument-master.v1"
    )
    text = _instrument_text(raw, Path(payload_path))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",|\t")
    except csv.Error as exc:
        raise ValueError("unable to detect Shoonya master delimiter") from exc
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    required = {"Exchange", "Token", "TradingSymbol"}
    fields = set(reader.fieldnames or [])
    if not required <= fields:
        raise ValueError("Shoonya master requires Exchange,Token,TradingSymbol columns")
    unsupported_isin = "ISIN" not in fields
    instruments: list[Instrument] = []
    source_records = 0
    for index, row in enumerate(reader):
        source_records += 1
        if row.get("Exchange") != "NSE":
            continue
        symbol = _required_string(row.get("TradingSymbol"), f"master row {index} TradingSymbol")
        token = _required_string(row.get("Token"), f"master row {index} Token")
        isin_value = None if unsupported_isin else (row.get("ISIN") or None)
        provenance = manifest.provenance
        field_provenance = {"nse_symbol": provenance, "shoonya_token": provenance}
        if isin_value is not None:
            field_provenance["isin"] = provenance
        instruments.append(
            Instrument(
                nse_symbol=symbol,
                isin=isin_value,
                shoonya_token=token,
                listing_date=None,
                delisting_date=None,
                membership_verified_through=None,
                aliases=(),
                alias_history_status="UNKNOWN",
                field_provenance=field_provenance,
            )
        )
    inventory = InstrumentInventory(
        as_of_date=manifest.publication_date,
        instruments=tuple(instruments),
    )
    return ImportedShoonyaInstrumentMasterV1(
        manifest=manifest,
        inventory=inventory,
        source_records=source_records,
        nse_records=len(instruments),
    )


def import_historical_symbol_mappings_v1(
    payload_path: Path, manifest_path: Path
) -> ImportedHistoricalSymbolMappingsV1:
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.historical-symbol-mappings.v1"
    )
    value = _json_loads(raw)
    if not isinstance(value, dict) or set(value) != {"records"} or not isinstance(value["records"], list):
        raise ValueError("historical-symbol payload must contain only a records array")
    records: list[HistoricalSymbolMappingRecordV1] = []
    aliases_by_symbol: dict[str, list[HistoricalAlias]] = {}
    for index, item in enumerate(value["records"]):
        if not isinstance(item, dict):
            raise ValueError(f"historical-symbol record {index} must be an object")
        allowed = {"canonical_symbol", "alias_symbol", "valid_from", "valid_to", "isin"}
        if set(item) != allowed:
            raise ValueError(f"historical-symbol record {index} has unsupported fields")
        canonical = _required_string(item["canonical_symbol"], "canonical_symbol")
        isin_value = item["isin"]
        if isin_value is not None:
            Instrument(
                nse_symbol=canonical,
                isin=isin_value,
                field_provenance={
                    "nse_symbol": manifest.provenance,
                    "isin": manifest.provenance,
                },
            )
        alias = HistoricalAlias(
            symbol=_required_string(item["alias_symbol"], "alias_symbol"),
            valid_from=item["valid_from"],
            valid_to=item["valid_to"],
            provenance=manifest.provenance,
        )
        manifest_start = date.fromisoformat(manifest.effective_start)
        manifest_end = date.fromisoformat(manifest.effective_end)
        alias_start = date.min if alias.valid_from is None else date.fromisoformat(alias.valid_from)
        alias_end = date.max if alias.valid_to is None else date.fromisoformat(alias.valid_to)
        if alias_start < manifest_start or alias_end > manifest_end:
            raise ValueError(f"historical-symbol record {index} is outside manifest range")
        aliases_by_symbol.setdefault(canonical, []).append(alias)
        records.append(
            HistoricalSymbolMappingRecordV1(
                canonical_symbol=canonical,
                alias_symbol=alias.symbol,
                valid_from=alias.valid_from,
                valid_to=alias.valid_to,
                isin=isin_value,
                provenance=manifest.provenance,
            )
        )
    for canonical, aliases in aliases_by_symbol.items():
        # Reuse Instrument's overlap validation without asserting membership.
        Instrument(
            nse_symbol=canonical,
            aliases=tuple(aliases),
            alias_history_status="PARTIAL",
            field_provenance={
                "nse_symbol": manifest.provenance,
                "alias_history_status": manifest.provenance,
            },
        )
    return ImportedHistoricalSymbolMappingsV1(manifest, tuple(records))


def merge_historical_symbol_mappings(
    inventory: InstrumentInventory,
    imported: ImportedHistoricalSymbolMappingsV1,
) -> InstrumentInventory:
    """Return an enriched snapshot; never modify the imported master in place."""
    grouped: dict[str, list[HistoricalSymbolMappingRecordV1]] = {}
    for record in imported.records:
        grouped.setdefault(record.canonical_symbol, []).append(record)
    enriched: list[Instrument] = []
    for instrument in inventory.instruments:
        records = grouped.get(instrument.nse_symbol, [])
        aliases = tuple(
            HistoricalAlias(
                record.alias_symbol,
                record.valid_from,
                record.valid_to,
                record.provenance,
            )
            for record in records
        )
        provenance = dict(instrument.field_provenance)
        alias_status = imported.manifest.coverage_status
        if alias_status != "UNKNOWN":
            provenance["alias_history_status"] = imported.manifest.provenance
        mapped_isins = {record.isin for record in records if record.isin is not None}
        isin = instrument.isin
        if isin is None and len(mapped_isins) == 1:
            isin = next(iter(mapped_isins))
            provenance["isin"] = imported.manifest.provenance
        if len(mapped_isins) > 1:
            raise ValueError(f"historical mappings disagree on ISIN for {instrument.nse_symbol}")
        enriched.append(
            replace(
                instrument,
                isin=isin,
                aliases=aliases,
                alias_history_status=alias_status,
                field_provenance=provenance,
            )
        )
    unknown_symbols = sorted(set(grouped) - {item.nse_symbol for item in inventory.instruments})
    if unknown_symbols:
        raise ValueError(
            "historical mappings reference symbols absent from current master: "
            + ", ".join(unknown_symbols)
        )
    return InstrumentInventory(
        as_of_date=inventory.as_of_date,
        instruments=tuple(enriched),
        schema_version=inventory.schema_version,
    )


def import_listing_history_v1(
    payload_path: Path, manifest_path: Path
) -> ImportedListingHistoryV1:
    """Import authoritative listing bounds without deriving them from candles."""
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.listing-history.v1"
    )
    value = _json_loads(raw)
    if not isinstance(value, dict) or set(value) != {"records"} or not isinstance(value["records"], list):
        raise ValueError("listing-history payload must contain only a records array")
    records: list[ListingHistoryRecordV1] = []
    seen: set[str] = set()
    manifest_start = date.fromisoformat(manifest.effective_start)
    manifest_end = date.fromisoformat(manifest.effective_end)
    for index, item in enumerate(value["records"]):
        if not isinstance(item, dict) or set(item) != {
            "canonical_symbol", "listing_date", "delisting_date",
            "membership_verified_through",
        }:
            raise ValueError(f"listing-history record {index} has unsupported fields")
        symbol = _required_string(item["canonical_symbol"], "canonical_symbol")
        if symbol in seen:
            raise ValueError(f"duplicate listing-history symbol: {symbol}")
        seen.add(symbol)
        listed = parse_date(item["listing_date"], "listing_date")
        delisted = (
            parse_date(item["delisting_date"], "delisting_date")
            if item["delisting_date"] is not None else None
        )
        verified = (
            parse_date(item["membership_verified_through"], "membership_verified_through")
            if item["membership_verified_through"] is not None else None
        )
        if not manifest_start <= listed <= manifest_end:
            raise ValueError(f"listing-history record {index} listing date is outside manifest range")
        if delisted is not None and not manifest_start <= delisted <= manifest_end:
            raise ValueError(f"listing-history record {index} delisting date is outside manifest range")
        if verified is not None and not manifest_start <= verified <= manifest_end:
            raise ValueError(f"listing-history record {index} verified boundary is outside manifest range")
        # Reuse the established membership invariant validation.
        Instrument(
            nse_symbol=symbol,
            listing_date=listed.isoformat(),
            delisting_date=delisted.isoformat() if delisted else None,
            membership_verified_through=verified.isoformat() if verified else None,
            field_provenance={
                "nse_symbol": manifest.provenance,
                "listing_date": manifest.provenance,
                **({"delisting_date": manifest.provenance} if delisted else {}),
                **({"membership_verified_through": manifest.provenance} if verified else {}),
            },
        )
        records.append(ListingHistoryRecordV1(
            canonical_symbol=symbol,
            listing_date=listed.isoformat(),
            delisting_date=delisted.isoformat() if delisted else None,
            membership_verified_through=verified.isoformat() if verified else None,
            provenance=manifest.provenance,
        ))
    return ImportedListingHistoryV1(manifest, tuple(records))


def merge_listing_history(
    inventory: InstrumentInventory, imported: ImportedListingHistoryV1
) -> InstrumentInventory:
    """Attach supplied listing evidence to current-master identities."""
    by_symbol = {item.canonical_symbol: item for item in imported.records}
    current = {item.nse_symbol for item in inventory.instruments}
    unknown = sorted(set(by_symbol) - current)
    if unknown:
        raise ValueError(
            "listing history references symbols absent from current master: "
            + ", ".join(unknown)
        )
    enriched: list[Instrument] = []
    for instrument in inventory.instruments:
        record = by_symbol.get(instrument.nse_symbol)
        if record is None:
            enriched.append(instrument)
            continue
        provenance = dict(instrument.field_provenance)
        provenance["listing_date"] = record.provenance
        if record.delisting_date is not None:
            provenance["delisting_date"] = record.provenance
        if record.membership_verified_through is not None:
            provenance["membership_verified_through"] = record.provenance
        enriched.append(replace(
            instrument,
            listing_date=record.listing_date,
            delisting_date=record.delisting_date,
            membership_verified_through=record.membership_verified_through,
            field_provenance=provenance,
        ))
    return replace(inventory, instruments=tuple(enriched))


def import_provider_unavailability_v1(
    payload_path: Path, manifest_path: Path
) -> ImportedProviderUnavailabilityV1:
    """Import authoritative provider outage intervals bound to a manifest hash."""
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.provider-unavailability.v1"
    )
    value = _json_loads(raw)
    if not isinstance(value, dict) or set(value) != {"outages"} or not isinstance(value["outages"], list):
        raise ValueError("provider-unavailability payload must contain only an outages array")
    outages: list[ProviderUnavailable] = []
    manifest_start = date.fromisoformat(manifest.effective_start)
    manifest_end = date.fromisoformat(manifest.effective_end)
    for index, item in enumerate(value["outages"]):
        if not isinstance(item, dict) or set(item) != {"start_date", "end_date", "reason"}:
            raise ValueError(f"provider-unavailability record {index} has unsupported fields")
        outage = ProviderUnavailable(
            start_date=item["start_date"],
            end_date=item["end_date"],
            reason=_required_string(item["reason"], "provider-unavailability reason"),
            provenance=manifest.provenance,
        )
        if (
            date.fromisoformat(outage.start_date) < manifest_start
            or date.fromisoformat(outage.end_date) > manifest_end
        ):
            raise ValueError(f"provider-unavailability record {index} is outside manifest range")
        outages.append(outage)
    ordered = sorted(outages, key=lambda item: (item.start_date, item.end_date))
    for left, right in zip(ordered, ordered[1:]):
        if date.fromisoformat(right.start_date) <= date.fromisoformat(left.end_date):
            raise ValueError("provider-unavailability intervals overlap")
    return ImportedProviderUnavailabilityV1(manifest, tuple(ordered))


def import_corporate_actions_v1(
    payload_path: Path, manifest_path: Path
) -> ImportedCorporateActionsV1:
    raw, manifest = _verified_payload(
        payload_path, manifest_path, "stock-gplm.corporate-actions.v1"
    )
    value = _json_loads(raw)
    if not isinstance(value, dict) or set(value) != {"catalogs"} or not isinstance(value["catalogs"], list):
        raise ValueError("corporate-action payload must contain only a catalogs array")
    catalogs: list[CorporateActionCatalog] = []
    for catalog_index, item in enumerate(value["catalogs"]):
        if not isinstance(item, dict):
            raise ValueError(f"corporate-action catalog {catalog_index} must be an object")
        if set(item) != {"symbol", "coverage_status", "coverage_start", "coverage_end", "actions"}:
            raise ValueError(f"corporate-action catalog {catalog_index} has unsupported fields")
        symbol = _required_string(item["symbol"], "corporate-action symbol")
        if not isinstance(item["actions"], list):
            raise ValueError(f"corporate-action catalog {catalog_index} actions must be an array")
        catalog_start = parse_date(item["coverage_start"], "corporate-action coverage_start")
        catalog_end = parse_date(item["coverage_end"], "corporate-action coverage_end")
        if (
            catalog_start < date.fromisoformat(manifest.effective_start)
            or catalog_end > date.fromisoformat(manifest.effective_end)
        ):
            raise ValueError(f"corporate-action catalog {catalog_index} is outside manifest range")
        actions: list[CorporateAction] = []
        for action_index, action in enumerate(item["actions"]):
            if not isinstance(action, dict):
                raise ValueError(f"corporate action {action_index} must be an object")
            if set(action) != {"action_type", "effective_date", "adjustment_status", "details"}:
                raise ValueError(f"corporate action {action_index} has unsupported fields")
            actions.append(
                CorporateAction(
                    symbol=symbol,
                    action_type=action["action_type"],
                    effective_date=action["effective_date"],
                    adjustment_status=action["adjustment_status"],
                    details=action["details"],
                    provenance=manifest.provenance,
                )
            )
            effective = date.fromisoformat(actions[-1].effective_date)
            if not (
                date.fromisoformat(manifest.effective_start)
                <= effective
                <= date.fromisoformat(manifest.effective_end)
            ):
                raise ValueError(f"corporate action {action_index} is outside manifest range")
        catalogs.append(
            CorporateActionCatalog(
                symbol=symbol,
                coverage_status=item["coverage_status"],
                coverage_start=item["coverage_start"],
                coverage_end=item["coverage_end"],
                provenance=manifest.provenance,
                actions=tuple(actions),
            )
        )
    symbols = [item.symbol for item in catalogs]
    if len(symbols) != len(set(symbols)):
        raise ValueError("corporate-action payload contains duplicate symbol catalogs")
    return ImportedCorporateActionsV1(manifest, tuple(catalogs))
