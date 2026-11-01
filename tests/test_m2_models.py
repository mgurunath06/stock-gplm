from __future__ import annotations

from datetime import date

import pytest

from stock_gplm.m2 import (
    CalendarDay,
    CorporateAction,
    CorporateActionCatalog,
    HistoricalAlias,
    Instrument,
    InstrumentInventory,
    NSETradingCalendar,
    Provenance,
)


def _provenance(source: str = "synthetic-test") -> Provenance:
    return Provenance(source=source, as_of_date="2026-10-31", reference="fixture")


def test_inventory_preserves_unknown_values_as_null():
    instrument = Instrument(
        nse_symbol="UNKNOWN-EQ",
        field_provenance={"nse_symbol": _provenance()},
    )
    inventory = InstrumentInventory(as_of_date="2026-10-31", instruments=(instrument,))
    output = inventory.to_dict()["instruments"][0]
    assert output["isin"] is None
    assert output["shoonya_token"] is None
    assert output["listing_date"] is None
    assert output["membership_verified_through"] is None
    assert instrument.membership_on(date(2020, 1, 1)) == "UNKNOWN"


def test_known_fields_require_provenance():
    with pytest.raises(ValueError, match="listing_date"):
        Instrument(
            nse_symbol="TEST-EQ",
            listing_date="2020-01-01",
            field_provenance={"nse_symbol": _provenance()},
        )


def test_listing_date_alone_does_not_imply_indefinite_membership():
    provenance = _provenance()
    instrument = Instrument(
        nse_symbol="TEST-EQ", listing_date="2020-01-01",
        field_provenance={"nse_symbol": provenance, "listing_date": provenance},
    )
    assert instrument.membership_on(date(2021, 1, 1)) == "UNKNOWN"


def test_point_in_time_membership_and_alias():
    provenance = _provenance()
    instrument = Instrument(
        nse_symbol="NEW-EQ",
        isin="IN1234567890",
        shoonya_token="12345",
        listing_date="2020-01-01",
        delisting_date="2026-12-31",
        aliases=(HistoricalAlias("OLD-EQ", "2020-01-01", "2022-12-31", provenance),),
        field_provenance={
            "nse_symbol": provenance,
            "isin": provenance,
            "shoonya_token": provenance,
            "listing_date": provenance,
            "delisting_date": provenance,
        },
    )
    assert instrument.membership_on(date(2019, 12, 31)) == "PRE_LISTING"
    assert instrument.membership_on(date(2021, 1, 1)) == "LISTED"
    assert instrument.membership_on(date(2027, 1, 1)) == "DELISTED"
    assert instrument.symbol_on(date(2021, 1, 1)) == "OLD-EQ"
    assert instrument.symbol_on(date(2024, 1, 1)) == "NEW-EQ"
    inventory = InstrumentInventory(as_of_date="2026-10-31", instruments=(instrument,))
    assert inventory.resolve("OLD-EQ", date(2021, 1, 1)) is instrument
    assert inventory.resolve("OLD-EQ", date(2024, 1, 1)) is None


def test_overlapping_alias_ranges_are_rejected():
    provenance = _provenance()
    with pytest.raises(ValueError, match="alias validity ranges overlap"):
        Instrument(
            nse_symbol="NEW-EQ", listing_date="2020-01-01",
            membership_verified_through="2023-12-31",
            aliases=(
                HistoricalAlias("OLD1-EQ", "2020-01-01", "2022-12-31", provenance),
                HistoricalAlias("OLD2-EQ", "2022-01-01", "2023-12-31", provenance),
            ),
            field_provenance={
                "nse_symbol": provenance, "listing_date": provenance,
                "membership_verified_through": provenance,
            },
        )


def test_inventory_allows_shared_isin_but_rejects_duplicate_token():
    provenance = _provenance()
    records = tuple(
        Instrument(
            nse_symbol=f"TEST{index}-EQ",
            isin="IN1234567890", shoonya_token="12345",
            field_provenance={
                "nse_symbol": provenance, "isin": provenance,
                "shoonya_token": provenance,
            },
        )
        for index in range(2)
    )
    with pytest.raises(ValueError, match="duplicate known instrument.shoonya_token"):
        InstrumentInventory(as_of_date="2026-10-31", instruments=records)


def test_calendar_never_infers_unsupplied_dates():
    calendar = NSETradingCalendar(
        days=(CalendarDay("2026-10-01", "TRADING_SESSION", _provenance()),)
    )
    assert calendar.status_on(date(2026, 10, 1)) == "TRADING_SESSION"
    assert calendar.status_on(date(2026, 10, 2)) == "UNKNOWN"


def test_complete_calendar_requires_every_calendar_date():
    with pytest.raises(ValueError, match="omits 2026-10-02"):
        NSETradingCalendar(
            coverage_start="2026-10-01",
            coverage_end="2026-10-03",
            coverage_complete=True,
            days=(
                CalendarDay("2026-10-01", "TRADING_SESSION", _provenance()),
                CalendarDay("2026-10-03", "MARKET_CLOSED", _provenance()),
            ),
        )


def test_special_session_is_explicit_expected_session():
    calendar = NSETradingCalendar(
        days=(CalendarDay("2026-11-01", "SPECIAL_SESSION", _provenance(), "fixture"),)
    )
    assert calendar.status_on(date(2026, 11, 1)) == "SPECIAL_SESSION"


def test_corporate_action_is_metadata_only():
    action = CorporateAction(
        symbol="TEST-EQ",
        action_type="SPLIT",
        effective_date="2026-05-01",
        adjustment_status="POLICY_REQUIRED",
        provenance=_provenance("synthetic-corporate-action"),
        details={"ratio": "2:1"},
    )
    assert action.adjustment_status == "POLICY_REQUIRED"
    assert action.details == {"ratio": "2:1"}


def test_empty_corporate_action_catalog_needs_provenance_to_prove_coverage():
    with pytest.raises(ValueError, match="bounds and provenance"):
        CorporateActionCatalog("TEST-EQ", "COMPLETE")
    catalog = CorporateActionCatalog(
        "TEST-EQ", "COMPLETE", "2026-01-01", "2026-12-31", _provenance(), ()
    )
    assert catalog.covers(date(2026, 2, 1), date(2026, 3, 1))
