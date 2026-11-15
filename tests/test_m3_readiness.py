from __future__ import annotations

import csv
from stock_gplm import locking as fcntl
import gzip
import hashlib
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from stock_gplm.m3.readiness import build_readiness_run


HEADER = "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"


def _master(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "NSE_symbols.txt",
            HEADER
            + "NSE,1,1,RELIANCE,RELIANCE-EQ,EQ,0.05,\n"
            + "NSE,2,1,TCS,TCS-EQ,EQ,0.05,\n",
        )
    return path


def _publish(root: Path, symbol: str, count: int) -> Path:
    directory = root / symbol
    directory.mkdir(parents=True)
    start = date(2026, 1, 1)
    end = start + timedelta(days=count - 1)
    path = directory / f"{start.isoformat()}_{end.isoformat()}.json.gz"
    records = []
    for index in range(count):
        day = start + timedelta(days=index)
        close = 100 + index
        records.append(json.dumps({
            "time": day.strftime("%d-%b-%Y").upper(),
            "into": f"{close - 1}.00", "inth": f"{close + 2}.00",
            "intl": f"{close - 2}.00", "intc": f"{close}.00",
            "intv": str(1000 + index),
            "ssboe": str(int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())),
        }))
    raw = gzip.compress(json.dumps(records).encode(), mtime=0)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_name(path.name.removesuffix(".json.gz") + ".meta.json").write_text(json.dumps({
        "records": count, "sha256_compressed": digest,
        "quality_status": "UNVALIDATED_RAW",
    }))
    Path(f"{path}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": path.name, "sha256": digest,
        "bytes": len(raw), "records": count,
    }))
    return path


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_readiness_run_is_deterministic_read_only_and_labels_missing_evidence(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    _publish(raw, "RELIANCE-EQ", 40)
    master = _master(tmp_path / "master.zip")
    before = _hashes(raw)

    run = build_readiness_run(
        raw_root=raw, master=master, output_root=tmp_path / "reports",
    )

    assert _hashes(raw) == before
    quality = json.loads((run / "coverage-quality.json").read_text())
    inventory = json.loads((run / "inventory.json").read_text())
    assert inventory["symbols_with_validated_candles"] == 1
    assert inventory["zero_validated_candle_symbols"] == 1
    assert inventory["validated_candles"] == 40
    assert quality["decision"] == "INCOMPLETE"
    assert quality["training_readiness"] == "NOT_READY"
    reliance, tcs = quality["coverage"]
    assert reliance["observed_candles"] == 40
    assert reliance["point_in_time_membership"] == "NOT_ESTABLISHED"
    assert tcs["blockers"][0] == "NO_LOCAL_VALIDATED_CANDLES"
    exploration = json.loads((run / "RELIANCE-EQ.exploration.json").read_text())
    assert exploration["indicator_availability"] == {
        "ema20": True, "macd_12_26": True, "macd_signal_9": True,
        "price": True, "rsi14": True, "volume": True,
    }
    assert (run / "RELIANCE-EQ.exploration.svg").exists()
    assert not (run / "TCS-EQ.exploration.svg").exists()
    dataset = (run / "research-dataset.csv").read_text().splitlines()
    assert len(dataset) == 41
    assert dataset[-1].split(",")[-3:] != ["", "", ""]
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["raw_inputs_mutated"] is False
    with pytest.raises(FileExistsError, match="already exists"):
        build_readiness_run(raw_root=raw, master=master, output_root=tmp_path / "reports")


def test_short_history_plots_unavailable_indicators_without_fabrication(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    _publish(raw, "RELIANCE-EQ", 4)
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"),
        output_root=tmp_path / "reports",
    )
    exploration = json.loads((run / "RELIANCE-EQ.exploration.json").read_text())
    assert exploration["decision"] == "INCOMPLETE"
    assert not any(exploration["indicator_availability"][name] for name in (
        "ema20", "rsi14", "macd_12_26", "macd_signal_9",
    ))
    svg = (run / "RELIANCE-EQ.exploration.svg").read_text()
    assert "EMA20 unavailable: 4/20" in svg
    assert "No missing-date explanation" in svg


def test_active_sync_lock_stops_readiness_snapshot(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    lock = raw / ".stock-gplm-m24.lock"
    with lock.open("w", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="active M2.4"):
            build_readiness_run(
                raw_root=raw, master=_master(tmp_path / "master.zip"),
                output_root=tmp_path / "reports",
            )


def _publish_window(root: Path, symbol: str, days: list[date], closes: list[str] | None = None, volume: str = "1000") -> Path:
    directory = root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    closes = closes or ["100.00"] * len(days)
    path = directory / f"{days[0].isoformat()}_{days[-1].isoformat()}.json.gz"
    records = [
        json.dumps({
            "time": day.strftime("%d-%b-%Y").upper(), "into": close, "inth": close,
            "intl": close, "intc": close, "intv": volume,
            "ssboe": str(int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp())),
        })
        for day, close in zip(days, closes)
    ]
    raw = gzip.compress(json.dumps(records).encode(), mtime=0)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    path.with_name(path.name.removesuffix(".json.gz") + ".meta.json").write_text(json.dumps({
        "records": len(days), "sha256_compressed": digest,
    }))
    Path(f"{path}.done").write_text(json.dumps({
        "status": "COMPLETE", "file": path.name, "sha256": digest,
        "bytes": len(raw), "records": len(days),
    }))
    return path


def _csv(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return {row["symbol"]: row for row in csv.DictReader(stream)}


def test_segments_outside_collector_windows_stay_visible_but_add_no_candles(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(4)]
    _publish_window(raw, "RELIANCE-EQ", days)
    overlap = _publish_window(raw, "RELIANCE-EQ", days[1:3], closes=["999.00", "999.00"])
    before = _hashes(raw)
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
        collector_windows=(("2026-01-01", "2026-01-04"),),
    )
    assert _hashes(raw) == before
    inventory = json.loads((run / "inventory.json").read_text())
    assert inventory["validated_candles"] == 4
    assert inventory["excluded_segments"] == [f"RELIANCE-EQ/{overlap.name}"]
    assert inventory["issues"] == []
    assert "999.00" not in (run / "research-dataset.csv").read_text()
    assert overlap.name in (run / "run-manifest.json").read_text()


def test_contradictory_overlap_is_reported_without_a_window_filter(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(4)]
    _publish_window(raw, "RELIANCE-EQ", days)
    _publish_window(raw, "RELIANCE-EQ", days[1:3], closes=["999.00", "999.00"])
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
    )
    codes = [item["code"] for item in json.loads((run / "inventory.json").read_text())["issues"]]
    assert codes.count("CONTRADICTORY_CANDLE") == 2


def test_universe_coverage_compares_observed_sessions_and_keeps_causes_unknown(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(6)]
    _publish_window(raw, "RELIANCE-EQ", days)
    _publish_window(raw, "TCS-EQ", [days[1], days[2], days[4]])
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
    )
    rows = _csv(run / "symbol-coverage.csv")
    tcs = rows["TCS-EQ"]
    assert tcs["observed_sessions_before_first"] == "1"
    assert tcs["observed_sessions_after_last"] == "1"
    assert tcs["unobserved_sessions_within_span"] == "1"
    assert tcs["longest_unobserved_run"] == "1"
    assert tcs["rsi14_available"] == "False"
    universe = json.loads((run / "coverage-quality.json").read_text())["universe"]
    assert universe["observed_session_classification"] == "EMPIRICAL_UNION_NOT_AUTHORITATIVE"
    assert universe["unobserved_session_classification"] == "UNKNOWN"
    assert universe["observed_session_dates"] == 6
    sessions = (run / "observed-sessions.csv").read_text().splitlines()
    assert sessions[1] == "2026-01-01,Thu,1" and sessions[2] == "2026-01-02,Fri,2"


def test_symbol_absent_from_local_data_is_still_reported(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    _publish_window(raw, "RELIANCE-EQ", [date(2026, 1, 1), date(2026, 1, 2)])
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
        collector_windows=(("2026-01-01", "2026-01-02"), ("2026-02-01", "2026-02-02")),
    )
    rows = _csv(run / "symbol-coverage.csv")
    assert rows["TCS-EQ"]["observed_candles"] == "0"
    assert rows["TCS-EQ"]["collector_windows_absent"] == "2"
    assert rows["RELIANCE-EQ"]["collector_windows_absent"] == "1"


def test_ohlcv_flags_are_reported_but_never_change_the_candle(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(3)]
    _publish_window(raw, "RELIANCE-EQ", days, closes=["100.00", "150.00", "150.00"], volume="0")
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
    )
    flags = (run / "ohlcv-flags.csv").read_text()
    assert "RELIANCE-EQ,2026-01-02,LARGE_CLOSE_MOVE,0.500000" in flags
    assert flags.count("ZERO_VOLUME") == 3 and flags.count("FLAT_OHLC") == 3
    assert "150.00" in (run / "research-dataset.csv").read_text()
    rows = _csv(run / "symbol-coverage.csv")
    assert rows["RELIANCE-EQ"]["large_close_moves"] == "1"


def test_long_history_svg_plots_indicators_on_a_true_date_scale(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(40)]
    days += [date(2026, 6, 1) + timedelta(days=index) for index in range(40)]
    closes = [f"{100 + index}.00" for index in range(80)]
    _publish_window(raw, "RELIANCE-EQ", days, closes=closes)
    run = build_readiness_run(
        raw_root=raw, master=_master(tmp_path / "master.zip"), output_root=tmp_path / "reports",
    )
    svg = (run / "RELIANCE-EQ.exploration.svg").read_text()
    ET.fromstring(svg)
    assert "unavailable" not in svg
    assert svg.count('class="series-1"') == 6  # Close, RSI and MACD, each split at the gap.
    assert "<title>2026-06-01 close 140.00" in svg
    assert 'class="legend"' not in svg and "EMA20</text>" in svg


def test_input_bound_does_not_cap_derived_outputs(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    payload = _publish(raw, "RELIANCE-EQ", 40)
    master = _master(tmp_path / "master.zip")
    monkeypatch.setattr(
        "stock_gplm.m3.readiness.MAX_INPUT_BYTES",
        max(payload.stat().st_size, master.stat().st_size, 512),
    )
    run = build_readiness_run(raw_root=raw, master=master, output_root=tmp_path / "reports")
    size = (run / "research-dataset.csv").stat().st_size
    assert size > max(payload.stat().st_size, master.stat().st_size, 512)
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["outputs"]["research-dataset.csv"] == hashlib.sha256(
        (run / "research-dataset.csv").read_bytes()
    ).hexdigest()
