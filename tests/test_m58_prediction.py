"""Frozen prediction/reveal tests with synthetic candles only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from stock_gplm.app.db import AuditDB
from stock_gplm.app.ingest import SymbolData
from stock_gplm.app import m58_prediction as mp
from stock_gplm.verify import fixtures as fx


class Settings:
    def __init__(self, root: Path):
        self.cache_dir = root / "cache"
        self.reports_dir = root / "reports"

    def section(self, name: str):
        return {
            "costs": {"delivery": {"commission_bps": 0, "slippage_bps": 0, "buy_statutory_bps": 0,
                                    "sell_statutory_bps": 0},
                      "intraday": {"commission_bps": 0, "slippage_bps": 0, "buy_statutory_bps": 0,
                                   "sell_statutory_bps": 0}},
            "m58": {"stock_dna": {"discovery_fraction": 0.55, "validation_fraction": 0.25,
                                     "quantiles": [0.2, 0.8], "max_features": 10,
                                     "min_validation_observations": 8, "fdr_q": 0.10},
                    "prediction": {"minimum_calibration_outcomes": 5}},
            "prediction": {"min_probability": 0.56},
        }.get(name, {})


class SnapshotSettings(Settings):
    def section(self, name: str):
        if name == "feature_space":
            return {"snapshot_indicators": {
                "EMA": {"params": {"period": [3]}},
                "RSI": {"params": {"period": [4]}},
                "VWAP": {"params": {"session": [True]}},
                "VOLUME": {"params": {"period": [3]}},
            }}
        return super().section(name)


def _data() -> SymbolData:
    f = fx.ohlcv(540, seed=919)
    d = f.rename(columns={"session": "date"}).drop(columns="end_ns")
    d["source"] = "SYNTHETIC"
    return SymbolData("SYN-EQ", d, pd.DataFrame(), "m58-predict-fixture", {"minute_bars": 0})


def _minute_data() -> tuple[SymbolData, str, str]:
    base = _data()
    sessions = base.daily["date"].iloc[496:500].astype(str).tolist()
    rows = []
    value = 100.0
    for session in sessions[:3]:
        for minute in range(375):
            stamp = pd.Timestamp(f"{session} 09:15") + pd.Timedelta(int(minute), unit="min")
            value += 0.01
            rows.append({"start_ist": stamp, "session": session, "open": value - 0.02,
                         "high": value + 0.05, "low": value - 0.05, "close": value,
                         "volume": 100.0 + minute})
    # Make the 10:45-11:00 bucket incomplete; prediction must fall back to the 10:30-10:45 bar.
    current = sessions[2]
    rows = [r for r in rows if not (r["session"] == current and r["start_ist"].strftime("%H:%M") == "10:50")]
    minute = pd.DataFrame(rows)
    data = SymbolData(base.symbol, base.daily, minute, "m58-minute-fixture", {"minute_bars": len(minute)})
    return data, current, sessions[3]


def test_prediction_is_frozen_before_separate_reveal_and_evaluates(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    data = _data()
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    app = SimpleNamespace(s=Settings(tmp_path), db=db)
    as_of = str(data.daily["date"].iloc[499])
    target = str(data.daily["date"].iloc[500])
    frozen = mp.predict(app, "SYN", as_of, target, "1d", 1, tmp_path / "frozen")
    assert frozen["status"] == "FROZEN" and frozen["target_session"] == target
    row = db.one("SELECT * FROM predictions WHERE prediction_id=?", (frozen["prediction_id"],))
    assert row is not None and row["mode"] == "HISTORICAL_FROZEN"
    payload = json.loads(row["payload_json"])
    assert payload["data_last_session"] == as_of and payload["requested_as_of"] == as_of
    assert db.one("SELECT * FROM evaluations WHERE prediction_id=?", (frozen["prediction_id"],)) is None
    with pytest.raises(Exception):
        db.conn.execute("UPDATE predictions SET direction='UP' WHERE prediction_id=?", (frozen["prediction_id"],))

    revealed = mp.reveal(app, frozen["prediction_id"])
    assert revealed["status"] == "REVEALED" and revealed["target_session"] == target
    assert db.one("SELECT * FROM evaluations WHERE prediction_id=?", (frozen["prediction_id"],)) is not None
    again = mp.reveal(app, frozen["prediction_id"])
    assert again["status"] == "ALREADY_REVEALED"
    report = mp.evaluate(app, "SYN", tmp_path / "evaluation")
    assert report["predictions"] == 1 and report["revealed"] == 1
    assert report["abstention_rate"] in (0.0, 1.0)
    db.close()


def test_future_mutation_cannot_change_frozen_prediction_identity(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    data = _data()
    as_of = str(data.daily["date"].iloc[499])
    target = str(data.daily["date"].iloc[500])
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    app = SimpleNamespace(s=Settings(tmp_path), db=db)
    first = mp.predict(app, "SYN", as_of, target, "1d", 1, tmp_path / "frozen")
    data.daily.loc[500:, ["open", "high", "low", "close", "volume"]] *= 11
    second = mp.predict(app, "SYN", as_of, target, "1d", 1, tmp_path / "frozen")
    assert second["status"] == "ALREADY_FROZEN" and second["prediction_id"] == first["prediction_id"]
    assert db.one("SELECT COUNT(*) n FROM predictions")["n"] == 1
    db.close()


def test_walk_forward_freezes_before_revealing_and_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _data()
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    app = SimpleNamespace(s=Settings(tmp_path), db=db)
    start = str(data.daily["date"].iloc[497]); end = str(data.daily["date"].iloc[499])
    first = mp.walk_forward(app, "SYN", start, end, "1d", 1, tmp_path / "wf", max_steps=2)
    assert first["status"] == "COMPLETED" and first["steps_done"] == 2
    assert first["newly_frozen"] == 2 and first["newly_revealed"] == 2
    assert len(first["steps"]) == first["evaluation"]["predictions"] == 2
    assert first["evaluation"]["directional_accuracy_denominator"] == sum(
        s["correctness"] in {"CORRECT", "INCORRECT"} for s in first["steps"])
    table = mp.format_step_table(first["steps"])
    assert len(table) == 3 and table[0].startswith("Time") and "NO_TRADE" in "\n".join(table)
    second = mp.walk_forward(app, "SYN", start, end, "1d", 1, tmp_path / "wf", max_steps=2)
    assert second["newly_frozen"] == 0 and second["newly_revealed"] == 0
    assert db.one("SELECT COUNT(*) n FROM predictions")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM evaluations")["n"] == 2
    db.close()


def test_snapshot_is_frozen_declarative_and_reproducible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _data()
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    app = SimpleNamespace(s=SnapshotSettings(tmp_path), db=db)
    start = str(data.daily["date"].iloc[497]); end = str(data.daily["date"].iloc[498])
    target = str(data.daily["date"].iloc[498])
    frozen = mp.predict(app, "SYN", start, target, "1d", 1, tmp_path / "wf" / "frozen")
    snap_path = Path(frozen["feature_snapshot_report"])
    before = snap_path.read_bytes()
    mp.reveal(app, frozen["prediction_id"])
    assert snap_path.read_bytes() == before
    first = mp.walk_forward(app, "SYN", start, end, "1d", 1, tmp_path / "wf", max_steps=1)
    snapshot = json.loads(before)
    frame = snapshot["timeframes"]["1d"]
    assert "EMA(period=3)" in frame["contextual_indicators"]
    assert frame["contextual_indicators"]["VWAP(session=true)"]["value"] is None
    assert "accepted minute bars" in frame["contextual_indicators"]["VWAP(session=true)"]["reason"]
    assert snapshot["patterns"]["fired"] or snapshot["patterns"]["not_fired"]
    csv_text = (tmp_path / "wf" / "walk-forward-steps.csv").read_text(encoding="utf-8")
    assert "1d_EMA_3" in csv_text and "1d_VWAP_True" in csv_text
    second = mp.walk_forward(app, "SYN", start, end, "1d", 1, tmp_path / "wf", max_steps=1)
    assert second["newly_frozen"] == 0 and snap_path.read_bytes() == before
    db.close()


def test_multitimeframe_alignment_excludes_incomplete_bar_and_vwap_is_causal(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data, current, target = _minute_data()
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    app = SimpleNamespace(s=SnapshotSettings(tmp_path), db=db)
    frozen = mp.predict(app, "SYN", f"{current}T11:00:00+05:30", target, "15m", 1, tmp_path / "frozen")
    snapshot = db.get_feature_snapshot(frozen["feature_snapshot_sha256"])
    assert snapshot is not None
    frames = snapshot["timeframes"]
    assert pd.Timestamp(frames["15m"]["bar_end"]).tz_convert("Asia/Kolkata").strftime("%H:%M") == "10:45"
    assert pd.Timestamp(frames["60m"]["bar_end"]).tz_convert("Asia/Kolkata").strftime("%H:%M") == "10:15"
    assert pd.Timestamp(frames["1d"]["bar_end"]).tz_convert("Asia/Kolkata").date().isoformat() < current
    used = data.minute[(data.minute["session"] == current) &
                       (data.minute["start_ist"] < pd.Timestamp(f"{current} 10:45"))]
    expected = (((used["high"] + used["low"] + used["close"]) / 3 * used["volume"]).sum() /
                used["volume"].sum())
    actual = frames["15m"]["contextual_indicators"]["VWAP(session=true)"]["value"]
    assert actual == pytest.approx(expected)
    daily = mp.predict(app, "SYN", current, target, "1d", 1, tmp_path / "frozen-daily")
    assert daily["prediction_id"] != frozen["prediction_id"]
    assert mp.evaluate(app, "SYN", tmp_path / "eval-15m", timeframe="15m", horizon=1)["predictions"] == 1
    assert mp.evaluate(app, "SYN", tmp_path / "eval-1d", timeframe="1d", horizon=1)["predictions"] == 1
    intraday = mp.walk_forward(app, "SYN", current, current, "15m", 1, tmp_path / "wf-15m", max_steps=2)
    assert intraday["steps_done"] == 2
    assert all("T" in step["target_timestamp"] for step in intraday["steps"])
    assert len({step["prediction_id"] for step in intraday["steps"]}) == 2
    db.close()


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: SymbolData, settings=SnapshotSettings):
    class FakeIngestor:
        def __init__(self, *_a, **_k): pass
        def ingest(self, *_a, **_k): return data
    monkeypatch.setattr(mp, "Ingestor", FakeIngestor)
    db = AuditDB(tmp_path / "gplm.sqlite", tmp_path / "events", run_id="test-run")
    return SimpleNamespace(s=settings(tmp_path), db=db)


def test_named_run_is_range_specific_and_reuses_without_duplicates(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    data = _data()
    app = _app(monkeypatch, tmp_path, data)
    d = [str(x) for x in data.daily["date"]]
    # Prior history for the same stock (outside the named range) must not leak into the named run.
    mp.walk_forward(app, "SYN", d[494], d[496], "1d", 1, tmp_path / "prior", run_name="prior")
    mp.walk_forward(app, "SYN", d[497], d[497], "1d", 1, tmp_path / "seed", run_name="seed")
    run = mp.walk_forward(app, "SYN", d[497], d[499], "1d", 1, tmp_path / "named", run_name="named")
    assert run["planned"] == run["steps_done"] == 3 and run["evaluation"]["predictions"] == 3
    assert run["evaluation"]["mode"] == "NAMED_RUN"
    assert [s["prediction_status"] for s in run["steps"]] == ["REUSED", "NEWLY_CREATED", "NEWLY_CREATED"]
    assert run["newly_frozen"] == 2 and run["reused_predictions"] == 1
    assert all(d[497] <= s["target_timestamp"] and s["timeframe"] == "1d" and s["horizon"] == 1
               for s in run["steps"])
    before = db_counts = (app.db.one("SELECT COUNT(*) n FROM predictions")["n"],
                          app.db.one("SELECT COUNT(*) n FROM evaluations")["n"],
                          app.db.one("SELECT COUNT(*) n FROM feature_snapshots")["n"])
    steps_json = (tmp_path / "named" / "walk-forward-steps.json").read_bytes()
    again = mp.walk_forward(app, "SYN", d[497], d[499], "1d", 1, tmp_path / "named", run_name="named")
    assert again["newly_frozen"] == 0 and again["reused_predictions"] == 3
    assert (app.db.one("SELECT COUNT(*) n FROM predictions")["n"],
            app.db.one("SELECT COUNT(*) n FROM evaluations")["n"],
            app.db.one("SELECT COUNT(*) n FROM feature_snapshots")["n"]) == before == db_counts
    assert json.loads((tmp_path / "named" / "walk-forward-steps.json").read_bytes()) == \
        [{**s, "prediction_status": "REUSED"} for s in json.loads(steps_json)]
    # prediction-evaluate filters reproduce the named-run figures; cumulative mode is labelled.
    filtered = mp.evaluate(app, "SYN", tmp_path / "f", start=d[497], end=d[499], timeframe="1d", horizon=1)
    assert filtered["mode"] == "FILTERED" and filtered["predictions"] == 3
    cumulative = mp.evaluate(app, "SYN", tmp_path / "c")
    assert cumulative["mode"] == "CUMULATIVE" and cumulative["predictions"] == 6
    app.db.close()


def test_totals_and_table_match_displayed_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = _data()
    app = _app(monkeypatch, tmp_path, data)
    d = [str(x) for x in data.daily["date"]]
    run = mp.walk_forward(app, "SYN", d[495], d[499], "1d", 1, tmp_path / "wf", run_name="wf")
    steps, e = run["steps"], run["evaluation"]
    table = mp.format_step_table(steps)
    assert len(table) == len(steps) + 1
    assert table[0].startswith("Time")
    for header in ("Prediction", "Probability", "Actual", "Return", "Correct", "Trade"):
        assert header in table[0]
    assert e["correct_directional"] == sum(s["correctness"] == "CORRECT" for s in steps)
    assert e["incorrect_directional"] == sum(s["correctness"] == "INCORRECT" for s in steps)
    assert e["abstentions"] == sum(s["predicted_direction"] == "UNCERTAIN" for s in steps)
    assert e["trades"] == sum(s["trade_decision"] == "TRADE" for s in steps)
    assert e["brier_denominator"] == sum(s["calibrated_probability"] is not None for s in steps)
    totals = "\n".join(mp.format_run_totals(run))
    assert "not cumulative" in totals and f"completed {len(steps)}" in totals
    assert f"executable trades {e['trades']}" in totals
    summary = json.loads((tmp_path / "wf" / "walk-forward-summary.json").read_text(encoding="utf-8"))
    assert len(summary["steps"]) == len(steps) and "feature_snapshot" not in summary["steps"][0]
    snap = app.db.get_feature_snapshot(steps[0]["feature_snapshot_sha256"])
    details = "\n".join(mp.format_step_details(steps[0], snap))
    assert "1d EMA(period=3)" in details and "Trade decision:" in details and "Fired patterns:" in details
    app.db.close()


def test_classification_abstention_trade_and_missing_confidence_are_distinct() -> None:
    rows = [
        {"direction": "UP", "realized_return": 0.01, "correct": 1, "prob_calibrated": 0.7, "brier": 0.09,
         "trade_net_return": 0.008},
        {"direction": "DOWN", "realized_return": 0.02, "correct": 0, "prob_calibrated": None, "brier": None,
         "trade_net_return": None},
        {"direction": "UNCERTAIN", "realized_return": -0.01, "correct": None, "prob_calibrated": None,
         "brier": None, "trade_net_return": None},
    ]
    m = mp._metrics(rows)
    assert (m["correct_directional"], m["incorrect_directional"], m["directional_accuracy_denominator"]) == (1, 1, 2)
    assert m["abstentions"] == 1 and m["trades"] == 1 and m["brier_denominator"] == 1
    assert m["total_compounded_trade_return"] == pytest.approx(0.008)
    step = {"prediction_timestamp": "2025-08-01T15:30:00+05:30", "timeframe": "1d", "predicted_direction": "DOWN",
            "displayed_confidence": None, "trade_decision": "NO_TRADE", "actual_direction": "UP",
            "actual_gross_return": 0.021, "actual_net_return": 0.02, "correctness": "INCORRECT"}
    line = mp.format_step_table([step])[1]
    assert line.split() == ["2025-08-01", "DOWN", "—", "UP", "+2.10%", "NO", "NO_TRADE"]
    intraday = mp.format_step_table([{**step, "timeframe": "15m", "displayed_confidence": 0.67,
                                      "prediction_timestamp": "2025-08-01T10:45:00+05:30"}])[1]
    assert intraday.startswith("2025-08-01 10:45") and "67%" in intraday


def test_legacy_prediction_gets_one_labelled_stable_reconstruction(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    data = _data()
    app = _app(monkeypatch, tmp_path, data)
    d = [str(x) for x in data.daily["date"]]
    original = mp._resolve_snapshot
    monkeypatch.setattr(mp, "_resolve_snapshot", lambda _a, _p, snap: (dict(snap), "", "PRE_SNAPSHOT"))
    legacy = mp.predict(app, "SYN", d[498], d[499], "1d", 1, tmp_path / "legacy")
    monkeypatch.setattr(mp, "_resolve_snapshot", original)
    assert json.loads(app.db.one("SELECT payload_json FROM predictions")["payload_json"])[
        "feature_snapshot_sha256"] == ""
    first = mp.walk_forward(app, "SYN", d[498], d[498], "1d", 1, tmp_path / "wf", run_name="wf")
    second = mp.walk_forward(app, "SYN", d[498], d[498], "1d", 1, tmp_path / "wf", run_name="wf")
    step1, step2 = first["steps"][0], second["steps"][0]
    assert step1["prediction_id"] == legacy["prediction_id"] and step1["prediction_status"] == "REUSED"
    assert step1["feature_snapshot_origin"] == mp.SNAPSHOT_LEGACY
    assert step1["feature_snapshot_sha256"] == step2["feature_snapshot_sha256"]
    assert app.db.one("SELECT COUNT(*) n FROM predictions")["n"] == 1
    app.db.close()
