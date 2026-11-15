"""Synthetic, targeted tests for the M5.8 reusable feature catalogue."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from stock_gplm.app.feature_engine import TIMEFRAMES, compute_lane, coverage_rows, load_catalogue
from stock_gplm.m54 import timeframes as tfm
from stock_gplm.verify import fixtures as fx


def test_authoritative_catalogue_has_eleven_modules_and_preserves_tracked_gaps() -> None:
    doc, digest = load_catalogue()
    assert len({row["module"] for row in doc["rows"]}) == 11
    assert len(doc["rows"]) == 125 and len(digest) == 64
    missing = [row for row in doc["rows"] if any(m["kind"] == "NOT_IMPLEMENTED"
                                                   for m in row.get("missing", []))]
    assert len([row for row in missing if not row.get("exprs")]) == 11
    assert len([row for row in missing if row.get("exprs")]) == 5
    assert TIMEFRAMES == ("1m", "5m", "15m", "30m", "60m", "4h", "1d", "1w")


def test_catalogue_arrays_are_computed_once_cached_and_blockers_stay_explicit(tmp_path: Path) -> None:
    frame = fx.ohlcv(300)
    frame["date"] = frame.pop("session")
    bars = tfm.daily_bars(frame)
    rows = [
        {"module": "1", "name": "SMA", "exprs": ["sma(close, 20)"], "verified_by": "SMA(20)"},
        {"module": "2", "name": "Missing algorithm", "exprs": [],
         "missing": [{"kind": "NOT_IMPLEMENTED", "what": "algorithm", "reason": "not built"}]},
        {"module": "3", "name": "Session VWAP", "exprs": ["vwap"], "intraday": True},
    ]
    matrix = coverage_rows("SYN-EQ", "1d", bars, "fp-1", rows, "c" * 64, tmp_path)
    assert matrix[0]["implementation_status"] == "COMPUTED"
    assert matrix[0]["valid_observations"] == 281 and matrix[0]["warmup_bars"] == 19
    assert matrix[0]["verification_status"] == "INDEPENDENT_REFERENCE_TEST_CONFIGURED"
    assert matrix[1]["implementation_status"] == "BLOCKED_IMPLEMENTATION"
    assert matrix[2]["implementation_status"] == "NOT_APPLICABLE"
    arrays, errors, state = compute_lane("SYN-EQ", "1d", bars, "fp-1", rows, "c" * 64, tmp_path)
    assert state == "REUSED" and not errors and np.isfinite(arrays["sma(close, 20)"][19:]).all()
    meta = next(tmp_path.rglob("*.json"))
    assert json.loads(meta.read_text())["contract"] == "stock-gplm.feature-engine.v1"


def test_missing_bars_are_not_reported_as_computed(tmp_path: Path) -> None:
    rows = [{"module": "1", "name": "EMA", "exprs": ["ema(close, 20)"]}]
    got = coverage_rows("SYN-EQ", "4h", None, "fp", rows, "d" * 64, tmp_path)[0]
    assert got["implementation_status"] == "BLOCKED_MISSING_DATA"
    assert got["valid_observations"] == 0 and "no accepted 4h candles" in got["missing_data_reason"]
