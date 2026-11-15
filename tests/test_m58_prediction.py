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


def _data() -> SymbolData:
    f = fx.ohlcv(540, seed=919)
    d = f.rename(columns={"session": "date"}).drop(columns="end_ns")
    d["source"] = "SYNTHETIC"
    return SymbolData("SYN-EQ", d, pd.DataFrame(), "m58-predict-fixture", {"minute_bars": 0})


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
    second = mp.walk_forward(app, "SYN", start, end, "1d", 1, tmp_path / "wf", max_steps=2)
    assert second["newly_frozen"] == 0 and second["newly_revealed"] == 0
    assert db.one("SELECT COUNT(*) n FROM predictions")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM evaluations")["n"] == 2
    db.close()
