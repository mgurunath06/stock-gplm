"""Pure deterministic price/volume adjustment functions for M3.9-B."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Iterable, Sequence

from .models import (
    AdjustmentMode, AdjustmentResult, AdjustedCandle, CandleSeries, CorporateActionEvent,
    EventType, LicenceStatus, RawCandle, SeriesBasis, ValidationState, decimal_value,
    parse_timestamp,
)

OUTPUT_QUANTUM = Decimal("0.00000001")
SUPPORTED = frozenset({EventType.STOCK_SPLIT, EventType.REVERSE_SPLIT, EventType.BONUS, EventType.CASH_DIVIDEND})


class AdjustmentRefused(ValueError):
    """An event or series is not safe and sufficient for adjustment."""


class LookaheadError(AdjustmentRefused):
    """Evidence was not available at the requested knowledge timestamp."""


class ConflictingEvents(AdjustmentRefused):
    """Sources disagree about one economic event; the group is quarantined."""


class DuplicateEvents(AdjustmentRefused):
    """The same source event occurs more than once and could be reapplied."""


def _positive_ratio(new_shares: int, base_shares: int) -> tuple[int, int]:
    if type(new_shares) is not int or type(base_shares) is not int or new_shares <= 0 or base_shares <= 0:
        raise ValueError("share ratio terms must be positive integers")
    return new_shares, base_shares


def split_factor(*, new_shares: int, old_shares: int) -> Fraction:
    """Backward price factor: old shares / resulting new shares."""
    new, old = _positive_ratio(new_shares, old_shares)
    return Fraction(old, new)


def bonus_factor(*, bonus_shares: int, held_shares: int) -> Fraction:
    """Backward price factor for bonus shares: held / (held + bonus)."""
    bonus, held = _positive_ratio(bonus_shares, held_shares)
    return Fraction(held, held + bonus)


def dividend_total_return_factor(*, cash_amount_inr: Decimal | str,
                                 cum_dividend_price_inr: Decimal | str) -> Fraction:
    """Backward total-return factor ``(cum price - cash) / cum price``.

    The caller must provide a validated pre-ex-date reference price. A cash
    dividend is deliberately not a price-return adjustment.
    """
    cash = decimal_value(cash_amount_inr, "cash_amount_inr")
    price = decimal_value(cum_dividend_price_inr, "cum_dividend_price_inr")
    if cash >= price:
        raise ValueError("dividend and reference price must satisfy 0 < cash < price")
    return Fraction(price - cash) / Fraction(price)


def _factor(event: CorporateActionEvent, mode: AdjustmentMode) -> tuple[Fraction, Fraction, str]:
    if event.event_type in {EventType.STOCK_SPLIT, EventType.REVERSE_SPLIT}:
        if event.new_shares is None or event.base_shares is None:
            raise AdjustmentRefused(f"{event.event_id} lacks an explicit split ratio")
        price = split_factor(new_shares=event.new_shares, old_shares=event.base_shares)
        disposition = "APPLIED_SPLIT_PRICE_AND_VOLUME"
        volume = 1 / price
    elif event.event_type == EventType.BONUS:
        if event.new_shares is None or event.base_shares is None:
            raise AdjustmentRefused(f"{event.event_id} lacks an explicit bonus ratio")
        price = bonus_factor(bonus_shares=event.new_shares, held_shares=event.base_shares)
        disposition = "APPLIED_BONUS_PRICE_AND_VOLUME"
        volume = 1 / price
    elif event.event_type == EventType.CASH_DIVIDEND:
        if mode == AdjustmentMode.PRICE_RETURN:
            return Fraction(1), Fraction(1), "EXCLUDED_FROM_PRICE_RETURN"
        if event.cash_amount_inr is None or event.reference_price_inr is None:
            raise AdjustmentRefused(f"{event.event_id} lacks explicit dividend economics")
        price = dividend_total_return_factor(
            cash_amount_inr=event.cash_amount_inr,
            cum_dividend_price_inr=event.reference_price_inr,
        )
        volume = Fraction(1)
        disposition = "APPLIED_TOTAL_RETURN_PRICE_ONLY"
    else:
        raise AdjustmentRefused(f"{event.event_type.value} adjustment is unsupported")
    if event.stated_price_factor is not None and Fraction(event.stated_price_factor) != price:
        raise ConflictingEvents(f"{event.event_id} stated factor disagrees with its event terms")
    return price, volume, disposition


def _signature(event: CorporateActionEvent, mode: AdjustmentMode) -> tuple[object, ...]:
    price, volume, _ = _factor(event, mode)
    return (price, volume, event.new_shares, event.base_shares, event.cash_amount_inr,
            event.reference_price_inr, event.subscription_price_inr)


@dataclass(frozen=True)
class _Resolved:
    event: CorporateActionEvent
    evidence: tuple[CorporateActionEvent, ...]
    price_factor: Fraction
    volume_factor: Fraction
    disposition: str


def _resolve(events: Sequence[CorporateActionEvent], *, symbol: str, mode: AdjustmentMode,
             knowledge_at: str) -> tuple[_Resolved, ...]:
    cutoff = parse_timestamp(knowledge_at, "knowledge_at")
    ids: set[str] = set()
    source_keys: set[tuple[str, str]] = set()
    groups: dict[tuple[EventType, str], list[CorporateActionEvent]] = {}
    for event in events:
        if event.event_id in ids or (event.source.source, event.source.source_identifier) in source_keys:
            raise DuplicateEvents(f"duplicate event/source identifier: {event.event_id}")
        ids.add(event.event_id)
        source_keys.add((event.source.source, event.source.source_identifier))
        if event.symbol != symbol:
            raise AdjustmentRefused(f"event {event.event_id} belongs to another symbol")
        if event.validation_state != ValidationState.VALIDATED:
            raise AdjustmentRefused(
                f"event {event.event_id} is {event.validation_state.value}: {event.validation_reason}"
            )
        if event.source.licence_status != LicenceStatus.APPROVED_FOR_DERIVATION:
            raise AdjustmentRefused(f"event {event.event_id} is not licensed for derivation")
        if parse_timestamp(event.source.available_at, "available_at") > cutoff:
            raise LookaheadError(f"event {event.event_id} was unavailable at {knowledge_at}")
        if event.event_type not in SUPPORTED:
            raise AdjustmentRefused(f"{event.event_type.value} adjustment is unsupported")
        if event.ex_date is None:
            raise AdjustmentRefused(f"event {event.event_id} lacks an ex-date")
        if date.fromisoformat(event.ex_date) > cutoff.date():
            continue  # known announcement, but the action is not effective at this point in time
        groups.setdefault((event.event_type, event.ex_date), []).append(event)
    resolved: list[_Resolved] = []
    for key, records in groups.items():
        signatures = {_signature(item, mode) for item in records}
        if len(signatures) != 1:
            raise ConflictingEvents(f"conflicting sources for {key[0].value} on {key[1]}")
        representative = min(records, key=lambda item: (item.source.source, item.event_id))
        price, volume, disposition = _factor(representative, mode)
        resolved.append(_Resolved(representative, tuple(sorted(records, key=lambda item: item.event_id)),
                                  price, volume, disposition))
    return tuple(sorted(resolved, key=lambda item: (item.event.ex_date, item.event.event_type.value,
                                                     item.event.event_id)))


def _decimal(value: Decimal, factor: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        adjusted = value * Decimal(factor.numerator) / Decimal(factor.denominator)
        return adjusted.quantize(OUTPUT_QUANTUM, rounding=ROUND_HALF_EVEN).normalize()


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def adjust_series(series: CandleSeries, events: Sequence[CorporateActionEvent], *,
                  mode: AdjustmentMode, knowledge_at: str) -> AdjustmentResult:
    """Return a separate backward-adjusted series; never mutate source candles.

    Only candles strictly before an action's ex-date receive that action. The
    cumulative factor is formed exactly as a :class:`Fraction`; output values are
    rounded once to eight decimal places using ROUND_HALF_EVEN.
    """
    mode = AdjustmentMode(mode)
    if series.basis != SeriesBasis.RAW_UNADJUSTED:
        raise AdjustmentRefused(
            f"input basis is {series.basis.value}; only RAW_UNADJUSTED series may be adjusted"
        )
    cutoff = parse_timestamp(knowledge_at, "knowledge_at")
    if date.fromisoformat(series.candles[-1].trading_date) > cutoff.date():
        raise LookaheadError("candle series contains observations after knowledge_at")
    resolved = _resolve(events, symbol=series.symbol, mode=mode, knowledge_at=knowledge_at)
    adjusted: list[AdjustedCandle] = []
    for candle in series.candles:
        applicable = [item for item in resolved if candle.trading_date < item.event.ex_date]
        price = Fraction(1)
        volume = Fraction(1)
        for item in applicable:
            price *= item.price_factor
            volume *= item.volume_factor
        adjusted.append(AdjustedCandle(
            candle.trading_date, candle,
            _decimal(candle.open, price), _decimal(candle.high, price),
            _decimal(candle.low, price), _decimal(candle.close, price),
            _decimal(candle.volume, volume), _fraction_text(price), _fraction_text(volume),
            tuple(item.event.event_id for item in applicable if item.disposition != "EXCLUDED_FROM_PRICE_RETURN"),
        ))
    provenance = tuple({
        "canonical_event_id": item.event.event_id, "event_type": item.event.event_type.value,
        "ex_date": item.event.ex_date, "price_factor": _fraction_text(item.price_factor),
        "volume_factor": _fraction_text(item.volume_factor), "disposition": item.disposition,
        "evidence": [record.to_dict() for record in item.evidence],
    } for item in resolved)
    identity = {
        "contract": "stock-gplm.m39b-adjustment.v1", "source_sha256": series.sha256,
        "mode": mode.value, "knowledge_at": knowledge_at, "events": provenance,
    }
    adjustment_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return AdjustmentResult(
        "stock-gplm.m39b-adjustment.v1", adjustment_id, series.symbol,
        series.source_series_id, series.sha256, SeriesBasis.DERIVED_ADJUSTED, mode,
        knowledge_at, tuple(adjusted), provenance,
        {"raw_mutated": False, "duplicate_application_guard": "RAW_BASIS_REQUIRED",
         "point_in_time_cutoff_enforced": True, "training_authorized": False,
         "holdout_accessed": False, "network_used": False,
         "rounding": "8_DECIMAL_PLACES_HALF_EVEN"},
    )


def detect_suspicious_discontinuities(candles: Iterable[RawCandle], *,
                                      threshold: Decimal | str = Decimal("0.20")) -> tuple[dict[str, str], ...]:
    """Flag close-to-next-open jumps without assigning a cause."""
    limit = decimal_value(threshold, "threshold")
    rows = tuple(candles)
    findings: list[dict[str, str]] = []
    for previous, current in zip(rows, rows[1:]):
        change = (current.open / previous.close) - Decimal(1)
        if abs(change) >= limit:
            findings.append({
                "previous_date": previous.trading_date, "current_date": current.trading_date,
                "previous_close": str(previous.close), "current_open": str(current.open),
                "open_gap_return": str(change), "classification": "SUSPICIOUS_DISCONTINUITY",
                "attributed_cause": "UNKNOWN",
            })
    return tuple(findings)
