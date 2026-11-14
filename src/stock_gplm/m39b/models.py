"""Typed canonical records for M3.9-B corporate-action remediation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    STOCK_SPLIT = "STOCK_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    BONUS = "BONUS"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    RIGHTS = "RIGHTS"
    MERGER = "MERGER"
    DEMERGER = "DEMERGER"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class ValidationState(StrEnum):
    VALIDATED = "VALIDATED"
    UNKNOWN = "UNKNOWN"
    QUARANTINED = "QUARANTINED"
    UNSUPPORTED = "UNSUPPORTED"


class LicenceStatus(StrEnum):
    APPROVED_FOR_DERIVATION = "APPROVED_FOR_DERIVATION"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    UNKNOWN = "UNKNOWN"
    PROHIBITED = "PROHIBITED"


class AdjustmentMode(StrEnum):
    PRICE_RETURN = "PRICE_RETURN"
    TOTAL_RETURN = "TOTAL_RETURN"


class SeriesBasis(StrEnum):
    RAW_UNADJUSTED = "RAW_UNADJUSTED"
    DERIVED_ADJUSTED = "DERIVED_ADJUSTED"
    UNKNOWN = "UNKNOWN"


COMPLEX_TYPES = frozenset({EventType.MERGER, EventType.DEMERGER, EventType.SYMBOL_CHANGE})


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _day(value: str | None, name: str, *, required: bool = False) -> date | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def parse_timestamp(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def decimal_value(value: Decimal | str | int, name: str, *, positive: bool = True) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be finite and {qualifier}")
    return result


@dataclass(frozen=True)
class SourceEvidence:
    source: str
    source_identifier: str
    licence_status: LicenceStatus
    available_at: str
    retrieved_at: str

    def __post_init__(self) -> None:
        _required(self.source, "source")
        _required(self.source_identifier, "source_identifier")
        try:
            object.__setattr__(self, "licence_status", LicenceStatus(self.licence_status))
        except ValueError as exc:
            raise ValueError("unsupported licence_status") from exc
        available = parse_timestamp(self.available_at, "available_at")
        retrieved = parse_timestamp(self.retrieved_at, "retrieved_at")
        if available > retrieved:
            raise ValueError("available_at cannot be after retrieved_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source, "source_identifier": self.source_identifier,
            "licence_status": self.licence_status.value, "available_at": self.available_at,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True)
class CorporateActionEvent:
    """Source-independent event record; ratio meanings depend on event type.

    For splits, ``new_shares`` is the resulting number for ``base_shares`` old
    shares. For bonus/rights, it is the additional/entitled number per base shares.
    Cash dividend and rights subscription prices are INR per pre-event share.
    """

    event_id: str
    symbol: str
    event_type: EventType
    ex_date: str | None
    source: SourceEvidence
    validation_state: ValidationState
    validation_reason: str | None = None
    event_date: str | None = None
    record_date: str | None = None
    announcement_date: str | None = None
    new_shares: int | None = None
    base_shares: int | None = None
    cash_amount_inr: Decimal | str | None = None
    reference_price_inr: Decimal | str | None = None
    subscription_price_inr: Decimal | str | None = None
    stated_price_factor: Decimal | str | None = None

    def __post_init__(self) -> None:
        _required(self.event_id, "event_id")
        _required(self.symbol, "symbol")
        try:
            event_type = EventType(self.event_type)
            state = ValidationState(self.validation_state)
        except ValueError as exc:
            raise ValueError("unsupported event_type or validation_state") from exc
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "validation_state", state)
        _day(self.ex_date, "ex_date", required=True)
        for name in ("event_date", "record_date", "announcement_date"):
            _day(getattr(self, name), name)
        if (self.new_shares is None) != (self.base_shares is None):
            raise ValueError("new_shares and base_shares must be supplied together")
        if self.new_shares is not None:
            if type(self.new_shares) is not int or type(self.base_shares) is not int:
                raise ValueError("share ratios must use integers")
            if self.new_shares <= 0 or self.base_shares <= 0:
                raise ValueError("share ratios must be positive")
        for name in ("cash_amount_inr", "reference_price_inr", "subscription_price_inr", "stated_price_factor"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal_value(value, name))
        if state != ValidationState.VALIDATED and not self.validation_reason:
            raise ValueError("non-validated events require validation_reason")
        if state == ValidationState.VALIDATED and self.source.licence_status != LicenceStatus.APPROVED_FOR_DERIVATION:
            raise ValueError("VALIDATED events require a derivation-approved licence")
        if event_type in COMPLEX_TYPES and state != ValidationState.UNSUPPORTED:
            raise ValueError("mergers, demergers and symbol changes must remain UNSUPPORTED")
        if state == ValidationState.VALIDATED:
            if event_type in {EventType.STOCK_SPLIT, EventType.REVERSE_SPLIT, EventType.BONUS}:
                if self.new_shares is None:
                    raise ValueError(f"{event_type.value} requires an explicit share ratio")
                assert self.base_shares is not None
                if event_type == EventType.STOCK_SPLIT and self.new_shares <= self.base_shares:
                    raise ValueError("STOCK_SPLIT must increase the shares outstanding")
                if event_type == EventType.REVERSE_SPLIT and self.new_shares >= self.base_shares:
                    raise ValueError("REVERSE_SPLIT must decrease the shares outstanding")
            if event_type == EventType.CASH_DIVIDEND:
                if self.cash_amount_inr is None or self.reference_price_inr is None:
                    raise ValueError("CASH_DIVIDEND requires cash amount and cum-dividend reference price")
                if self.cash_amount_inr >= self.reference_price_inr:
                    raise ValueError("cash dividend must be smaller than its reference price")
            if event_type == EventType.RIGHTS and (
                self.new_shares is None or self.subscription_price_inr is None
            ):
                raise ValueError("RIGHTS requires ratio and subscription price metadata")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id, "symbol": self.symbol,
            "event_type": self.event_type.value, "ex_date": self.ex_date,
            "event_date": self.event_date, "record_date": self.record_date,
            "announcement_date": self.announcement_date, "source": self.source.to_dict(),
            "validation_state": self.validation_state.value,
            "validation_reason": self.validation_reason,
            "new_shares": self.new_shares, "base_shares": self.base_shares,
        }
        for name in ("cash_amount_inr", "reference_price_inr", "subscription_price_inr", "stated_price_factor"):
            value = getattr(self, name)
            result[name] = None if value is None else str(value)
        return result


@dataclass(frozen=True)
class RawCandle:
    trading_date: str
    open: Decimal | str
    high: Decimal | str
    low: Decimal | str
    close: Decimal | str
    volume: Decimal | str | int

    def __post_init__(self) -> None:
        _day(self.trading_date, "trading_date", required=True)
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name))
        object.__setattr__(self, "volume", decimal_value(self.volume, "volume", positive=False))
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("OHLC bounds are inconsistent")

    def to_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in ("trading_date", "open", "high", "low", "close", "volume")}


@dataclass(frozen=True)
class CandleSeries:
    symbol: str
    source_series_id: str
    candles: tuple[RawCandle, ...]
    basis: SeriesBasis = SeriesBasis.RAW_UNADJUSTED
    prior_adjustment_id: str | None = None

    def __post_init__(self) -> None:
        _required(self.symbol, "symbol")
        _required(self.source_series_id, "source_series_id")
        object.__setattr__(self, "basis", SeriesBasis(self.basis))
        if not self.candles:
            raise ValueError("candle series must not be empty")
        days = [item.trading_date for item in self.candles]
        if days != sorted(days) or len(days) != len(set(days)):
            raise ValueError("candles must have unique, strictly ascending dates")
        if self.basis == SeriesBasis.DERIVED_ADJUSTED and not self.prior_adjustment_id:
            raise ValueError("derived series requires prior_adjustment_id")

    @property
    def sha256(self) -> str:
        payload = {"symbol": self.symbol, "source_series_id": self.source_series_id,
                   "basis": self.basis.value, "candles": [item.to_dict() for item in self.candles]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class AdjustedCandle:
    trading_date: str
    raw: RawCandle
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    adjusted_volume: Decimal
    cumulative_price_factor: str
    cumulative_volume_factor: str
    applied_event_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date, "raw": self.raw.to_dict(),
            "adjusted": {"open": str(self.adjusted_open), "high": str(self.adjusted_high),
                         "low": str(self.adjusted_low), "close": str(self.adjusted_close),
                         "volume": str(self.adjusted_volume)},
            "cumulative_price_factor": self.cumulative_price_factor,
            "cumulative_volume_factor": self.cumulative_volume_factor,
            "applied_event_ids": list(self.applied_event_ids),
        }


@dataclass(frozen=True)
class AdjustmentResult:
    contract: str
    adjustment_id: str
    symbol: str
    source_series_id: str
    source_series_sha256: str
    basis: SeriesBasis
    mode: AdjustmentMode
    knowledge_at: str
    candles: tuple[AdjustedCandle, ...]
    event_provenance: tuple[dict[str, Any], ...]
    safeguards: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "adjustment_id": self.adjustment_id, "symbol": self.symbol,
            "source_series_id": self.source_series_id, "source_series_sha256": self.source_series_sha256,
            "basis": self.basis.value, "mode": self.mode.value, "knowledge_at": self.knowledge_at,
            "candles": [item.to_dict() for item in self.candles],
            "event_provenance": list(self.event_provenance), "safeguards": self.safeguards,
        }
