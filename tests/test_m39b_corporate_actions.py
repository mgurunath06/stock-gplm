"""Synthetic-only tests for the isolated M3.9-B corporate-action engine."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from stock_gplm.m39b import (
    AdjustmentMode, AdjustmentRefused, CandleSeries, ConflictingEvents,
    CorporateActionEvent, DuplicateEvents, EventType, LicenceStatus, LookaheadError,
    RawCandle, SeriesBasis, SourceEvidence, ValidationState, adjust_series, bonus_factor,
    detect_suspicious_discontinuities, dividend_total_return_factor, split_factor,
)


KNOWLEDGE = "2026-04-01T00:00:00Z"


def evidence(identifier: str = "notice-1", *, available: str = "2026-01-01T06:00:00Z",
             licence: LicenceStatus = LicenceStatus.APPROVED_FOR_DERIVATION) -> SourceEvidence:
    return SourceEvidence("SYNTHETIC_FIXTURE", identifier, licence, available, "2026-06-01T06:00:00Z")


def event(kind: EventType, ex_date: str = "2026-02-01", *, event_id: str = "event-1",
          source: SourceEvidence | None = None, new: int | None = None, base: int | None = None,
          cash: str | None = None, reference: str | None = None,
          subscription: str | None = None, factor: str | None = None,
          state: ValidationState = ValidationState.VALIDATED,
          reason: str | None = None) -> CorporateActionEvent:
    return CorporateActionEvent(
        event_id, "TEST-EQ", kind, ex_date, source or evidence(event_id), state, reason,
        event_date=ex_date, record_date=ex_date, announcement_date="2026-01-01",
        new_shares=new, base_shares=base, cash_amount_inr=cash,
        reference_price_inr=reference, subscription_price_inr=subscription,
        stated_price_factor=factor,
    )


def split(ex_date: str = "2026-02-01", *, event_id: str = "split", new: int = 2,
          base: int = 1, source: SourceEvidence | None = None) -> CorporateActionEvent:
    return event(EventType.STOCK_SPLIT, ex_date, event_id=event_id, source=source, new=new, base=base)


def series(*rows: tuple[str, str, str], basis: SeriesBasis = SeriesBasis.RAW_UNADJUSTED) -> CandleSeries:
    candles = tuple(RawCandle(day, price, price, price, price, volume) for day, price, volume in rows)
    return CandleSeries("TEST-EQ", "synthetic-series", candles, basis,
                        "prior-adjustment" if basis == SeriesBasis.DERIVED_ADJUSTED else None)


def test_factor_formulas_cover_one_to_two_one_to_five_reverse_and_bonus():
    assert split_factor(new_shares=2, old_shares=1) == Decimal("0.5")
    assert split_factor(new_shares=5, old_shares=1) == Decimal("0.2")
    assert split_factor(new_shares=1, old_shares=5) == 5
    assert bonus_factor(bonus_shares=1, held_shares=1) == Decimal("0.5")
    assert bonus_factor(bonus_shares=1, held_shares=2) == pytest.approx(2 / 3)


@pytest.mark.parametrize("function,kwargs", [
    (split_factor, {"new_shares": 0, "old_shares": 1}),
    (split_factor, {"new_shares": 2, "old_shares": -1}),
    (bonus_factor, {"bonus_shares": 0, "held_shares": 1}),
])
def test_invalid_ratios_are_rejected(function, kwargs):
    with pytest.raises(ValueError, match="positive integers"):
        function(**kwargs)


def test_split_type_must_agree_with_ratio_direction():
    with pytest.raises(ValueError, match="increase"):
        split(new=1, base=2)
    with pytest.raises(ValueError, match="decrease"):
        event(EventType.REVERSE_SPLIT, new=2, base=1)


def test_stock_split_adjusts_only_pre_ex_date_and_preserves_raw_objects():
    raw = series(("2026-01-30", "100", "10"), ("2026-02-01", "50", "20"))
    before = raw.candles
    result = adjust_series(raw, [split()], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert result.candles[0].adjusted_close == 50
    assert result.candles[0].adjusted_volume == 20
    assert result.candles[0].cumulative_price_factor == "1/2"
    assert result.candles[1].adjusted_close == 50
    assert result.candles[1].adjusted_volume == 20
    assert raw.candles == before and result.candles[0].raw is raw.candles[0]
    assert result.safeguards["raw_mutated"] is False


def test_one_to_five_and_reverse_split_volume_factors_are_reciprocals():
    raw = series(("2026-01-01", "100", "10"), ("2026-02-01", "20", "50"))
    forward = adjust_series(raw, [split(new=5)], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert forward.candles[0].adjusted_close == 20
    assert forward.candles[0].adjusted_volume == 50
    reverse = event(EventType.REVERSE_SPLIT, event_id="reverse", new=1, base=5)
    consolidated = adjust_series(raw, [reverse], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert consolidated.candles[0].adjusted_close == 500
    assert consolidated.candles[0].adjusted_volume == 2


@pytest.mark.parametrize("new,held,expected_price,expected_volume", [
    (1, 1, Decimal("50"), Decimal("20")),
    (1, 2, Decimal("66.66666667"), Decimal("15")),
])
def test_bonus_adjustments(new, held, expected_price, expected_volume):
    raw = series(("2026-01-01", "100", "10"), ("2026-02-01", "50", "20"))
    bonus = event(EventType.BONUS, event_id=f"bonus-{new}-{held}", new=new, base=held)
    result = adjust_series(raw, [bonus], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert result.candles[0].adjusted_close == expected_price
    assert result.candles[0].adjusted_volume == expected_volume


def test_multiple_actions_use_exclusive_boundaries_and_exact_cumulative_order():
    raw = series(("2026-01-01", "120", "10"), ("2026-02-01", "60", "20"),
                 ("2026-03-01", "30", "40"))
    events = [split("2026-02-01", event_id="a", new=2),
              event(EventType.BONUS, "2026-03-01", event_id="b", new=1, base=1)]
    result = adjust_series(raw, events, mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert [row.adjusted_close for row in result.candles] == [Decimal("30"), Decimal("30"), Decimal("30")]
    assert [row.adjusted_volume for row in result.candles] == [Decimal("40"), Decimal("40"), Decimal("40")]
    assert result.candles[0].applied_event_ids == ("a", "b")
    assert result.candles[1].applied_event_ids == ("b",)
    assert result.candles[2].applied_event_ids == ()


def test_dividend_is_excluded_from_price_return_and_applied_to_total_return_only():
    raw = series(("2026-01-31", "100", "10"), ("2026-02-01", "90", "12"))
    dividend = event(EventType.CASH_DIVIDEND, cash="10", reference="100")
    price = adjust_series(raw, [dividend], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    total = adjust_series(raw, [dividend], mode=AdjustmentMode.TOTAL_RETURN, knowledge_at=KNOWLEDGE)
    assert price.candles[0].adjusted_close == 100 and price.candles[0].applied_event_ids == ()
    assert price.event_provenance[0]["disposition"] == "EXCLUDED_FROM_PRICE_RETURN"
    assert total.candles[0].adjusted_close == 90 and total.candles[0].adjusted_volume == 10
    assert dividend_total_return_factor(cash_amount_inr="10", cum_dividend_price_inr="100") == Decimal("0.9")


@pytest.mark.parametrize("cash,reference", [("0", "100"), ("100", "100"), ("101", "100"), ("1", "0")])
def test_invalid_dividend_economics_are_rejected(cash, reference):
    with pytest.raises(ValueError):
        dividend_total_return_factor(cash_amount_inr=cash, cum_dividend_price_inr=reference)


def test_zero_prices_bad_ohlc_and_negative_volume_are_rejected():
    with pytest.raises(ValueError, match="positive"):
        RawCandle("2026-01-01", "0", "1", "1", "1", 1)
    with pytest.raises(ValueError, match="bounds"):
        RawCandle("2026-01-01", "10", "9", "8", "10", 1)
    with pytest.raises(ValueError, match="nonnegative"):
        RawCandle("2026-01-01", "10", "10", "10", "10", -1)


def test_missing_ex_date_and_ambiguous_metadata_are_rejected_not_guessed():
    with pytest.raises(ValueError, match="ex_date is required"):
        event(EventType.STOCK_SPLIT, ex_date=None, new=2, base=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="explicit share ratio"):
        event(EventType.STOCK_SPLIT)
    unknown = event(EventType.BONUS, state=ValidationState.UNKNOWN, reason="ratio absent")
    assert unknown.validation_state == ValidationState.UNKNOWN
    with pytest.raises(AdjustmentRefused, match="UNKNOWN"):
        adjust_series(series(("2026-01-01", "100", "1")), [unknown],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_conflicting_sources_quarantine_the_event_group():
    first = split(source=evidence("source-a"), event_id="a", new=2)
    second = split(source=evidence("source-b"), event_id="b", new=5)
    with pytest.raises(ConflictingEvents, match="conflicting sources"):
        adjust_series(series(("2026-01-01", "100", "1")), [first, second],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_corroborating_sources_are_deduplicated_with_complete_provenance():
    first = split(source=evidence("source-a"), event_id="a")
    second = split(source=evidence("source-b"), event_id="b")
    result = adjust_series(series(("2026-01-01", "100", "1")), [second, first],
                           mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert result.candles[0].adjusted_close == 50
    assert len(result.event_provenance) == 1
    assert [item["event_id"] for item in result.event_provenance[0]["evidence"]] == ["a", "b"]


def test_duplicate_source_event_is_rejected_before_application():
    duplicate = split()
    with pytest.raises(DuplicateEvents, match="duplicate"):
        adjust_series(series(("2026-01-01", "100", "1")), [duplicate, duplicate],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_point_in_time_cutoff_blocks_unavailable_evidence_and_future_candles():
    late = split(source=evidence("late", available="2026-04-02T00:00:00Z"))
    with pytest.raises(LookaheadError, match="unavailable"):
        adjust_series(series(("2026-01-01", "100", "1")), [late],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    with pytest.raises(LookaheadError, match="observations after"):
        adjust_series(series(("2026-04-02", "100", "1")), [],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_announced_but_not_yet_effective_event_is_not_applied():
    future = split("2026-05-01", source=evidence("future", available="2026-03-01T00:00:00Z"))
    result = adjust_series(series(("2026-03-01", "100", "1")), [future],
                           mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert result.candles[0].adjusted_close == 100 and result.event_provenance == ()


def test_adjusted_unknown_series_mixing_and_reapplication_are_refused():
    rows = (("2026-01-01", "100", "1"),)
    with pytest.raises(AdjustmentRefused, match="DERIVED_ADJUSTED"):
        adjust_series(series(*rows, basis=SeriesBasis.DERIVED_ADJUSTED), [split()],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    with pytest.raises(AdjustmentRefused, match="UNKNOWN"):
        adjust_series(series(*rows, basis=SeriesBasis.UNKNOWN), [split()],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_adjustment_is_deterministic_and_idempotency_marker_is_stable():
    raw = series(("2026-01-01", "100", "10"), ("2026-02-01", "50", "20"))
    first = adjust_series(raw, [split()], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    second = adjust_series(raw, [split()], mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert first.to_dict() == second.to_dict()
    assert first.adjustment_id == second.adjustment_id
    assert first.basis == SeriesBasis.DERIVED_ADJUSTED


@pytest.mark.parametrize("kind", [EventType.RIGHTS, EventType.MERGER, EventType.DEMERGER, EventType.SYMBOL_CHANGE])
def test_rights_and_complex_restructurings_fail_safely(kind):
    if kind == EventType.RIGHTS:
        action = event(kind, new=1, base=5, subscription="80")
    else:
        action = event(kind, state=ValidationState.UNSUPPORTED, reason="methodology not validated")
    with pytest.raises(AdjustmentRefused, match="unsupported|UNSUPPORTED"):
        adjust_series(series(("2026-01-01", "100", "1")), [action],
                      mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)


def test_reference_only_nse_mcp_fact_cannot_be_promoted_to_validated_event():
    with pytest.raises(ValueError, match="derivation-approved"):
        split(source=evidence("mcp", licence=LicenceStatus.REFERENCE_ONLY))


def test_suspicious_discontinuity_remains_unattributed():
    raw = series(("2026-01-01", "100", "1"), ("2026-01-02", "50", "2"))
    findings = detect_suspicious_discontinuities(raw.candles)
    assert findings[0]["classification"] == "SUSPICIOUS_DISCONTINUITY"
    assert findings[0]["attributed_cause"] == "UNKNOWN"


def test_canonical_records_and_raw_candles_are_immutable():
    action = split()
    with pytest.raises(FrozenInstanceError):
        action.ex_date = "2026-02-02"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        series(("2026-01-01", "100", "1")).candles[0].close = Decimal("1")  # type: ignore[misc]


def test_engine_has_no_training_or_holdout_side_effects():
    result = adjust_series(series(("2026-01-01", "100", "1")), [split()],
                           mode=AdjustmentMode.PRICE_RETURN, knowledge_at=KNOWLEDGE)
    assert result.safeguards["training_authorized"] is False
    assert result.safeguards["holdout_accessed"] is False
    assert result.safeguards["network_used"] is False


def test_training_research_and_mcp_modules_do_not_import_m39b():
    source = Path(__file__).parents[1] / "src" / "stock_gplm"
    for package in ("m3", "m35", "m37", "m38", "m39"):
        for path in (source / package).glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "stock_gplm.m39b" not in text and "from .m39b" not in text and "import m39b" not in text


def test_m39b_contains_no_filesystem_network_or_training_entry_point():
    package = Path(__file__).parents[1] / "src" / "stock_gplm" / "m39b"
    sources = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
    assert "from pathlib" not in sources and "import requests" not in sources
    assert "import http" not in sources and "import urllib" not in sources
    assert "def main(" not in sources and "lightgbm" not in sources.lower()
