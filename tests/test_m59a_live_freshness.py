"""M5.9A regression tests: absolute freshness and stale suppression, the safe meaning of LIVE in the
rolling engine, report session handling, and the latest completed intraday candle. Synthetic data only
(stock_gplm.m54.fixture); bounded runs (``--max-steps``, one or two symbols)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from stock_gplm.app import cli
from stock_gplm.app import contracts as C
from stock_gplm.app.compute import resolve
from stock_gplm.app.db import AuditDB
from stock_gplm.app.ingest import Ingestor, SymbolData
from stock_gplm.app.lanes import all_bars, build_lane
from stock_gplm.app.library import Library
from stock_gplm.app.progress import Progress
from stock_gplm.app.report import live_forecasts
from stock_gplm.app.rolling import LaneReplay
from stock_gplm.app.session_calendar import SessionCalendar
from stock_gplm.app.settings import DEFAULT_CONFIG, load
from stock_gplm.app.daily import intraday_freshness
from stock_gplm.app.targets import EOD_NEXT_SESSION, derive_target, label_ns
from stock_gplm.m54.candles import ist_ns
from stock_gplm.m54.fixture import make_fixture


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("m59a")
    raw = base / "raw"
    make_fixture(raw, ("AAA", "BBB"), start="2025-06-01", end="2026-03-31", minute_from="2026-01-01",
                 cutoff="2026-03-02", seed=21)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["home"] = str(base / "home")
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["rolling"].update(start="2026-03-16", min_train_rows=60, mlp_epochs_full=5, mlp_epochs_update=2,
                          rediscover_every=50, calibration_min_outcomes=10, champion_min_outcomes=8,
                          champion_window=30)
    cfg["models"]["logit"]["epochs"] = 20
    cfg["horizons"].update({"1d": [1], "15m": [1]})
    cfg["timeframes"]["intraday_min_sessions"] = 20
    cfg["prediction"]["min_rolling_outcomes"] = 5
    cfg["discovery"]["engine_config"] = str(DEFAULT_CONFIG.parent.parent / "m54" / "unified-default.json")
    path = base / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return {"base": base, "config": path}


def _settings(world: dict, home: Path):
    s = load(world["config"], home)
    s.ensure()
    return s


# ----------------------------------------------------------------------------- D. freshness (unit)
def test_freshness_is_absolute_one_mixed_all_stale_and_historical() -> None:
    # one stale symbol on its own: the old batch-max rule could never see this
    one = C.evaluate_freshness("2026-03-11", "2026-03-12", calendar_confirmed=True)
    assert one.status == C.STALE_DATA
    mixed = {s: C.evaluate_freshness(last, "2026-03-12", calendar_confirmed=True)
             for s, last in (("A", "2026-03-12"), ("B", "2026-03-10"))}
    assert mixed["A"].fresh and mixed["B"].status == C.STALE_DATA
    every = [C.evaluate_freshness(x, "2026-03-12", calendar_confirmed=True) for x in ("2026-03-09", "2026-03-10")]
    assert not any(f.fresh for f in every)
    approved = {"model_id": "m", "artifact_sha256": "a" * 64}
    assert all(C.live_gate(latest_eligible_bar=True, freshness=f, model_approval=approved).status == C.UNAVAILABLE
               for f in every)
    # historical replay: data runs past the explicit as-of session -> not stale
    assert C.evaluate_freshness("2026-03-20", "2026-03-12", calendar_confirmed=True).fresh
    # owner-asserted session without a registered calendar: staleness still detected, but never LIVE
    unconfirmed = C.evaluate_freshness("2026-03-12", "2026-03-12", calendar_confirmed=False)
    assert unconfirmed.status == C.CALENDAR_UNKNOWN
    assert C.evaluate_freshness("2026-03-11", "2026-03-12", calendar_confirmed=False).status == C.STALE_DATA
    assert C.evaluate_freshness("2026-03-12", None, calendar_confirmed=False).status == C.CALENDAR_UNKNOWN
    assert C.evaluate_freshness("2026-03-12", "2026-03-12", calendar_confirmed=True,
                                quarantined=True).status == C.QUARANTINED_INPUT


def _pred(db: AuditDB, key: str, sym: str, label: str, mode: str, payload: dict, actionable: int = 0) -> None:
    opp = {"entry": "next open", "invalidation": "below stop", "expected_return": 0.01, "model_version": "m@sha",
           "freshness_status": "FRESH"}
    db.add_prediction({"natural_key": key, "mode": mode, "symbol": sym, "timeframe": "1d", "lane_key": f"{sym}|1d",
                       "source_kind": "MODEL", "source_id": "logit", "source_version": "rp", "horizon_bars": 1,
                       "decision_label": label, "decision_ns": ist_ns(label, "15:30"), "direction": "LONG",
                       "prob_calibrated": 0.6, "actionable": actionable, "entry_ref": 100.0, "stop": 98.0,
                       "target": 104.0, "payload": {**payload, "opportunity": opp}})


def test_report_uses_requested_session_and_never_shows_stale_or_unapproved_as_live(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    _pred(db, "a1", "A", "2026-03-11", "SHADOW", {"status": "SHADOW", "status_reasons": ["MODEL_NOT_APPROVED"]})
    _pred(db, "b-legacy", "B", "2026-03-12", "LIVE", {})  # pre-M5.9A row: never passed the gate
    _pred(db, "c-live", "C", "2026-03-12", "LIVE", {"status": "LIVE", "status_reasons": []}, actionable=1)
    fc = {f["symbol"]: f for f in live_forecasts(db, ["A", "B", "C"], session="2026-03-12")}
    assert fc["A"]["status"] == C.UNAVAILABLE and fc["A"]["reason_code"] == C.STALE_DATA and not fc["A"]["actionable"]
    assert fc["B"]["status"] == "LEGACY_UNVERIFIED_LIVE" and not fc["B"]["actionable"]
    assert fc["B"]["candidates"][0]["actionable"] == 0
    assert fc["C"]["status"] == C.LIVE and len(fc["C"]["actionable"]) == 1
    older = {f["symbol"]: f for f in live_forecasts(db, ["A"], session="2026-03-11")}
    assert older["A"]["status"] == C.SHADOW and not older["A"]["actionable"]
    db.close()


# ----------------------------------------------------------------------------- D/E. daily end to end (bounded)
def test_daily_all_stale_suppresses_live_and_reports_stale(world: dict, tmp_path: Path) -> None:
    home = tmp_path / "home"
    args = ["--config", str(world["config"]), "--home", str(home), "--quiet", "--no-intraday", "--max-steps", "2"]
    # the requested session is after every symbol's last candle: every symbol is stale
    assert cli.main(["daily", "--stocks", "AAA,BBB", "--device", "cpu", "--as-of", "2026-04-30", *args]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    assert not db.q("SELECT 1 FROM predictions WHERE mode='LIVE' OR actionable=1 AND mode!='REPLAY'")
    summary = json.loads((home / "reports" / "latest" / "summary.json").read_text(encoding="utf-8"))
    assert all(v["stale"] and v["freshness"]["status"] == C.STALE_DATA for v in summary["data"].values())
    fc = json.loads((home / "reports" / "latest" / "forecasts.json").read_text(encoding="utf-8"))
    assert {f["status"] for f in fc} == {C.UNAVAILABLE} and not any(f["actionable"] for f in fc)
    stale_events = db.q("SELECT subject FROM events WHERE kind='DATA_STALE'")
    assert {e["subject"] for e in stale_events} == {"AAA-EQ", "BBB-EQ"}
    db.close()


def _april_calendar() -> SessionCalendar:
    from datetime import date, timedelta
    st = {}
    for i in range(45):
        d = date(2026, 3, 1) + timedelta(days=i)
        st[d.isoformat()] = "MARKET_CLOSED" if d.weekday() >= 5 else "TRADING_SESSION"
    return SessionCalendar.from_statuses(st, source="synthetic-test-calendar", as_of_date="2026-03-01")


def _daily_replay(world: dict, home: Path, ctx: dict):
    s = _settings(world, home)
    db = AuditDB(s.db_path, s.events_dir)
    db.start_run("replay", "t", {}, device="cpu", code_sha="x", config_sha="y")
    d = Ingestor(s, db).ingest("AAA")
    bars = all_bars(d)
    lane = build_lane(d, "1d", s.section("costs"), bars=bars)
    fresh = C.evaluate_freshness(d.last_session, d.last_session, calendar_confirmed=True)
    rp = LaneReplay(db, lane, [1], dict(s.raw), resolve("cpu"), Progress(None, quiet=True), s.models_dir,
                    Library(s.strategies_dir, db), bars, live_context={"freshness": fresh, **ctx})
    return rp.run(), db, lane


def test_unapproved_rolling_refit_on_fresh_latest_bar_is_shadow_not_live(world: dict, tmp_path: Path) -> None:
    out, db, lane = _daily_replay(world, tmp_path / "cal", {"calendar": _april_calendar()})
    rows = db.q("SELECT * FROM predictions WHERE decision_ns=?", (int(lane.market.end_ns[-1]),))
    assert rows and all(r["mode"] == "SHADOW" and r["actionable"] == 0 for r in rows)
    assert all(r["target_session"] == "2026-04-01" for r in rows if r["source_kind"] == "MODEL")
    models = [json.loads(r["payload_json"]) for r in rows if r["source_kind"] == "MODEL"]
    assert models and all(p["status"] == C.SHADOW and C.MODEL_NOT_APPROVED in p["status_reasons"] for p in models)
    assert all(p["model_id"] and p["model_artifact_sha256"] for p in models)
    assert out["latest_bar"]["status"] == C.SHADOW
    assert not db.q("SELECT 1 FROM predictions WHERE mode='LIVE'")
    # target-horizon contract on every stored row
    for r in db.q("SELECT target_session, horizon_bars, payload_json FROM predictions WHERE source_kind='MODEL'"):
        tc = json.loads(r["payload_json"])["target_contract"]
        assert tc["declared_horizon"] == r["horizon_bars"] and tc["derived_target"] == r["target_session"]
    db.close()
    # Without a registered calendar the latest daily bar's target cannot be derived: UNAVAILABLE, nothing guessed.
    out2, db2, lane2 = _daily_replay(world, tmp_path / "nocal", {})
    latest = [json.loads(r["payload_json"]) for r in db2.q(
        "SELECT payload_json FROM predictions WHERE source_kind='MODEL' AND decision_ns=?",
        (int(lane2.market.end_ns[-1]),))]
    assert latest and all(p["status"] == C.UNAVAILABLE and p["status_reasons"] == [C.CALENDAR_UNKNOWN]
                          for p in latest)
    assert out2["latest_bar"]["status"] == C.UNAVAILABLE
    db2.close()


# ----------------------------------------------------------------------------- G. intraday latest completed candle
def test_intraday_targets_mid_session_final_bar_and_next_session() -> None:
    cal = SessionCalendar.from_statuses({"2026-03-12": "TRADING_SESSION", "2026-03-13": "TRADING_SESSION"},
                                        source="synthetic-test-calendar", as_of_date="2026-03-01")
    mid = derive_target("15m", "2026-03-12", ist_ns("2026-03-12", "11:00"), 1, calendar=cal)
    assert mid["derivation"] == "SESSION_GRID" and label_ns(mid["target"]) == ist_ns("2026-03-12", "11:15")
    last = derive_target("15m", "2026-03-12", ist_ns("2026-03-12", "15:30"), 1, calendar=cal)
    assert last["status"] == "UNAVAILABLE" and last["reason"] == C.END_OF_SESSION
    nxt = derive_target("15m", "2026-03-12", ist_ns("2026-03-12", "15:30"), 1, calendar=cal,
                        eod_policy=EOD_NEXT_SESSION)
    assert label_ns(nxt["target"]) == ist_ns("2026-03-13", "09:30")
    nocal = derive_target("15m", "2026-03-12", ist_ns("2026-03-12", "15:30"), 1, eod_policy=EOD_NEXT_SESSION)
    assert nocal["reason"] == C.CALENDAR_UNKNOWN  # nothing manufactured
    # 4h: 09:15-13:15 then a legitimate short 13:15-15:30 bar
    four = derive_target("4h", "2026-03-12", ist_ns("2026-03-12", "13:15"), 1)
    assert label_ns(four["target"]) == ist_ns("2026-03-12", "15:30")


def _intraday_lane(world: dict, tmp_path: Path, keep_until: str | None):
    s = _settings(world, tmp_path / "h")
    db = AuditDB(s.db_path, s.events_dir)
    db.start_run("replay", "t", {}, device="cpu", code_sha="x", config_sha="y")
    d = Ingestor(s, db).ingest("AAA")
    minute = d.minute
    last_session = str(minute["session"].iloc[-1])
    if keep_until:
        stamp = pd.to_datetime(minute["start_ist"])
        keep = (minute["session"] != last_session) | (stamp.dt.strftime("%H:%M") < keep_until)
        minute = minute[keep.to_numpy()].reset_index(drop=True)
    data = SymbolData(d.symbol, d.daily, minute, d.fingerprint + (keep_until or ""), d.summary)
    bars = all_bars(data)
    lane = build_lane(data, "15m", s.section("costs"), bars=bars)
    fresh = C.evaluate_freshness(last_session, last_session, calendar_confirmed=True)
    rp = LaneReplay(db, lane, [1], dict(s.raw), resolve("cpu"), Progress(None, quiet=True), s.models_dir,
                    Library(s.strategies_dir, db), bars, live_context={"freshness": fresh})
    return rp, lane, db, last_session


def test_latest_completed_mid_session_candle_is_a_decision_bar_and_current_partial_is_not(world, tmp_path) -> None:
    # the session is in progress: minutes up to 11:07 exist, so 11:00-11:15 is the incomplete current candle
    rp, lane, db, session = _intraday_lane(world, tmp_path, "11:08")
    m = lane.market
    assert bool(lane.bars.frame["incomplete"].iloc[-1])                       # current partial candle
    assert m.label(rp.latest_idx) == f"{session} 11:00"                       # latest completed candle
    steps = dict(rp._steps())
    assert rp.latest_idx in steps[session].tolist()                           # it is a decision bar
    assert rp.grid_left[rp.latest_idx] >= 1
    t = rp._target(rp.latest_idx, 1)
    assert label_ns(t["target"]) == ist_ns(session, "11:15")                  # next candle, not manufactured data
    # stale within the session: at 12:00 the 11:45 bar should exist
    now = pd.Timestamp(f"{session} 12:00", tz="Asia/Kolkata").to_pydatetime()
    base = C.evaluate_freshness(session, session, calendar_confirmed=True)
    assert intraday_freshness(base, lane, now).status == C.STALE_DATA
    early = pd.Timestamp(f"{session} 11:10", tz="Asia/Kolkata").to_pydatetime()
    assert intraday_freshness(base, lane, early).fresh
    db.close()


def test_final_completed_session_candle_is_explicitly_unavailable(world, tmp_path) -> None:
    rp, lane, db, session = _intraday_lane(world, tmp_path, None)
    assert lane.market.label(rp.latest_idx) == f"{session} 15:30"
    assert rp.grid_left[rp.latest_idx] == 0
    assert rp.latest_idx not in dict(rp._steps())[session].tolist()
    summary = rp._latest_summary()
    assert summary["status"] == C.UNAVAILABLE and summary["reasons"] == [C.END_OF_SESSION]
    db.close()
