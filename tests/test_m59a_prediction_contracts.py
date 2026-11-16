"""M5.9A regression tests: weekly completeness, exact target-horizon contract, neutral band,
actionable-output schema and the fail-closed LIVE/approval gate. Synthetic calendars and candles only."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from stock_gplm.app import contracts as C
from stock_gplm.app import m58_prediction as mp
from stock_gplm.app.db import AuditDB
from stock_gplm.app.ingest import SymbolData
from stock_gplm.app.session_calendar import SessionCalendar, expected_completed_session
from stock_gplm.app.targets import check_explicit_target, derive_target, label_ns
from stock_gplm.m54.timeframes import daily_bars, weekly_bars
from stock_gplm.m54.candles import ist_ns
from stock_gplm.verify import fixtures as fx

# 2026-03-02 is a Monday. Week 2 (03-09..03-15) has a Friday holiday (03-13).
MON = date(2026, 3, 2)
HOLIDAY = "2026-03-13"


def _calendar(days: int = 35, holidays: tuple[str, ...] = (HOLIDAY,), unknown: tuple[str, ...] = ()) -> SessionCalendar:
    statuses = {}
    for i in range(days):
        d = MON + timedelta(days=i)
        k = d.isoformat()
        if k in unknown:
            continue
        statuses[k] = "MARKET_CLOSED" if d.weekday() >= 5 or k in holidays else "TRADING_SESSION"
    return SessionCalendar.from_statuses(statuses, source="synthetic-test-calendar", as_of_date="2026-03-01")


def _daily(sessions: list[str]) -> pd.DataFrame:
    n = len(sessions)
    return pd.DataFrame({"date": sessions, "open": [100.0 + i for i in range(n)], "high": [101.0 + i for i in range(n)],
                         "low": [99.0 + i for i in range(n)], "close": [100.5 + i for i in range(n)],
                         "volume": [1000.0] * n})


def _weekdays(start: date, n_days: int, skip: tuple[str, ...] = (HOLIDAY,)) -> list[str]:
    out = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        if d.weekday() < 5 and d.isoformat() not in skip:
            out.append(d.isoformat())
    return out


# ----------------------------------------------------------------------------- A. weekly completeness
def test_weekly_ordinary_complete_partial_holiday_and_unknown() -> None:
    cal = _calendar()
    # weeks 1-2 complete (week 2 holiday-shortened), week 3 open (Mon-Wed only)
    sessions = _weekdays(MON, 14) + ["2026-03-16", "2026-03-17", "2026-03-18"]
    w = weekly_bars(daily_bars(_daily(sessions)), cal).frame
    assert list(w["week_status"]) == ["COMPLETE", "COMPLETE", "PARTIAL"]
    assert list(w["incomplete"]) == [False, False, True]
    assert int(w["avail_ns"].iloc[0]) == ist_ns("2026-03-06", "15:30")      # ordinary week: Friday close
    assert int(w["avail_ns"].iloc[1]) == ist_ns("2026-03-12", "15:30")      # holiday week: Thursday close
    assert w["session"].iloc[1] == "2026-03-12"
    assert int(w["avail_ns"].iloc[2]) == ist_ns("2026-03-20", "15:30")      # open week not readable before it ends
    # No registered calendar: no week is ever final (Friday is never assumed).
    nocal = weekly_bars(daily_bars(_daily(sessions))).frame
    assert set(nocal["week_status"]) == {"CALENDAR_UNKNOWN"} and nocal["incomplete"].all()
    # A day the calendar does not list -> CALENDAR_UNKNOWN; a missing mid-week session -> MISSING_EXPECTED_BAR.
    unk = weekly_bars(daily_bars(_daily(sessions)), _calendar(unknown=("2026-03-04",))).frame
    assert unk["week_status"].iloc[0] == "CALENDAR_UNKNOWN" and bool(unk["incomplete"].iloc[0])
    gap = [s for s in sessions if s != "2026-03-04"]
    miss = weekly_bars(daily_bars(_daily(gap)), cal).frame
    assert miss["week_status"].iloc[0] == "MISSING_EXPECTED_BAR"


def test_weekly_decision_and_target_align_across_holiday_week() -> None:
    cal = _calendar()
    # decision = week 1 (ends Fri 03-06); h=1 -> the holiday-shortened week ending Thu 03-12; h=2 -> Fri 03-20
    d1 = derive_target("1w", "2026-03-06", ist_ns("2026-03-06", "15:30"), 1, calendar=cal)
    d2 = derive_target("1w", "2026-03-06", ist_ns("2026-03-06", "15:30"), 2, calendar=cal)
    assert label_ns(d1["target"]) == ist_ns("2026-03-12", "15:30")
    assert label_ns(d2["target"]) == ist_ns("2026-03-20", "15:30")
    # weekly targets need a registered calendar
    assert derive_target("1w", "2026-03-06", ist_ns("2026-03-06", "15:30"), 1)["reason"] == C.CALENDAR_UNKNOWN
    # A holiday-week bar is decision-eligible at its real last session (Thursday), not on the holiday.
    sessions = _weekdays(MON, 14)
    w = weekly_bars(daily_bars(_daily(sessions)), cal).frame
    thu_close = ist_ns("2026-03-12", "15:30")
    eligible = w[(w["avail_ns"] <= thu_close) & ~w["incomplete"]]
    assert eligible["session"].iloc[-1] == "2026-03-12"


def _fake_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: SymbolData, calendar_section: dict | None = None):
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data

    class S:
        cache_dir = tmp_path / "cache"
        reports_dir = tmp_path / "reports"

        def section(self, name: str):
            return {"costs": {p: {"commission_bps": 0, "slippage_bps": 0, "buy_statutory_bps": 0,
                                  "sell_statutory_bps": 0} for p in ("delivery", "intraday")},
                    "m58": {"prediction": {"minimum_calibration_outcomes": 5}},
                    "prediction": {"min_probability": 0.56},
                    "calendar": calendar_section or {}}.get(name, {})

    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    return SimpleNamespace(s=S(), db=AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t"))


def _fixture_data() -> SymbolData:
    f = fx.ohlcv(540, seed=919)
    d = f.rename(columns={"session": "date"}).drop(columns="end_ns")
    d["source"] = "SYNTHETIC"
    return SymbolData("SYN-EQ", d, pd.DataFrame(), "m59a-fixture", {"minute_bars": 0})


def test_weekly_prediction_suppressed_without_registered_calendar(monkeypatch, tmp_path) -> None:
    data = _fixture_data()
    app = _fake_app(monkeypatch, tmp_path, data)
    with pytest.raises(ValueError, match="CALENDAR_UNKNOWN"):
        mp.predict(app, "SYN", str(data.daily["date"].iloc[520]), None, "1w", 1, tmp_path / "f")
    assert app.db.one("SELECT COUNT(*) n FROM predictions")["n"] == 0
    app.db.close()


# ----------------------------------------------------------------------------- B. target-horizon contract
def test_horizon_one_cannot_accept_target_three_bars_later_daily_intraday_weekly() -> None:
    cal = _calendar()
    # daily: Thu 03-12 -> next session skips the Friday holiday
    d = derive_target("1d", "2026-03-12", ist_ns("2026-03-12", "15:30"), 1, calendar=cal)
    assert d["target"] == "2026-03-16" and d["derivation"] == "REGISTERED_CALENDAR"
    with pytest.raises(ValueError, match="TARGET_HORIZON_MISMATCH"):
        check_explicit_target("2026-03-18", d, 1)  # three eligible sessions later
    assert check_explicit_target("2026-03-16", d, 1) == "2026-03-16"
    # intraday: 15m decision ending 10:45 -> h=1 target 11:00 on the session grid
    i = derive_target("15m", "2026-03-12", ist_ns("2026-03-12", "10:45"), 1, calendar=cal)
    assert label_ns(i["target"]) == ist_ns("2026-03-12", "11:00")
    with pytest.raises(ValueError, match="TARGET_HORIZON_MISMATCH"):
        check_explicit_target("2026-03-12T11:30:00+05:30", i, 1)
    # weekly: h=1 cannot accept the week three weeks later
    w = derive_target("1w", "2026-03-06", ist_ns("2026-03-06", "15:30"), 1, calendar=cal)
    with pytest.raises(ValueError, match="TARGET_HORIZON_MISMATCH"):
        check_explicit_target("2026-03-27T15:30:00+05:30", w, 1)


def test_m58_rejects_mismatched_target_before_freezing_and_stores_contract(monkeypatch, tmp_path) -> None:
    data = _fixture_data()
    d = [str(x) for x in data.daily["date"]]
    app = _fake_app(monkeypatch, tmp_path, data)
    with pytest.raises(ValueError, match="TARGET_HORIZON_MISMATCH"):
        mp.predict(app, "SYN", d[499], d[502], "1d", 1, tmp_path / "f")
    assert app.db.one("SELECT COUNT(*) n FROM predictions")["n"] == 0
    frozen = mp.predict(app, "SYN", d[499], None, "1d", 2, tmp_path / "f")
    assert frozen["target_session"] == d[501] and frozen["horizon"] == 2
    row = app.db.one("SELECT * FROM predictions")
    tc = json.loads(row["payload_json"])["target_contract"]
    assert tc == {"decision_bar": d[499], "decision_end_ns": int(row["decision_ns"]), "declared_horizon": 2,
                  "derived_target": d[501], "derivation": "OBSERVED_ELIGIBLE_BARS", "explicit_target_supplied": False}
    assert row["actionable"] == 0 and row["stop"] is None
    # The reveal scores exactly the frozen target; if a bar between disappears the outcome is NOT_COMPARABLE.
    data.daily.drop(index=500, inplace=True)
    data.daily.reset_index(drop=True, inplace=True)
    out = mp.reveal(app, frozen["prediction_id"])
    assert out["status"] == "NOT_COMPARABLE" and out["reason"] == C.TARGET_HORIZON_MISMATCH and out["bars_held"] == 1
    ev = app.db.one("SELECT * FROM evaluations")
    assert ev["correct"] is None and ev["brier"] is None and ev["outcome_label"] == d[501] and ev["bars_held"] == 1
    app.db.close()


def test_m58_reveal_records_reveal_bar_and_bars_held(monkeypatch, tmp_path) -> None:
    data = _fixture_data()
    d = [str(x) for x in data.daily["date"]]
    app = _fake_app(monkeypatch, tmp_path, data)
    frozen = mp.predict(app, "SYN", d[499], d[502], "1d", 3, tmp_path / "f")
    out = mp.reveal(app, frozen["prediction_id"])
    assert out["status"] == "REVEALED" and out["bars_held"] == 3 and out["target_session"] == d[502]
    payload = json.loads(app.db.one("SELECT payload_json FROM evaluations")["payload_json"])
    assert payload["reveal_bar"] == d[502] and payload["declared_horizon"] == 3 and payload["bars_held"] == 3
    app.db.close()


# ----------------------------------------------------------------------------- C. neutral vs uncertainty
@pytest.mark.parametrize("gross,expected", [(0.004 - 1e-9, "NEUTRAL"), (0.004, "NEUTRAL"), (0.004 + 1e-9, "UP"),
                                            (-0.004 + 1e-9, "NEUTRAL"), (-0.004, "NEUTRAL"), (-0.004 - 1e-9, "DOWN")])
def test_neutral_band_boundaries(gross: float, expected: str) -> None:
    band = C.neutral_band(None, 0.004)
    assert band.mode == "cost_aware" and band.threshold == pytest.approx(0.004)
    assert C.classify_movement(gross, band) == expected


def test_neutral_is_not_down_and_uncertain_is_not_neutral() -> None:
    assert C.direction_correct("DOWN", "NEUTRAL") is None and C.direction_correct("UP", "NEUTRAL") is None
    assert C.direction_correct("UNCERTAIN", "DOWN") is None
    assert C.brier_up(0.3, "NEUTRAL") is None and C.brier_up(0.3, "DOWN") == pytest.approx(0.09)
    assert C.ABSTAIN_TOKEN not in C.MOVEMENTS
    assert C.neutral_band({"mode": "fixed", "floor_bps": 10}, 0.004).threshold == pytest.approx(0.001)
    assert C.neutral_band({"floor_bps": 50}, 0.004).threshold == pytest.approx(0.005)
    m = mp._metrics([
        {"direction": "UP", "realized_return": 0.001, "up": None, "correct": None, "prob_calibrated": 0.7,
         "brier": None, "trade_net_return": None},
        {"direction": "DOWN", "realized_return": -0.02, "up": 0, "correct": 1, "prob_calibrated": 0.2,
         "brier": 0.04, "trade_net_return": None}])
    assert m["neutral_outcomes"] == 1 and m["directional_accuracy_denominator"] == 1 and m["brier_denominator"] == 1


# ----------------------------------------------------------------------------- E/F. LIVE gate and actionable schema
def _row(**kw):
    base = {"natural_key": "k", "mode": "LIVE", "symbol": "A", "timeframe": "1d", "lane_key": "A|1d",
            "source_kind": "MODEL", "source_id": "logit", "source_version": "v", "horizon_bars": 1,
            "decision_label": "2026-03-12", "decision_ns": 1, "direction": "LONG", "prob_calibrated": 0.6,
            "actionable": 1, "entry_ref": 100.0, "stop": 98.0, "target": 104.0,
            "payload": {"opportunity": {"entry": "buy next open", "invalidation": "close below 98.0",
                                        "expected_return": 0.01, "model_version": "m|sha",
                                        "freshness_status": "FRESH"}}}
    base.update(kw)
    return base


def test_actionable_rows_require_the_full_opportunity_contract(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    with pytest.raises(ValueError, match="stop"):
        db.add_prediction(_row(natural_key="no-stop", stop=None, target=None))
    with pytest.raises(ValueError, match="invalidation"):
        db.add_prediction(_row(natural_key="no-inv", payload={"opportunity": {"entry": "x", "expected_return": 0.0,
                                                                               "model_version": "m",
                                                                               "freshness_status": "FRESH"}}))
    assert db.add_prediction(_row(natural_key="ok"))
    assert db.add_prediction(_row(natural_key="research", actionable=0, stop=None, target=None, payload={}))
    db.close()


def test_unapproved_adaptive_refit_cannot_become_live(tmp_path: Path) -> None:
    fresh = C.evaluate_freshness("2026-03-12", "2026-03-12", calendar_confirmed=True)
    assert fresh.fresh
    shadow = C.live_gate(latest_eligible_bar=True, freshness=fresh, model_approval=None)
    assert shadow.status == C.SHADOW and C.MODEL_NOT_APPROVED in shadow.reasons and not shadow.actionable
    kind_only = C.live_gate(latest_eligible_bar=True, freshness=fresh, model_approval={"model_id": "logit"})
    assert kind_only.status == C.SHADOW
    approved = {"model_id": "rp|logit|h1|2026-03-12", "artifact_sha256": "a" * 64}
    assert C.live_gate(latest_eligible_bar=True, freshness=fresh, model_approval=approved).status == C.LIVE
    assert C.live_gate(latest_eligible_bar=True, freshness=fresh, model_approval=approved,
                       missing_fields=["stop"]).status == C.NO_TRADE
    stale = C.evaluate_freshness("2026-03-11", "2026-03-12", calendar_confirmed=True)
    assert C.live_gate(latest_eligible_bar=True, freshness=stale, model_approval=approved).status == C.UNAVAILABLE
    # DB approval lookup is fail-closed: kind strings, unknown ids and digest mismatches are never approved.
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert db.model_approval("logit", "a" * 64) is None and db.model_approval(None, None) is None
    db.conn.execute("INSERT INTO models (model_id, lane_key, kind, horizon, created_utc, sha256) VALUES "
                    "('m1','A|1d','logit',1,'t',?)", ("b" * 64,))
    db.conn.commit()
    with pytest.raises(ValueError):
        db.record_model_approval("m1", "c" * 64, "owner", {"note": "x"})
    with pytest.raises(ValueError):
        db.record_model_approval("m1", "b" * 64, "owner", {})
    assert db.record_model_approval("m1", "b" * 64, "owner", {"review": "owner-reviewed OOS report"})
    assert db.model_approval("m1", "b" * 64)["approved_by"] == "owner"
    with pytest.raises(Exception):
        db.conn.execute("DELETE FROM model_approvals")
    db.close()


def test_schema_v1_database_is_migrated_additively(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert db.add_prediction(_row(natural_key="old", actionable=0, payload={}))
    db.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db.conn.execute("DROP TABLE model_approvals")
    db.conn.commit()
    db.close()
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t2")
    assert db.one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "2"
    assert db.one("SELECT COUNT(*) n FROM predictions")["n"] == 1
    assert db.one("SELECT kind FROM events WHERE kind='SCHEMA_MIGRATED'") is not None
    assert db.verify_chain()["ok"]
    db.close()


# ----------------------------------------------------------------------------- calendar-derived expected session
def test_expected_session_requires_calendar_or_explicit_as_of() -> None:
    cal = _calendar()
    assert expected_completed_session(None, None) == (None, False)
    assert expected_completed_session(None, "2026-03-12") == ("2026-03-12", False)
    assert expected_completed_session(cal, "2026-03-13") == ("2026-03-12", True)  # holiday -> previous session
    assert expected_completed_session(cal, "2026-03-12") == ("2026-03-12", True)
    ist = pd.Timestamp("2026-03-16 10:00", tz="Asia/Kolkata").to_pydatetime()
    assert expected_completed_session(cal, None, ist) == ("2026-03-12", True)  # Monday morning -> Thursday
    eve = pd.Timestamp("2026-03-16 16:00", tz="Asia/Kolkata").to_pydatetime()
    assert expected_completed_session(cal, None, eve) == ("2026-03-16", True)


def test_owner_verifier_flags_ungated_live_and_passes_clean_db(tmp_path: Path) -> None:
    from stock_gplm.app.m59a_verify import verify

    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert db.add_prediction(_row(natural_key="legacy-live", actionable=0, payload={}))
    db.close()
    clean = verify(tmp_path / "a.sqlite")
    assert clean["ok"] and clean["info"]["legacy_live_rows"] == 1
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert db.add_prediction(_row(natural_key="bad", actionable=0,
                                  payload={"engine": "gplm-rolling.v2", "status": "SHADOW"}))
    db.close()
    bad = verify(tmp_path / "a.sqlite")
    assert not bad["ok"] and bad["violations"]["stale_or_ungated_live"] == 1
