"""M3.2 contracts on synthetic fixtures only; nothing here is market evidence."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, getcontext, localcontext
from pathlib import Path

import pytest

from stock_gplm.m2.models import (
    CalendarDay, CorporateAction, CorporateActionCatalog, HistoricalAlias, Instrument,
    InstrumentInventory, NSETradingCalendar, Provenance,
)
from stock_gplm.m3.evidence import ReferenceEvidence, SessionAxis
from stock_gplm.m3.features import FEATURE_NAMES, FEATURE_SPECS, compute_symbol_features
from stock_gplm.m3.intraday import (
    CONFIRMED_MINUTE_SCHEMAS, SYNTHETIC_FIXTURE_SCHEMA, MinuteBar, MinuteSeries, SessionBounds,
    build_intraday_labels,
)
from stock_gplm.m3.labels import (
    CONTRACTS_BY_NAME, DAILY_LABEL_CONTRACTS, INTRADAY_LABEL_CONTRACT, IntradayDataRequired,
    LabelRow, build_daily_labels,
)
from stock_gplm.m3.readiness import DatasetRow, build_readiness_run
from stock_gplm.m3.research import build_research_run, load_readiness_run
from stock_gplm.m3.splits import (
    LeakageError, Partition, WalkForwardPolicy, build_walk_forward, default_policy,
    select_samples, verify_plan,
)
from tests.test_m3_readiness import _hashes, _master, _publish

SHORT = CONTRACTS_BY_NAME["1-7d"]
PROVENANCE = Provenance("synthetic-test-fixture", "2026-01-01")


def _sessions(count: int) -> tuple[str, ...]:
    start = date(2020, 1, 1)
    return tuple((start + timedelta(days=index)).isoformat() for index in range(count))


def _axis(count: int) -> SessionAxis:
    return SessionAxis(_sessions(count), "EMPIRICAL_UNION")


def _row(day: str, close: Decimal, *, spread: Decimal = Decimal("2"), gap: Decimal = Decimal("0.5"), volume: int = 1000, symbol: str = "ALPHA-EQ") -> DatasetRow:
    opening = close - gap
    return DatasetRow(
        symbol=symbol, trading_date=day, source_timestamp_utc=f"{day}T00:00:00Z",
        open=f"{opening:.2f}", high=f"{max(opening, close) + spread:.2f}",
        low=f"{min(opening, close) - spread:.2f}", close=f"{close:.2f}", volume=volume,
        source_path=f"{symbol}/fixture.json.gz", source_sha256="0" * 64,
    )


def _series(count: int, *, seed: int = 7) -> list[DatasetRow]:
    generator = random.Random(seed)
    price = Decimal("500")
    rows = []
    for day in _sessions(count):
        price = max(Decimal("50"), price + Decimal(generator.randint(-1000, 1050)) / 100)
        rows.append(_row(
            day, price, spread=Decimal(generator.randint(10, 700)) / 100,
            gap=Decimal(generator.randint(-300, 300)) / 100, volume=generator.randint(100, 100000),
        ))
    return rows


def _poison(row: DatasetRow) -> DatasetRow:
    return replace(row, open="9000.00", high="99999.00", low="1.00", close="5000.00", volume=10**9)


# --- feature contract and warm-up ---------------------------------------------------


def test_registry_declares_every_requested_family_and_unique_columns():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
    required = (
        "ema_20", "sma_20", "rsi_14", "macd_line", "macd_signal", "atr_14", "bb_upper_20",
        "adx_14", "volume_ratio_20", "volatility_20", "momentum_21", "close_location",
    )
    assert set(required) <= set(FEATURE_NAMES)
    assert {item.family for item in FEATURE_SPECS} == {"trend", "momentum", "volatility", "volume", "structure"}
    assert set(compute_symbol_features(_series(3), _axis(3))[0].values) == set(FEATURE_NAMES)


def test_no_label_or_future_field_is_a_feature():
    forbidden = ("label", "forward", "future", "return_at", "excursion", "confidence", "probability")
    assert not [name for name in FEATURE_NAMES if any(word in name for word in forbidden)]


@pytest.mark.parametrize("spec", FEATURE_SPECS, ids=lambda item: item.name)
def test_documented_warm_up_is_exact(spec):
    rows = compute_symbol_features(_series(300), _axis(300))
    values = [item.values[spec.name] for item in rows]
    assert all(value is None for value in values[: spec.min_observations - 1])
    assert values[spec.min_observations - 1] is not None


def test_warm_up_applies_exactly_when_history_is_one_short():
    for spec in FEATURE_SPECS:
        if spec.min_observations > 1:
            count = spec.min_observations - 1
            last = compute_symbol_features(_series(count), _axis(count))[-1]
            assert last.values[spec.name] is None, spec.name


# --- causality ----------------------------------------------------------------------


@pytest.mark.parametrize("cut", [1, 14, 34, 60, 150, 299])
def test_poisoning_every_future_candle_cannot_change_any_past_feature(cut):
    clean = _series(320)
    poisoned = clean[:cut] + [_poison(item) for item in clean[cut:]]
    axis = _axis(320)
    before = compute_symbol_features(clean, axis)[:cut]
    assert before == compute_symbol_features(poisoned, axis)[:cut]


@pytest.mark.parametrize("cut", [5, 40, 120, 260])
def test_features_match_on_a_truncated_prefix(cut):
    rows = _series(300)
    axis = _axis(300)
    assert compute_symbol_features(rows[:cut], axis) == compute_symbol_features(rows, axis)[:cut]


def test_a_changed_candle_affects_only_its_own_session_onward():
    rows = _series(120)
    changed = list(rows)
    changed[70] = _poison(rows[70])
    left = compute_symbol_features(rows, _axis(120))
    right = compute_symbol_features(changed, _axis(120))
    assert left[:70] == right[:70]
    assert left[70].values["close_location"] != right[70].values["close_location"]


def test_bounded_lookback_forgets_a_candle_outside_its_window():
    rows = _series(300)
    changed = list(rows)
    changed[10] = _poison(rows[10])
    left = compute_symbol_features(rows, _axis(300))[-1].values
    right = compute_symbol_features(changed, _axis(300))[-1].values
    for spec in FEATURE_SPECS:
        if spec.lookback_sessions is not None and spec.lookback_sessions < 289:
            assert left[spec.name] == right[spec.name], spec.name


def test_output_is_deterministic_and_ignores_the_callers_decimal_context():
    rows = _series(200)
    expected = compute_symbol_features(rows, _axis(200))
    with localcontext() as context:
        context.prec = 5
        assert compute_symbol_features(rows, _axis(200)) == expected
    assert getcontext().prec != 5


# --- values, checked against independent float arithmetic ----------------------------


def _floats(rows):
    return (
        [float(item.open) for item in rows], [float(item.high) for item in rows],
        [float(item.low) for item in rows], [float(item.close) for item in rows],
        [float(item.volume) for item in rows],
    )


def _at(rows, name, index):
    value = compute_symbol_features(rows, _axis(len(rows)))[index].values[name]
    return None if value is None else float(value)


def test_simple_features_match_hand_calculation():
    rows = [_row(day, Decimal(100 + index), volume=1000 + index) for index, day in enumerate(_sessions(30))]
    assert _at(rows, "sma_5", 4) == pytest.approx(102)
    assert _at(rows, "momentum_5", 5) == pytest.approx(105 / 100 - 1, abs=1e-6)
    assert _at(rows, "gap_open", 1) == pytest.approx(100.5 / 100 - 1, abs=1e-6)
    assert _at(rows, "range_pct", 0) == pytest.approx((102 - 97.5) / 100, abs=1e-6)
    assert _at(rows, "close_location", 0) == pytest.approx(2.5 / 4.5, abs=1e-6)
    assert _at(rows, "volume_ratio_5", 5) == pytest.approx(1005 / 1002, abs=1e-6)


def test_volume_ratio_excludes_the_current_session_from_its_base():
    rows = [_row(day, Decimal(100), volume=1000) for day in _sessions(6)]
    rows[5] = replace(rows[5], volume=5000)
    assert _at(rows, "volume_ratio_5", 5) == pytest.approx(5.0)


def test_bollinger_and_volatility_match_independent_arithmetic():
    rows = _series(150)
    _, _, _, closes, _ = _floats(rows)
    window = closes[80:100]
    mean = sum(window) / 20
    deviation = math.sqrt(sum((item - mean) ** 2 for item in window) / 20)
    assert _at(rows, "bb_upper_20", 99) == pytest.approx(mean + 2 * deviation, abs=1e-5)
    assert _at(rows, "bb_lower_20", 99) == pytest.approx(mean - 2 * deviation, abs=1e-5)
    returns = [closes[i] / closes[i - 1] - 1 for i in range(80, 100)]
    average = sum(returns) / 20
    assert _at(rows, "volatility_20", 99) == pytest.approx(
        math.sqrt(sum((item - average) ** 2 for item in returns) / 19), abs=1e-6,
    )


def test_atr_and_adx_match_independent_wilder_arithmetic():
    rows = _series(120)
    _, highs, lows, closes, _ = _floats(rows)
    count = len(rows)
    tr = [0.0] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, count)
    ]
    plus = [0.0] * count
    minus = [0.0] * count
    for i in range(1, count):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus[i] = up if up > down and up > 0 else 0.0
        minus[i] = down if down > up and down > 0 else 0.0
    atr = sum(tr[1:15]) / 14
    plus_avg = sum(plus[1:15]) / 14
    minus_avg = sum(minus[1:15]) / 14
    dx = []
    adx = None
    for i in range(14, count):
        if i > 14:
            atr = (atr * 13 + tr[i]) / 14
            plus_avg = (plus_avg * 13 + plus[i]) / 14
            minus_avg = (minus_avg * 13 + minus[i]) / 14
        p, m = 100 * plus_avg / atr, 100 * minus_avg / atr
        dx.append(100 * abs(p - m) / (p + m))
        if len(dx) == 14:
            adx = sum(dx) / 14
        elif len(dx) > 14:
            adx = (adx * 13 + dx[-1]) / 14
        assert _at(rows, "atr_14", i) == pytest.approx(atr, abs=1e-5)
        assert _at(rows, "plus_di_14", i) == pytest.approx(p, abs=1e-5)
        assert _at(rows, "minus_di_14", i) == pytest.approx(m, abs=1e-5)
        if adx is not None:
            assert _at(rows, "adx_14", i) == pytest.approx(adx, abs=1e-5)


def test_undefined_ratios_stay_none_and_are_never_zero_filled():
    rows = [_row(day, Decimal(100), spread=Decimal(0), gap=Decimal(0), volume=0) for day in _sessions(30)]
    last = compute_symbol_features(rows, _axis(30))[-1].values
    assert last["close_location"] is None
    assert last["volume_ratio_5"] is None
    assert last["bb_percent_b_20"] is None
    assert last["atr_14"] == "0.000000"


# --- missing candles and corporate actions --------------------------------------------


def test_missing_session_restarts_warm_up_and_nothing_bridges_the_gap():
    rows = _series(60)
    gapped = rows[:30] + rows[31:]
    result = compute_symbol_features(gapped, _axis(60))
    after = result[30]
    assert after.as_of_date == rows[31].trading_date
    assert (after.run_index, after.run_observation) == (1, 1)
    assert after.values["gap_open"] is None and after.values["sma_5"] is None
    assert result[34].values["sma_5"] is not None
    expected = sum(Decimal(item.close) for item in rows[31:36]) / 5
    assert Decimal(result[34].values["sma_5"]) == expected.quantize(Decimal("0.000001"))


def test_no_row_is_ever_invented_for_a_missing_session():
    rows = _series(60)
    gapped = rows[:30] + rows[33:]
    days = [item.as_of_date for item in compute_symbol_features(gapped, _axis(60))]
    assert days == [item.trading_date for item in gapped]


def test_corporate_action_restarts_the_series_and_prices_stay_unadjusted():
    rows = _series(60)
    effective = rows[30].trading_date
    result = compute_symbol_features(rows, _axis(60), (effective,))
    assert (result[30].run_index, result[30].run_observation) == (1, 1)
    assert result[30].values["momentum_1"] is None
    assert result[29].values == compute_symbol_features(rows, _axis(60))[29].values


def test_action_marked_not_applicable_does_not_break_the_series():
    catalog = CorporateActionCatalog(
        symbol="ALPHA-EQ", coverage_status="COMPLETE", coverage_start="2020-01-01",
        coverage_end="2020-12-31", provenance=PROVENANCE,
        actions=(
            CorporateAction("ALPHA-EQ", "SPLIT", "2020-03-01", PROVENANCE, "UNKNOWN"),
            CorporateAction("ALPHA-EQ", "NOTICE", "2020-04-01", PROVENANCE, "NOT_APPLICABLE"),
        ),
    )
    evidence = ReferenceEvidence(catalogs={"ALPHA-EQ": catalog})
    assert evidence.discontinuities("ALPHA-EQ") == ("2020-03-01",)


@pytest.mark.parametrize("mutate, message", [
    (lambda rows: list(reversed(rows)), "ascending"),
    (lambda rows: rows + [rows[-1]], "ascending"),
    (lambda rows: rows + [replace(rows[-1], symbol="OTHER-EQ", trading_date="2020-01-20")], "one symbol"),
])
def test_feature_input_is_rejected_when_malformed(mutate, message):
    with pytest.raises(ValueError, match=message):
        compute_symbol_features(mutate(_series(5)), _axis(30))


def test_candle_on_a_date_the_axis_lacks_is_rejected_not_absorbed():
    with pytest.raises(ValueError, match="not a session"):
        compute_symbol_features(_series(5), SessionAxis(_sessions(5)[1:], "EMPIRICAL_UNION"))


# --- label contracts --------------------------------------------------------------------


def test_every_requested_horizon_is_defined_and_daily_buckets_do_not_overlap():
    assert list(CONTRACTS_BY_NAME) == ["intraday", "1-7d", "7-15d", "15-30d", "1-3m", "3-6m", "6-12m"]
    assert [(item.min_offset, item.max_offset) for item in DAILY_LABEL_CONTRACTS] == [
        (1, 7), (8, 15), (16, 30), (31, 63), (64, 126), (127, 252),
    ]
    for item in DAILY_LABEL_CONTRACTS:
        assert item.unit == "NSE_SESSION" and item.as_of_policy.startswith("AFTER_SESSION_CLOSE")
        assert "PLUS_1" in item.entry_policy and item.cost_policy == "GROSS_NOT_COST_ADJUSTED"
    assert INTRADAY_LABEL_CONTRACT.unit == "MINUTE"


def test_label_row_has_no_confidence_or_prediction_field():
    assert not {"confidence", "probability", "prediction", "signal"} & set(LabelRow.__dataclass_fields__)


def test_label_values_and_timestamps_match_hand_calculation():
    rows = [_row(day, Decimal(100 + index)) for index, day in enumerate(_sessions(12))]
    label = build_daily_labels(rows, _axis(12), SHORT)[0]
    days = _sessions(12)
    assert (label.as_of_date, label.entry_date, label.exit_date) == (days[0], days[1], days[7])
    assert label.label_available_date == days[7] and label.status == "COMPLETE"
    entry = Decimal("100.50")
    assert Decimal(label.gross_return_at_min_offset) == (Decimal(101) / entry - 1).quantize(Decimal("0.000001"))
    assert Decimal(label.gross_return_at_max_offset) == (Decimal(107) / entry - 1).quantize(Decimal("0.000001"))
    assert Decimal(label.max_favorable_excursion) == (Decimal(109) / entry - 1).quantize(Decimal("0.000001"))
    assert Decimal(label.max_adverse_excursion) == (Decimal("98.50") / entry - 1).quantize(Decimal("0.000001"))


def test_label_excursions_match_a_brute_force_window():
    rows = _series(90)
    contract = CONTRACTS_BY_NAME["7-15d"]
    for index, label in enumerate(build_daily_labels(rows, _axis(90), contract)):
        if label.status != "COMPLETE":
            continue
        window = rows[index + 1 : index + 16]
        entry = Decimal(rows[index + 1].open)
        assert Decimal(label.max_favorable_excursion) == (
            max(Decimal(item.high) for item in window) / entry - 1
        ).quantize(Decimal("0.000001"))
        assert Decimal(label.max_adverse_excursion) == (
            min(Decimal(item.low) for item in window) / entry - 1
        ).quantize(Decimal("0.000001"))


def test_label_does_not_read_the_as_of_session_or_past_the_exit():
    rows = _series(30)
    base = build_daily_labels(rows, _axis(30), SHORT)[5]
    changed = list(rows)
    for index in (*range(0, 6), *range(13, 30)):
        changed[index] = _poison(rows[index])
    assert build_daily_labels(changed, _axis(30), SHORT)[5] == base


def test_label_changes_when_its_own_window_changes_while_features_do_not():
    rows = _series(40)
    changed = list(rows)
    changed[8] = _poison(rows[8])
    assert build_daily_labels(rows, _axis(40), SHORT)[5] != build_daily_labels(changed, _axis(40), SHORT)[5]
    left = compute_symbol_features(rows, _axis(40))[5]
    assert left == compute_symbol_features(changed, _axis(40))[5]


def test_recent_rows_are_censored_never_zero_or_truncated():
    labels = build_daily_labels(_series(20), _axis(20), SHORT)
    assert [item.status for item in labels[-7:]] == ["CENSORED"] * 7
    assert labels[-8].status == "COMPLETE"
    for item in labels[-7:]:
        assert item.gross_return_at_max_offset is None
        assert item.exit_date is None and item.label_available_date is None


def test_exit_offset_counts_sessions_on_the_axis_not_remaining_rows():
    rows = _series(30)
    gapped = rows[:10] + rows[11:]
    labels = {item.as_of_date: item for item in build_daily_labels(gapped, _axis(30), SHORT)}
    days = _sessions(30)
    for index in (3, 4, 5, 6, 7, 8, 9):
        label = labels[days[index]]
        assert label.status == "MISSING_CANDLE_IN_HORIZON"
        assert label.exit_date == days[index + 7]
        assert label.gross_return_at_max_offset is None
    assert labels[days[2]].status == "COMPLETE"
    assert labels[days[11]].status == "COMPLETE"


def test_symbol_ending_before_the_axis_is_not_reported_as_censored():
    rows = _series(30)[:15]
    labels = build_daily_labels(rows, _axis(30), SHORT)
    assert labels[-1].status == "MISSING_CANDLE_IN_HORIZON"


@pytest.mark.parametrize("offset, expected", [
    (0, "COMPLETE"), (1, "COMPLETE"), (2, "CORPORATE_ACTION_IN_HORIZON"),
    (7, "CORPORATE_ACTION_IN_HORIZON"), (8, "COMPLETE"),
])
def test_corporate_action_boundaries_within_the_horizon(offset, expected):
    rows = _series(30)
    catalog = CorporateActionCatalog(
        symbol="ALPHA-EQ", coverage_status="COMPLETE", coverage_start=rows[0].trading_date,
        coverage_end=rows[-1].trading_date, provenance=PROVENANCE,
        actions=(CorporateAction("ALPHA-EQ", "SPLIT", rows[5 + offset].trading_date, PROVENANCE),),
    )
    label = build_daily_labels(rows, _axis(30), SHORT, ReferenceEvidence(catalogs={"ALPHA-EQ": catalog}))[5]
    assert label.status == expected
    assert label.corporate_action_coverage == "COMPLETE"
    assert (label.gross_return_at_max_offset is None) == (expected != "COMPLETE")


def _instrument(**fields) -> Instrument:
    values = {"nse_symbol": "ALPHA-EQ", "listing_date": "2019-01-01", "membership_verified_through": "2021-01-01"}
    values.update(fields)
    provenance = {
        name: PROVENANCE for name, value in values.items()
        if value is not None and name != "aliases"
    }
    return Instrument(field_provenance=provenance, **values)


def _inventory(*instruments: Instrument) -> InstrumentInventory:
    return InstrumentInventory("2026-01-01", instruments)


def test_delisting_inside_the_horizon_stays_visible_for_survivorship():
    rows = _series(30)
    inventory = _inventory(_instrument(delisting_date=rows[10].trading_date))
    labels = build_daily_labels(rows, _axis(30), SHORT, ReferenceEvidence(inventory))
    assert labels[5].status == "DELISTED_IN_HORIZON"
    assert labels[5].gross_return_at_max_offset is None
    assert labels[2].status == "COMPLETE"


def test_without_evidence_coverage_and_membership_are_unknown_not_assumed():
    label = build_daily_labels(_series(20), _axis(20), SHORT)[0]
    assert label.corporate_action_coverage == "UNKNOWN"
    assert label.membership_at_as_of == "UNKNOWN"


def test_membership_uses_supplied_dates_and_aliases_only():
    alias = HistoricalAlias("OLDNAME-EQ", None, "2020-06-30", PROVENANCE)
    evidence = ReferenceEvidence(_inventory(_instrument(aliases=(alias,))))
    assert evidence.membership("ALPHA-EQ", "2018-01-01") == "PRE_LISTING"
    assert evidence.membership("ALPHA-EQ", "2020-08-01") == "LISTED"
    assert evidence.membership("ALPHA-EQ", "2020-03-01") == "ALIAS_MISMATCH"
    assert evidence.membership("OLDNAME-EQ", "2020-03-01") == "LISTED"
    assert evidence.membership("ALPHA-EQ", "2022-01-01") == "UNKNOWN"
    assert evidence.membership("MISSING-EQ", "2020-03-01") == "UNKNOWN"
    assert ReferenceEvidence().membership("ALPHA-EQ", "2020-03-01") == "UNKNOWN"
    assert evidence.alias_history_complete() is False


# --- calendar evidence ----------------------------------------------------------------------


def test_authoritative_axis_requires_complete_explicit_calendar():
    days = tuple(
        CalendarDay(f"2026-01-0{day}", "TRADING_SESSION" if day != 3 else "MARKET_CLOSED", PROVENANCE)
        for day in range(1, 6)
    )
    complete = NSETradingCalendar(days, "2026-01-01", "2026-01-05", True)
    axis = SessionAxis.from_calendar(complete)
    assert axis.source == "AUTHORITATIVE_CALENDAR"
    assert axis.sessions == ("2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05")
    with pytest.raises(ValueError, match="complete calendar"):
        SessionAxis.from_calendar(NSETradingCalendar(days))
    unknown = (*days[:2], CalendarDay("2026-01-03", "UNKNOWN", PROVENANCE), *days[3:])
    with pytest.raises(ValueError, match="UNKNOWN"):
        SessionAxis.from_calendar(NSETradingCalendar(unknown, "2026-01-01", "2026-01-05", True))


# --- intraday requires real minute data --------------------------------------------------------


def _minute_bars(count: int, *, day: date = date(2026, 1, 5), skip: frozenset[int] = frozenset()) -> tuple[MinuteBar, ...]:
    start = datetime(day.year, day.month, day.day, 3, 45, tzinfo=timezone.utc)
    bars = []
    for index in range(count):
        if index in skip:
            continue
        price = Decimal(100 + index)
        bars.append(MinuteBar(start + timedelta(minutes=index), price, price + 1, price - 1, price, 10))
    return tuple(bars)


def _bounds(bars, count=None, *, day="2026-01-05"):
    first = datetime.fromisoformat(f"{day}T03:45:00+00:00")
    return {day: SessionBounds(first, first + timedelta(minutes=(count or len(bars)) - 1), "fixture")}


def test_no_minute_schema_is_confirmed_so_production_refuses():
    assert CONFIRMED_MINUTE_SCHEMAS == frozenset()
    bars = _minute_bars(5)
    for schema in ("ShoonyaMinuteV0", SYNTHETIC_FIXTURE_SCHEMA):
        with pytest.raises(IntradayDataRequired, match="not confirmed"):
            build_intraday_labels(MinuteSeries("ALPHA-EQ", schema, bars), _bounds(bars))


def test_daily_contract_builder_refuses_the_intraday_horizon():
    with pytest.raises(IntradayDataRequired, match="never derived from daily"):
        build_daily_labels(_series(20), _axis(20), INTRADAY_LABEL_CONTRACT)


def test_daily_candles_cannot_pose_as_minute_bars():
    daily = tuple(
        MinuteBar(datetime(2026, 1, 1 + index, tzinfo=timezone.utc), Decimal(100), Decimal(101), Decimal(99), Decimal(100), 10)
        for index in range(5)
    )
    with pytest.raises(IntradayDataRequired, match="too few bars"):
        MinuteSeries("ALPHA-EQ", SYNTHETIC_FIXTURE_SCHEMA, daily)
    with pytest.raises(IntradayDataRequired, match="MinuteSeries"):
        build_intraday_labels(_series(5), {}, synthetic_fixture=True)


def test_synthetic_path_requires_the_fixture_schema_and_labels_its_output():
    bars = _minute_bars(5)
    with pytest.raises(IntradayDataRequired, match="labeled test fixture"):
        build_intraday_labels(MinuteSeries("ALPHA-EQ", "ShoonyaMinuteV0", bars), _bounds(bars), synthetic_fixture=True)
    rows = build_intraday_labels(
        MinuteSeries("ALPHA-EQ", SYNTHETIC_FIXTURE_SCHEMA, bars), _bounds(bars), synthetic_fixture=True,
    )
    assert {item.evidence for item in rows} == {"SYNTHETIC_TEST_FIXTURE"}


@pytest.mark.parametrize("fields, message", [
    ({"timestamp_utc": datetime(2026, 1, 5, 3, 45)}, "UTC"),
    ({"timestamp_utc": datetime(2026, 1, 5, 3, 45, 30, tzinfo=timezone.utc)}, "whole minute"),
    ({"high": Decimal(90)}, "OHLC"),
])
def test_invalid_minute_bars_are_rejected(fields, message):
    values = {
        "timestamp_utc": datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc),
        "open": Decimal(100), "high": Decimal(101), "low": Decimal(99), "close": Decimal(100), "volume": 1,
    }
    values.update(fields)
    with pytest.raises(ValueError, match=message):
        MinuteBar(**values)


def _intraday(bars, bounds):
    return build_intraday_labels(MinuteSeries("ALPHA-EQ", SYNTHETIC_FIXTURE_SCHEMA, bars), bounds, synthetic_fixture=True)


def test_intraday_values_enter_on_the_next_bar_and_read_no_future_session():
    bars = _minute_bars(40)
    first = _intraday(bars, _bounds(bars))[0]
    assert first.status == "COMPLETE"
    assert Decimal(first.gross_returns["15m"]) == (Decimal(115) / Decimal(101) - 1).quantize(Decimal("0.000001"))
    assert Decimal(first.gross_return_to_session_close) == (Decimal(139) / Decimal(101) - 1).quantize(Decimal("0.000001"))
    assert first.gross_returns["60m"] is None
    next_day = _minute_bars(40, day=date(2026, 1, 6))
    bounds = {**_bounds(bars), **_bounds(next_day, day="2026-01-06")}
    both = _intraday(bars + next_day, bounds)
    assert both[:40] == _intraday(bars, _bounds(bars))
    assert both[39].status == "NO_ENTRY_IN_SESSION" and both[39].gross_return_to_session_close is None


def test_missing_minute_blocks_the_path_without_interpolation():
    bars = _minute_bars(40, skip=frozenset({10}))
    rows = _intraday(bars, _bounds(bars, 40))
    assert rows[0].status == "MISSING_MINUTE_IN_HORIZON"
    assert rows[0].gross_returns["15m"] is None and rows[0].gross_return_to_session_close is None
    assert rows[9].status == "MISSING_ENTRY_BAR"
    assert len(rows) == 39


def test_session_bounds_are_required_not_assumed():
    bars = _minute_bars(5)
    assert {item.status for item in _intraday(bars, {})} == {"SESSION_BOUNDS_UNKNOWN"}


# --- splits, purge and embargo ------------------------------------------------------------------


@pytest.mark.parametrize("contract", DAILY_LABEL_CONTRACTS, ids=lambda item: item.name)
def test_default_plans_are_chronological_and_purged(contract):
    axis = _axis(3000)
    plan = build_walk_forward(axis, contract)
    assert plan.folds and plan.locked_holdout is not None
    verify_plan(plan, axis)
    separation = contract.max_offset + plan.policy.embargo_sessions + 1
    for fold in plan.folds:
        train, validation, test = fold.train, fold.validation, fold.test
        assert train.first_as_of <= train.last_as_of < validation.first_as_of
        assert validation.last_as_of < test.first_as_of <= test.last_as_of
        assert axis.position(validation.first_as_of) - axis.position(train.last_as_of) == separation
        assert axis.position(test.first_as_of) - axis.position(validation.last_as_of) == separation
        assert axis.position(plan.locked_holdout.first_as_of) - axis.position(test.last_as_of) >= separation
        assert axis.position(train.last_as_of) - axis.position(train.first_as_of) + 1 >= plan.policy.minimum_train_sessions
    tests = [(item.test.first_as_of, item.test.last_as_of) for item in plan.folds]
    assert all(left[1] < right[0] for left, right in zip(tests, tests[1:]))


def test_longer_horizons_get_a_longer_purge():
    purges = [build_walk_forward(_axis(3000), item).purge_sessions for item in DAILY_LABEL_CONTRACTS]
    assert purges == sorted(purges) and purges[0] == 7 and purges[-1] == 252


def test_insufficient_history_yields_no_folds_rather_than_shrunken_ones():
    contract = CONTRACTS_BY_NAME["6-12m"]
    plan = build_walk_forward(_axis(700), contract)
    assert plan.folds == ()
    assert build_walk_forward(_axis(100), SHORT).locked_holdout is None or not build_walk_forward(_axis(100), SHORT).folds


def test_rolling_train_moves_its_start_and_expanding_does_not():
    base = default_policy(SHORT)
    expanding = build_walk_forward(_axis(2000), SHORT, base)
    rolling = build_walk_forward(_axis(2000), SHORT, replace(base, expanding_train=False))
    assert len({item.train.first_as_of for item in expanding.folds}) == 1
    assert len({item.train.first_as_of for item in rolling.folds}) == len(rolling.folds) > 1
    assert [item.test for item in expanding.folds] == [item.test for item in rolling.folds]


def test_holdout_never_appears_in_any_fold():
    plan = build_walk_forward(_axis(2000), SHORT)
    for fold in plan.folds:
        assert fold.test.last_as_of < plan.locked_holdout.first_as_of
        assert fold.validation.last_as_of < plan.locked_holdout.first_as_of


@pytest.mark.parametrize("change", ["train", "test", "holdout"])
def test_verification_catches_a_tampered_boundary(change):
    axis = _axis(2000)
    plan = build_walk_forward(axis, SHORT)
    fold = plan.folds[0]
    if change == "train":
        tampered = replace(plan, folds=(replace(fold, train=replace(
            fold.train, last_as_of=axis.sessions[axis.position(fold.validation.first_as_of) - 1],
        )),))
    elif change == "test":
        tampered = replace(plan, folds=(replace(fold, test=replace(
            fold.test, first_as_of=axis.sessions[axis.position(fold.validation.last_as_of) + 1],
        )),))
    else:
        tampered = replace(plan, locked_holdout=Partition(fold.test.last_as_of, axis.sessions[-1]))
    with pytest.raises(LeakageError):
        verify_plan(tampered, axis)


def test_verification_requires_a_holdout_when_folds_exist():
    axis = _axis(2000)
    with pytest.raises(LeakageError, match="locked holdout"):
        verify_plan(replace(build_walk_forward(axis, SHORT), locked_holdout=None), axis)


def test_real_label_rows_satisfy_each_plan_and_a_resolving_label_is_refused():
    axis = _axis(1400)
    rows = _series(1400)
    labels = build_daily_labels(rows, axis, SHORT)
    plan = build_walk_forward(axis, SHORT)
    fold = plan.folds[0]
    embargo = plan.policy.embargo_sessions
    train = select_samples(labels, fold.train, next_partition_first=fold.validation.first_as_of, axis=axis, embargo_sessions=embargo)
    assert train and all(item.label_available_date < fold.validation.first_as_of for item in train)
    validation = select_samples(labels, fold.validation, next_partition_first=fold.test.first_as_of, axis=axis, embargo_sessions=embargo)
    assert validation and all(item.label_available_date < fold.test.first_as_of for item in validation)
    late = Partition(fold.train.first_as_of, axis.sessions[axis.position(fold.train.last_as_of) + 1])
    with pytest.raises(LeakageError, match="resolves inside"):
        select_samples(labels, late, next_partition_first=fold.validation.first_as_of, axis=axis, embargo_sessions=embargo)


def test_select_samples_keeps_only_complete_labels():
    axis = _axis(40)
    labels = build_daily_labels(_series(40), axis, SHORT)
    selected = select_samples(labels, Partition(axis.sessions[0], axis.sessions[-1]), next_partition_first=None, axis=axis, embargo_sessions=0)
    assert len(selected) == 33 and {item.status for item in selected} == {"COMPLETE"}


@pytest.mark.parametrize("fields", [
    {"minimum_train_sessions": 0}, {"embargo_sessions": -1}, {"trial_budget": 0},
    {"test_sessions": 50, "step_sessions": 10},
])
def test_invalid_policies_are_rejected(fields):
    with pytest.raises(ValueError):
        replace(default_policy(SHORT), **fields)


def test_intraday_cannot_use_a_daily_walk_forward_axis():
    with pytest.raises(ValueError, match="daily contract"):
        build_walk_forward(_axis(2000), INTRADAY_LABEL_CONTRACT)


# --- integration with M3.1 -----------------------------------------------------------------------


def _readiness(tmp_path: Path, count: int = 60) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    raw.mkdir()
    _publish(raw, "RELIANCE-EQ", count)
    return raw, build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "m31",
    )


def test_run_is_deterministic_read_only_and_blocks_without_evidence(tmp_path):
    raw, m31 = _readiness(tmp_path)
    before = _hashes(raw)
    run = build_research_run(readiness_run=m31, output_root=tmp_path / "one")
    again = build_research_run(readiness_run=m31, output_root=tmp_path / "two")
    assert _hashes(raw) == before
    assert run.name == again.name
    assert _hashes(run) == _hashes(again)
    gates = json.loads((run / "quality-gates.json").read_text())
    assert gates["training_readiness"] == "NOT_READY" and gates["model_training"] == "NOT_PERFORMED"
    assert gates["session_source"] == "EMPIRICAL_UNION"
    for name in ("authoritative_calendar", "point_in_time_membership", "alias_history", "corporate_action_coverage",
                 "instrument_classification", "minute_data"):
        assert gates["gates"][name]["status"] == "BLOCKED", name
    assert gates["gates"]["raw_candle_acceptance"]["status"] == "PASS"
    assert {item["decision"] for item in gates["horizons"].values()} == {"BLOCKED"}
    assert gates["horizons"]["intraday"]["label_status_counts"] == {}
    assert set(gates["horizons"]) == set(CONTRACTS_BY_NAME)
    with pytest.raises(FileExistsError):
        build_research_run(readiness_run=m31, output_root=tmp_path / "one")
    assert not [path for path in (tmp_path / "one").iterdir() if ".partial-" in path.name]


def test_run_outputs_match_manifest_and_column_contracts(tmp_path):
    _, m31 = _readiness(tmp_path)
    run = build_research_run(readiness_run=m31, output_root=tmp_path / "out")
    manifest = json.loads((run / "run-manifest.json").read_text())
    for name, digest in manifest["outputs"].items():
        assert hashlib.sha256((run / name).read_bytes()).hexdigest() == digest
    with (run / "features.csv").open(newline="") as stream:
        features = list(csv.DictReader(stream))
    with (run / "labels.csv").open(newline="") as stream:
        labels = list(csv.DictReader(stream))
    assert len(features) == 60
    assert set(FEATURE_NAMES) <= set(features[0])
    assert not {"gross_return_at_max_offset", "exit_date", "status"} & set(features[0])
    assert len(labels) == 60 * len(DAILY_LABEL_CONTRACTS)
    assert features[0]["sma_5"] == "" and features[4]["sma_5"] != ""
    for row in labels:
        if row["status"] != "COMPLETE":
            assert row["gross_return_at_max_offset"] == "" and row["label_available_date"] == ""
    text = (run / "contracts.json").read_text()
    assert "stock-gplm.m32-features.v1" in text and "stock-gplm.m32-labels.v1" in text
    assert (run / "split-plan.json").exists()


@pytest.mark.parametrize("target", ["research-dataset.csv", "observed-sessions.csv", "inventory.json"])
def test_tampered_m31_output_is_refused(tmp_path, target):
    _, m31 = _readiness(tmp_path)
    path = m31 / target
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="does not match its manifest"):
        build_research_run(readiness_run=m31, output_root=tmp_path / "out")


def test_non_readiness_input_and_missing_contract_outputs_are_refused(tmp_path):
    _, m31 = _readiness(tmp_path)
    manifest_path = m31 / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["outputs"]["observed-sessions.csv"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="predates"):
        load_readiness_run(m31)
    manifest["contract"] = "something-else"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="not an M3.1"):
        load_readiness_run(m31)


def test_output_inside_the_raw_tree_is_refused(tmp_path):
    raw, m31 = _readiness(tmp_path)
    with pytest.raises(ValueError, match="outside the immutable raw"):
        build_research_run(readiness_run=m31, output_root=raw / "reports")
    assert not (raw / "reports").exists()


def test_reference_evidence_changes_the_run_identity(tmp_path):
    _, m31 = _readiness(tmp_path)
    plain = build_research_run(readiness_run=m31, output_root=tmp_path / "a")
    bound = build_research_run(
        readiness_run=m31, output_root=tmp_path / "b", evidence_hashes={"calendar": "f" * 64},
    )
    assert plain.name != bound.name


def test_supplied_evidence_can_pass_reference_gates_but_never_training(tmp_path):
    _, m31 = _readiness(tmp_path, 60)
    catalog = CorporateActionCatalog(
        symbol="RELIANCE-EQ", coverage_status="COMPLETE", coverage_start="2020-01-01",
        coverage_end="2030-01-01", provenance=PROVENANCE,
    )
    instrument = _instrument(
        nse_symbol="RELIANCE-EQ", listing_date="2000-01-01", membership_verified_through="2030-01-01",
        alias_history_status="COMPLETE",
    )
    start = date(2026, 1, 1)
    days = tuple(
        CalendarDay((start + timedelta(days=i)).isoformat(), "TRADING_SESSION", PROVENANCE) for i in range(60)
    )
    calendar = NSETradingCalendar(days, days[0].date, days[-1].date, True)
    run = build_research_run(
        readiness_run=m31, output_root=tmp_path / "out",
        evidence=ReferenceEvidence(_inventory(instrument), {"RELIANCE-EQ": catalog}),
        axis=SessionAxis.from_calendar(calendar), instrument_classes={"RELIANCE-EQ": "EQUITY"},
    )
    gates = json.loads((run / "quality-gates.json").read_text())
    outputs = json.loads((run / "run-manifest.json").read_text())["outputs"]
    # The calendar axis and classes are bound so M3.5 rebuilds plans on the same sessions.
    assert {"session-axis.csv", "instrument-classes.csv"} <= set(outputs)
    assert (run / "session-axis.csv").read_text().splitlines()[1:] == [d.date for d in days]
    assert gates["gates"]["instrument_classification"]["status"] == "PASS"
    assert gates["gates"]["authoritative_calendar"]["status"] == "PASS"
    assert gates["gates"]["point_in_time_membership"]["status"] == "PASS"
    assert gates["gates"]["corporate_action_coverage"]["status"] == "PASS"
    assert gates["gates"]["alias_history"]["status"] == "PASS"
    assert gates["reference_evidence_complete"] is True
    assert gates["gates"]["minute_data"]["status"] == "BLOCKED"
    assert gates["training_readiness"] == "NOT_READY"
    assert gates["horizons"]["1-7d"]["blockers"] == ["INSUFFICIENT_HISTORY_FOR_WALK_FORWARD"]


def test_cli_builds_a_run_and_requires_paired_manifests(tmp_path, monkeypatch, capsys):
    from stock_gplm.m3 import research_cli

    _, m31 = _readiness(tmp_path)
    monkeypatch.setattr("sys.argv", ["research", "--readiness-run", str(m31), "--output-root", str(tmp_path / "out")])
    assert research_cli.main() == 0
    assert "NOT_READY" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", [
        "research", "--readiness-run", str(m31), "--output-root", str(tmp_path / "out2"),
        "--calendar", str(tmp_path / "x"),
    ])
    with pytest.raises(SystemExit):
        research_cli.main()
    tampered = m31 / "research-dataset.csv"
    tampered.write_bytes(b"tampered")
    monkeypatch.setattr("sys.argv", ["research", "--readiness-run", str(m31), "--output-root", str(tmp_path / "out3")])
    assert research_cli.main() == 3
