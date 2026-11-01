from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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
