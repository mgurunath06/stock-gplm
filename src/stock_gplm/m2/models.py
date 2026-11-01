"""Versioned M2 metadata models with explicit provenance and unknown values."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

CalendarStatus = Literal[
    "TRADING_SESSION", "SPECIAL_SESSION", "MARKET_CLOSED", "UNKNOWN"
]
AdjustmentStatus = Literal[
    "UNKNOWN", "UNADJUSTED", "ADJUSTED", "NOT_APPLICABLE", "POLICY_REQUIRED"
]
CoverageStatus = Literal["UNKNOWN", "PARTIAL", "COMPLETE"]

_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")


def parse_date(value: str, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class Provenance:
    source: str
    as_of_date: str
    reference: str | None = None

    def __post_init__(self) -> None:
        _required_string(self.source, "provenance.source")
        parse_date(self.as_of_date, "provenance.as_of_date")
        if self.reference is not None:
            _required_string(self.reference, "provenance.reference")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Provenance":
        return cls(
            source=value.get("source"),
            as_of_date=value.get("as_of_date"),
            reference=value.get("reference"),
        )


@dataclass(frozen=True)
class HistoricalAlias:
    symbol: str
    valid_from: str | None
    valid_to: str | None
    provenance: Provenance

    def __post_init__(self) -> None:
        _required_string(self.symbol, "alias.symbol")
        start = parse_date(self.valid_from, "alias.valid_from") if self.valid_from else None
        end = parse_date(self.valid_to, "alias.valid_to") if self.valid_to else None
        if start and end and start > end:
            raise ValueError("alias.valid_from is after alias.valid_to")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HistoricalAlias":
        return cls(
            symbol=value.get("symbol"),
            valid_from=value.get("valid_from"),
            valid_to=value.get("valid_to"),
            provenance=Provenance.from_dict(value.get("provenance", {})),
        )


@dataclass(frozen=True)
class Instrument:
    nse_symbol: str
    isin: str | None = None
    shoonya_token: str | None = None
    listing_date: str | None = None
    delisting_date: str | None = None
    membership_verified_through: str | None = None
    aliases: tuple[HistoricalAlias, ...] = ()
    alias_history_status: CoverageStatus = "UNKNOWN"
    field_provenance: dict[str, Provenance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_string(self.nse_symbol, "instrument.nse_symbol")
        if self.isin is not None and _ISIN.fullmatch(self.isin) is None:
            raise ValueError("instrument.isin must match IN followed by 10 letters/digits")
        if self.shoonya_token is not None:
            _required_string(self.shoonya_token, "instrument.shoonya_token")
        listed = parse_date(self.listing_date, "instrument.listing_date") if self.listing_date else None
        delisted = (
            parse_date(self.delisting_date, "instrument.delisting_date")
            if self.delisting_date else None
        )
        verified_through = (
            parse_date(
                self.membership_verified_through,
                "instrument.membership_verified_through",
            )
            if self.membership_verified_through else None
        )
        if listed and delisted and listed > delisted:
            raise ValueError("instrument.listing_date is after delisting_date")
        if listed and verified_through and verified_through < listed:
            raise ValueError("instrument membership coverage ends before listing")
        if verified_through and listed is None:
            raise ValueError("instrument membership coverage requires listing_date")
        if self.alias_history_status not in {"UNKNOWN", "PARTIAL", "COMPLETE"}:
            raise ValueError("unsupported instrument.alias_history_status")
        required = {"nse_symbol"}
        for name in (
            "isin", "shoonya_token", "listing_date", "delisting_date",
            "membership_verified_through",
        ):
            if getattr(self, name) is not None:
                required.add(name)
        if self.alias_history_status != "UNKNOWN":
            required.add("alias_history_status")
        missing = sorted(required - self.field_provenance.keys())
        if missing:
            raise ValueError(f"missing field provenance for: {', '.join(missing)}")
        for index, left in enumerate(self.aliases):
            left_start = date.min if left.valid_from is None else date.fromisoformat(left.valid_from)
            left_end = date.max if left.valid_to is None else date.fromisoformat(left.valid_to)
            for right in self.aliases[index + 1 :]:
                right_start = date.min if right.valid_from is None else date.fromisoformat(right.valid_from)
                right_end = date.max if right.valid_to is None else date.fromisoformat(right.valid_to)
                if max(left_start, right_start) <= min(left_end, right_end):
                    raise ValueError("historical alias validity ranges overlap")

    def membership_on(self, day: date) -> str:
        if self.listing_date is None:
            return "UNKNOWN"
        if day < date.fromisoformat(self.listing_date):
            return "PRE_LISTING"
        if self.delisting_date and day > date.fromisoformat(self.delisting_date):
            return "DELISTED"
        if self.delisting_date and day <= date.fromisoformat(self.delisting_date):
            return "LISTED"
        if (
            self.membership_verified_through
            and day <= date.fromisoformat(self.membership_verified_through)
        ):
            return "LISTED"
        return "UNKNOWN"

    def symbol_on(self, day: date) -> str | None:
        if self.membership_on(day) != "LISTED":
            return None
        for alias in self.aliases:
            start_ok = alias.valid_from is None or day >= date.fromisoformat(alias.valid_from)
            end_ok = alias.valid_to is None or day <= date.fromisoformat(alias.valid_to)
            if start_ok and end_ok:
                return alias.symbol
        return self.nse_symbol

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Instrument":
        provenance = value.get("field_provenance", {})
        if not isinstance(provenance, dict):
            raise ValueError("instrument.field_provenance must be an object")
        return cls(
            nse_symbol=value.get("nse_symbol"),
            isin=value.get("isin"),
            shoonya_token=value.get("shoonya_token"),
            listing_date=value.get("listing_date"),
            delisting_date=value.get("delisting_date"),
            membership_verified_through=value.get("membership_verified_through"),
            aliases=tuple(HistoricalAlias.from_dict(item) for item in value.get("aliases", [])),
            alias_history_status=value.get("alias_history_status", "UNKNOWN"),
            field_provenance={key: Provenance.from_dict(item) for key, item in provenance.items()},
        )


@dataclass(frozen=True)
class InstrumentInventory:
    as_of_date: str
    instruments: tuple[Instrument, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        parse_date(self.as_of_date, "inventory.as_of_date")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported inventory schema_version")
        symbols = [item.nse_symbol for item in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("duplicate instrument.nse_symbol")
        # The same ISIN may legitimately appear across NSE trading series. Tokens
        # identify master rows and must remain unique in one imported snapshot.
        for field_name in ("shoonya_token",):
            values = [getattr(item, field_name) for item in self.instruments]
            known = [value for value in values if value is not None]
            if len(known) != len(set(known)):
                raise ValueError(f"duplicate known instrument.{field_name}")

    def get(self, symbol: str) -> Instrument | None:
        return next((item for item in self.instruments if item.nse_symbol == symbol), None)

    def resolve(self, symbol: str, day: date) -> Instrument | None:
        matches = [
            item for item in self.instruments
            if item.symbol_on(day) == symbol
        ]
        if len(matches) > 1:
            raise ValueError("symbol resolves to multiple instruments on the same date")
        return matches[0] if matches else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstrumentInventory":
        return cls(
            schema_version=value.get("schema_version"),
            as_of_date=value.get("as_of_date"),
            instruments=tuple(Instrument.from_dict(item) for item in value.get("instruments", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "InstrumentInventory":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("inventory must be a JSON object")
        return cls.from_dict(value)


@dataclass(frozen=True)
class CalendarDay:
    date: str
    status: CalendarStatus
    provenance: Provenance
    label: str | None = None

    def __post_init__(self) -> None:
        parse_date(self.date, "calendar_day.date")
        if self.status not in {
            "TRADING_SESSION", "SPECIAL_SESSION", "MARKET_CLOSED", "UNKNOWN"
        }:
            raise ValueError("unsupported calendar_day.status")
        if self.label is not None:
            _required_string(self.label, "calendar_day.label")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CalendarDay":
        return cls(
            date=value.get("date"),
            status=value.get("status"),
            label=value.get("label"),
            provenance=Provenance.from_dict(value.get("provenance", {})),
        )


@dataclass(frozen=True)
class NSETradingCalendar:
    days: tuple[CalendarDay, ...]
    coverage_start: str | None = None
    coverage_end: str | None = None
    coverage_complete: bool = False
    exchange: str = "NSE"
    timezone: str = "Asia/Kolkata"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported calendar schema_version")
        if self.exchange != "NSE" or self.timezone != "Asia/Kolkata":
            raise ValueError("unsupported calendar identity or schema")
        if type(self.coverage_complete) is not bool:
            raise ValueError("calendar.coverage_complete must be boolean")
        dates = [item.date for item in self.days]
        if len(dates) != len(set(dates)):
            raise ValueError("duplicate calendar date")
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("calendar coverage requires both start and end")
        if self.coverage_start:
            start = parse_date(self.coverage_start, "calendar.coverage_start")
            end = parse_date(self.coverage_end, "calendar.coverage_end")
            if start > end:
                raise ValueError("calendar coverage_start is after coverage_end")
            if self.coverage_complete:
                supplied = set(dates)
                cursor = start
                while cursor <= end:
                    if cursor.isoformat() not in supplied:
                        raise ValueError(f"complete calendar omits {cursor.isoformat()}")
                    cursor += timedelta(days=1)
        elif self.coverage_complete:
            raise ValueError("complete calendar requires coverage bounds")

    def status_on(self, day: date) -> str:
        entry = next((item for item in self.days if item.date == day.isoformat()), None)
        return entry.status if entry else "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NSETradingCalendar":
        return cls(
            schema_version=value.get("schema_version"),
            exchange=value.get("exchange"),
            timezone=value.get("timezone"),
            coverage_start=value.get("coverage_start"),
            coverage_end=value.get("coverage_end"),
            coverage_complete=value.get("coverage_complete", False),
            days=tuple(CalendarDay.from_dict(item) for item in value.get("days", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "NSETradingCalendar":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("calendar must be a JSON object")
        return cls.from_dict(value)


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    action_type: str
    effective_date: str
    provenance: Provenance
    adjustment_status: AdjustmentStatus = "UNKNOWN"
    details: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_string(self.symbol, "corporate_action.symbol")
        _required_string(self.action_type, "corporate_action.action_type")
        parse_date(self.effective_date, "corporate_action.effective_date")
        if self.adjustment_status not in {
            "UNKNOWN", "UNADJUSTED", "ADJUSTED", "NOT_APPLICABLE", "POLICY_REQUIRED"
        }:
            raise ValueError("unsupported corporate_action.adjustment_status")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.details.items()):
            raise ValueError("corporate_action.details must contain string keys and values")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CorporateAction":
        return cls(
            symbol=value.get("symbol"),
            action_type=value.get("action_type"),
            effective_date=value.get("effective_date"),
            adjustment_status=value.get("adjustment_status", "UNKNOWN"),
            provenance=Provenance.from_dict(value.get("provenance", {})),
            details=value.get("details", {}),
        )


@dataclass(frozen=True)
class CorporateActionCatalog:
    symbol: str
    coverage_status: CoverageStatus
    coverage_start: str | None = None
    coverage_end: str | None = None
    provenance: Provenance | None = None
    actions: tuple[CorporateAction, ...] = ()

    def __post_init__(self) -> None:
        _required_string(self.symbol, "corporate_action_catalog.symbol")
        if self.coverage_status not in {"UNKNOWN", "PARTIAL", "COMPLETE"}:
            raise ValueError("unsupported corporate-action coverage_status")
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("corporate-action coverage requires both bounds")
        if self.coverage_start:
            start = parse_date(self.coverage_start, "corporate_action_catalog.coverage_start")
            end = parse_date(self.coverage_end, "corporate_action_catalog.coverage_end")
            if start > end:
                raise ValueError("corporate-action coverage start is after end")
        if self.coverage_status != "UNKNOWN":
            if self.provenance is None or self.coverage_start is None:
                raise ValueError("known corporate-action coverage requires bounds and provenance")
        if any(item.symbol != self.symbol for item in self.actions):
            raise ValueError("corporate-action catalog contains another symbol")

    def covers(self, start: date, end: date) -> bool:
        return (
            self.coverage_status == "COMPLETE"
            and self.coverage_start is not None
            and date.fromisoformat(self.coverage_start) <= start
            and date.fromisoformat(self.coverage_end) >= end
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CorporateActionCatalog":
        provenance = value.get("provenance")
        return cls(
            symbol=value.get("symbol"),
            coverage_status=value.get("coverage_status", "UNKNOWN"),
            coverage_start=value.get("coverage_start"),
            coverage_end=value.get("coverage_end"),
            provenance=Provenance.from_dict(provenance) if provenance is not None else None,
            actions=tuple(CorporateAction.from_dict(item) for item in value.get("actions", [])),
        )


@dataclass(frozen=True)
class ProviderUnavailable:
    start_date: str
    end_date: str
    provenance: Provenance
    reason: str

    def __post_init__(self) -> None:
        start = parse_date(self.start_date, "provider_unavailable.start_date")
        end = parse_date(self.end_date, "provider_unavailable.end_date")
        if start > end:
            raise ValueError("provider unavailability start is after end")
        _required_string(self.reason, "provider_unavailable.reason")

    def contains(self, day: date) -> bool:
        return date.fromisoformat(self.start_date) <= day <= date.fromisoformat(self.end_date)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProviderUnavailable":
        return cls(
            start_date=value.get("start_date"),
            end_date=value.get("end_date"),
            reason=value.get("reason"),
            provenance=Provenance.from_dict(value.get("provenance", {})),
        )


@dataclass(frozen=True)
class HorizonRequest:
    name: str
    start_date: str
    end_date: str

    def __post_init__(self) -> None:
        _required_string(self.name, "horizon.name")
        start = parse_date(self.start_date, "horizon.start_date")
        end = parse_date(self.end_date, "horizon.end_date")
        if start > end:
            raise ValueError("horizon start is after end")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HorizonRequest":
        return cls(
            name=value.get("name"),
            start_date=value.get("start_date"),
            end_date=value.get("end_date"),
        )
