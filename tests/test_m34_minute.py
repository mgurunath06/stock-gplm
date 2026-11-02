"""Synthetic M3.4 tests. They prove contracts, not real collector compatibility."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import stat
import tarfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stock_gplm.m3.features import FEATURE_NAMES
from stock_gplm.m3.fusion import (
    DAILY_COLUMNS, DailySnapshot, assert_causal, causality_audit, fuse_session, select_daily,
)
from stock_gplm.m3.intraday import CONFIRMED_MINUTE_SCHEMAS
from stock_gplm.m3.minute import (
    IST, MinuteSchemaError, SegmentReport, SessionPolicy, parse_record, validate_segment,
    verify_publication,
)
from stock_gplm.m3.minute_publish import import_snapshot, read_snapshot
from stock_gplm.m3.minute_run import build_minute_run
from stock_gplm.m3.readiness import build_readiness_run
from stock_gplm.m3.research import build_research_run
from stock_gplm.m3.splits import LeakageError

POLICY = SessionPolicy("09:15", "09:44", "OBSERVED_NOT_AUTHORITATIVE", "synthetic fixture")
DAY = "2026-03-02"


def _record(day: str, clock: str, *, price: str = "100.00", volume: int = 10, total: int = 10, **changes: str) -> dict[str, str]:
    local = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=IST)
    base = {
        "stat": "Ok", "time": local.strftime("%d-%m-%Y %H:%M:%S"), "ssboe": str(int(local.timestamp())),
        "into": price, "inth": str(Decimal(price) + 1), "intl": str(Decimal(price) - 1), "intc": price,
        "intvwap": price, "intv": str(volume), "intoi": "0", "v": str(total), "oi": "0",
    }
    return {**base, **changes}


def _session(day: str = DAY, minutes: int = 30, *, start: tuple[int, int] = (9, 15), base: int = 100) -> list[dict[str, str]]:
    """Provider order: newest bar first, cumulative volume rising with time."""
    rows = []
    first = datetime(2026, 1, 1, *start)
    for index in range(minutes):
        clock = (first + timedelta(minutes=index)).strftime("%H:%M")
        rows.append(_record(day, clock, price=f"{base + index}.00", volume=10, total=10 * (index + 1)))
    return list(reversed(rows))


def _gz(rows: object) -> bytes:
    return gzip.compress(json.dumps(rows).encode(), mtime=0)


def _triplet(symbol: str, day: str, rows: object, *, token: str = "2885") -> dict[str, bytes]:
    payload = _gz(rows)
    digest = hashlib.sha256(payload).hexdigest()
    count = len(rows) if isinstance(rows, list) else 0
    meta = {
        "source": "shoonya", "symbol": symbol, "token": token, "session_date": day, "session_type": "REGULAR",
        "quality_status": "UNVALIDATED_RAW", "expected_start": "09:15", "expected_end": "15:30",
        "sha256_compressed": digest, "records": count, "fetched_at_utc": "2026-10-30T00:00:00+00:00",
    }
    marker = {
        "status": "COMPLETE", "file": f"{day}.json.gz", "bytes": len(payload), "records": count,
        "sha256": digest, "quality_status": "UNVALIDATED_RAW", "published_at_utc": "2026-10-30T00:00:00+00:00",
    }
    return {
        ".json.gz": payload, ".meta.json": json.dumps(meta).encode(), ".json.gz.done": json.dumps(marker).encode(),
    }


def _master(path: Path, symbols: tuple[tuple[str, str], ...] = (("RELIANCE-EQ", "2885"), ("TCS-EQ", "11536"))) -> Path:
    lines = ["Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,"]
    lines += [f"NSE,{token},1,{symbol[:-3]},{symbol},EQ,0.05," for symbol, token in symbols]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("NSE_symbols.txt", "\n".join(lines) + "\n")
    return path


def _snapshot(tmp_path: Path, triplets: dict[tuple[str, str], dict[str, bytes]]) -> tuple[Path, str, Path]:
    source = tmp_path / "source"
    archive = tmp_path / "snapshot.tar"
    lines = []
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as stream:
        for (symbol, day), parts in sorted(triplets.items()):
            for suffix, data in parts.items():
                path = source / "NSE" / symbol / f"{day}{suffix}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                name = f"NSE/{symbol}/{day}{suffix}"
                stream.add(path, name)
                lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    hashes = tmp_path / "source-hashes.txt"
    hashes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest(), hashes


def _import(tmp_path: Path, triplets: dict[tuple[str, str], dict[str, bytes]], **changes: object) -> tuple[Path, Path]:
    archive, digest, hashes = _snapshot(tmp_path, triplets)
    raw = tmp_path / "minute-raw"
    arguments = dict(
        archive=archive, archive_sha256=digest, source_hashes=hashes, master=_master(tmp_path / "master.zip"),
        raw_root=raw, audit_root=tmp_path / "audit", source_label="synthetic",
    )
    arguments.update(changes)
    return import_snapshot(**arguments), raw  # type: ignore[arg-type]


# --- adapter ---------------------------------------------------------------------------------


def test_v1_record_is_parsed_without_coercion():
    bar = parse_record(_record(DAY, "09:15", volume=7, total=9))
    assert bar.start_ist.utcoffset() == timedelta(hours=5, minutes=30)
    assert bar.start_utc == bar.start_ist
    assert (bar.volume, bar.cumulative_volume) == (7, 9)


@pytest.mark.parametrize("changes, message", [
    ({"extra": "1"}, "exact V1 fields"),
    ({"intv": 5}, "must be strings"),
    ({"stat": "Not_Ok"}, "stat"),
    ({"time": "2026-03-02 09:15:00"}, "format"),
    ({"ssboe": "1"}, "disagrees"),
    ({"into": "NaN"}, "fixed-point"),
    ({"intl": "0"}, "positive"),
    ({"inth": "50.00"}, "bounds"),
    ({"intv": "1.5"}, "whole"),
    ({"intv": "-1.5"}, "whole"),
    ({"v": "-1"}, "fixed-point"),
])
def test_v1_record_rejects_anything_unobserved(changes, message):
    with pytest.raises(MinuteSchemaError, match=message):
        parse_record({**_record(DAY, "09:15"), **changes})


def test_seconds_are_rejected():
    local = datetime(2026, 3, 2, 9, 15, 30, tzinfo=IST)
    record = {**_record(DAY, "09:15"), "time": local.strftime("%d-%m-%Y %H:%M:%S"), "ssboe": str(int(local.timestamp()))}
    with pytest.raises(MinuteSchemaError, match="whole minute"):
        parse_record(record)


def test_label_schema_stays_closed():
    assert CONFIRMED_MINUTE_SCHEMAS == frozenset()


# --- segment quality --------------------------------------------------------------------------


def test_clean_session_is_accepted_and_normalized_ascending():
    report = validate_segment(_gz(_session()), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "ACCEPT"
    assert report.regular_bars == 30 and not report.missing_regular_minutes
    assert [item.clock for item in report.bars][:2] == ["09:15", "09:16"]


def test_duplicate_bars_are_rejected_not_deduplicated():
    rows = _session()
    rows.insert(1, dict(rows[1]))
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "REJECT" and "DUPLICATE_BAR_TIMESTAMP" in report.issues
    assert report.bars == ()


def test_conflicting_duplicate_bars_are_rejected():
    rows = _session()
    rows.insert(1, {**rows[1], "intc": "101.50", "inth": "200.00"})
    assert validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY).decision == "REJECT"


def test_ascending_provider_order_is_a_contract_change():
    report = validate_segment(_gz(list(reversed(_session()))), "RELIANCE-EQ", DAY, POLICY)
    assert report.issues == ["RECORDS_NOT_STRICTLY_DESCENDING"]


def test_bar_from_another_date_is_rejected():
    rows = _session()
    rows.insert(0, _record("2026-03-03", "09:15", total=999))
    assert "BAR_OUTSIDE_SEGMENT_DATE" in validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY).issues


def test_one_bad_record_rejects_the_segment_and_is_named():
    rows = _session()
    rows[3] = {**rows[3], "inth": "1.00"}
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "REJECT" and report.issues[0].startswith("INVALID_RECORD[3]")


def test_missing_minutes_are_reported_and_never_filled():
    rows = [item for item in _session() if not item["time"].endswith("09:20:00")]
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "REVIEW" and report.missing_regular_minutes == ("09:20",)
    assert len(report.bars) == 29


def test_off_session_bar_with_cumulative_volume_is_flagged_and_kept():
    rows = _session()
    rows.append(_record(DAY, "09:14", volume=0, total=5))
    # Cumulative volume then rises by intv alone, as observed in the real snapshot.
    for offset, item in enumerate(reversed(rows[:-1])):
        item["v"] = str(5 + 10 * (offset + 1))
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "REVIEW" and report.off_session_bars == 1 and report.regular_bars == 30
    assert report.bars[0].flags == ("OFF_SESSION", "ZERO_VOLUME")
    assert "CUMULATIVE_VOLUME_BREAK" not in report.flag_counts


def test_volume_anomalies_are_flagged_unchanged():
    rows = _session()
    rows[0] = {**rows[0], "intv": "0", "v": rows[1]["v"]}
    rows[10] = {**rows[10], "intv": "5000"}
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.flag_counts["ZERO_VOLUME"] == 1 and report.flag_counts["VOLUME_SPIKE"] == 1
    assert report.flag_counts["CUMULATIVE_VOLUME_BREAK"] >= 1
    assert any(item.volume == 5000 for item in report.bars)


def test_negative_interval_volume_quarantines_without_repair():
    rows = _session()
    rows[5] = {**rows[5], "intv": "-200"}
    report = validate_segment(_gz(rows), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "QUARANTINE"
    assert "NEGATIVE_INTERVAL_VOLUME_MEANING_UNKNOWN" in report.issues
    assert any(item.volume == -200 and "NEGATIVE_VOLUME" in item.flags for item in report.bars)
    with pytest.raises(ValueError, match="quarantined"):
        fuse_session(report, {}, AXIS, POLICY)


def test_session_after_policy_validity_is_quarantined_even_when_clean():
    limited = SessionPolicy("09:15", "09:44", "OBSERVED_NOT_AUTHORITATIVE", "fixture", "2026-03-01")
    report = validate_segment(_gz(_session()), "RELIANCE-EQ", DAY, limited)
    assert report.decision == "QUARANTINE"
    assert report.issues == ["SESSION_POLICY_NOT_ESTABLISHED_FOR_DATE"]


def test_oversized_decompression_is_refused():
    report = validate_segment(gzip.compress(b" " * (16 * 1024 * 1024 + 1)), "RELIANCE-EQ", DAY, POLICY)
    assert report.decision == "REJECT" and "exceeds" in report.issues[0]


def test_session_policy_requires_provenance_and_valid_order():
    with pytest.raises(ValueError, match="provenance"):
        SessionPolicy("09:15", "15:29", "AUTHORITATIVE", " ")
    with pytest.raises(ValueError, match="ascending"):
        SessionPolicy("15:29", "09:15", "AUTHORITATIVE", "x")


def test_shipped_policy_is_not_authoritative_and_stops_before_the_regime_change():
    policy = SessionPolicy.load(Path(__file__).parents[1] / "config" / "m3" / "minute-session-policy.json")
    assert policy.status == "OBSERVED_NOT_AUTHORITATIVE" and policy.valid_through == "2026-08-02"
    assert len(policy.regular_minutes) == 375


# --- publication and import -------------------------------------------------------------------


def test_publication_detects_payload_marker_and_identity_mismatch():
    parts = _triplet("RELIANCE-EQ", DAY, _session())
    verify_publication(parts[".json.gz"], parts[".meta.json"], parts[".json.gz.done"], "RELIANCE-EQ", DAY)
    with pytest.raises(MinuteSchemaError, match="marker"):
        verify_publication(_gz([]), parts[".meta.json"], parts[".json.gz.done"], "RELIANCE-EQ", DAY)
    with pytest.raises(MinuteSchemaError, match="another symbol"):
        verify_publication(parts[".json.gz"], parts[".meta.json"], parts[".json.gz.done"], "TCS-EQ", DAY)


def test_import_publishes_read_only_files_and_refuses_a_second_audit(tmp_path):
    triplets = {("RELIANCE-EQ", DAY): _triplet("RELIANCE-EQ", DAY, _session())}
    audit, raw = _import(tmp_path, triplets)
    payload = raw / "RELIANCE-EQ" / f"{DAY}.json.gz"
    assert payload.read_bytes() == triplets[("RELIANCE-EQ", DAY)][".json.gz"]
    assert stat.S_IMODE(payload.stat().st_mode) == 0o444
    assert json.loads((audit / "import-audit.json").read_text())["files_published"] == 3
    with pytest.raises(FileExistsError, match="audit"):
        _import(tmp_path, triplets)
    assert not [item for item in (raw / "RELIANCE-EQ").iterdir() if item.name.startswith(".")]


def test_import_rejects_a_changed_archive_and_source_mismatch(tmp_path):
    triplets = {("RELIANCE-EQ", DAY): _triplet("RELIANCE-EQ", DAY, _session())}
    archive, digest, hashes = _snapshot(tmp_path, triplets)
    with pytest.raises(ValueError, match="supplied SHA-256"):
        _import(tmp_path, triplets, archive_sha256="0" * 64)
    hashes.write_text(hashes.read_text().replace(hashes.read_text()[:64], "0" * 64, 1))
    with pytest.raises(ValueError, match="source hash"):
        import_snapshot(
            archive=archive, archive_sha256=digest, source_hashes=hashes, master=_master(tmp_path / "m.zip"),
            raw_root=tmp_path / "minute-raw", audit_root=tmp_path / "audit", source_label="synthetic",
        )
    assert not (tmp_path / "minute-raw").exists()


def test_import_reports_a_bad_triplet_without_publishing_it(tmp_path):
    good = _triplet("RELIANCE-EQ", DAY, _session())
    bad = _triplet("RELIANCE-EQ", "2026-03-03", _session("2026-03-03"))
    bad[".json.gz"] = _gz(_session("2026-03-03", 3))
    wrong_token = _triplet("TCS-EQ", DAY, _session(), token="999")
    audit, raw = _import(tmp_path, {
        ("RELIANCE-EQ", DAY): good, ("RELIANCE-EQ", "2026-03-03"): bad, ("TCS-EQ", DAY): wrong_token,
    })
    result = json.loads((audit / "import-audit.json").read_text())
    assert {item["session_date"] for item in result["exceptions"]} == {DAY, "2026-03-03"}
    assert not (raw / "RELIANCE-EQ" / "2026-03-03.json.gz").exists() and not (raw / "TCS-EQ").exists()


def test_import_stops_on_a_differing_destination_before_writing_anything(tmp_path):
    other = "2026-03-03"
    triplets = {
        ("RELIANCE-EQ", DAY): _triplet("RELIANCE-EQ", DAY, _session()),
        ("RELIANCE-EQ", other): _triplet("RELIANCE-EQ", other, _session(other)),
    }
    existing = tmp_path / "minute-raw" / "RELIANCE-EQ" / f"{other}.meta.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"{}")
    with pytest.raises(FileExistsError, match="differs"):
        _import(tmp_path, triplets)
    assert existing.read_bytes() == b"{}"
    assert sorted(item.name for item in existing.parent.iterdir()) == [existing.name]


def test_import_refuses_the_daily_tree_and_output_inside_raw(tmp_path):
    triplets = {("RELIANCE-EQ", DAY): _triplet("RELIANCE-EQ", DAY, _session())}
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / ".stock-gplm-m24.lock").touch()
    with pytest.raises(ValueError, match="separate"):
        _import(tmp_path, triplets, raw_root=tmp_path / "daily")
    with pytest.raises(ValueError, match="outside"):
        _import(tmp_path, triplets, audit_root=tmp_path / "minute-raw" / "audit")


@pytest.mark.parametrize("kind", ["symlink", "traversal", "compressed"])
def test_snapshot_reader_refuses_unsafe_archives(tmp_path, kind):
    archive = tmp_path / "bad.tar"
    if kind == "compressed":
        with tarfile.open(archive, "w:gz"):
            pass
        with pytest.raises(tarfile.ReadError):
            read_snapshot(archive)
        return
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as stream:
        if kind == "symlink":
            info = tarfile.TarInfo(f"NSE/RELIANCE-EQ/{DAY}.json.gz")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            stream.addfile(info)
        else:
            source = tmp_path / "x"
            source.write_bytes(b"x")
            stream.add(source, "NSE/../../evil.json.gz")
    with pytest.raises(ValueError, match="regular file|unexpected"):
        read_snapshot(archive)


# --- fusion -----------------------------------------------------------------------------------


def _snapshot_row(day: str, close: str = "90.00", *, sma: str | None = "80.00") -> DailySnapshot:
    features = {name: None for name in FEATURE_NAMES}
    features["sma_20"] = sma
    features["ema_20"] = sma
    return DailySnapshot(day, Decimal(close), "a" * 64, features)


def _report(day: str = DAY, rows: list[dict[str, str]] | None = None) -> SegmentReport:
    return validate_segment(_gz(rows if rows is not None else _session(day)), "RELIANCE-EQ", day, POLICY)


AXIS = ("2026-02-26", "2026-02-27", DAY, "2026-03-03")


def test_same_day_and_future_daily_values_cannot_reach_a_session():
    poison = {
        "2026-02-27": _snapshot_row("2026-02-27", "90.00"),
        DAY: _snapshot_row(DAY, "99999.00", sma="99999.00"),
        "2026-03-03": _snapshot_row("2026-03-03", "88888.00", sma="88888.00"),
    }
    rows = fuse_session(_report(), poison, AXIS, POLICY)
    clean = fuse_session(_report(), {"2026-02-27": poison["2026-02-27"]}, AXIS, POLICY)
    assert rows == clean
    assert {item["daily_as_of_date"] for item in rows} == {"2026-02-27"}
    assert rows[0]["close_to_prior_daily_close"] == "0.111111"


def test_first_morning_bar_uses_prior_close_only():
    row = fuse_session(_report(), {"2026-02-27": _snapshot_row("2026-02-27")}, AXIS, POLICY)[0]
    assert row["bar_label_utc"] == "2026-03-02T03:45:00Z"
    assert row["feature_available_utc"] == "2026-03-02T03:46:00Z"
    assert row["daily_available_utc"] == "2026-02-27T18:30:00Z"
    assert row["gap_from_prior_daily_close"] == "0.111111"


def test_missing_prior_candle_blanks_daily_values_instead_of_bridging():
    daily = {"2026-02-26": _snapshot_row("2026-02-26")}
    rows = fuse_session(_report(), daily, AXIS, POLICY)
    assert rows[0]["daily_status"] == "PRIOR_SESSION_CANDLE_MISSING"
    assert rows[0]["daily_as_of_date"] is None
    assert all(rows[0][name] is None for name in DAILY_COLUMNS)
    assert rows[0]["gap_from_prior_daily_close"] is None


def test_session_before_the_axis_has_no_prior_daily():
    assert select_daily({}, AXIS, "2026-01-01") == ("NO_PRIOR_SESSION", None)


def test_session_after_the_axis_uses_the_last_prior_candle():
    day = "2026-03-04"
    daily = {"2026-03-03": _snapshot_row("2026-03-03")}
    status, snapshot = select_daily(daily, AXIS, day)
    assert status == "PRIOR_SESSION" and snapshot is daily["2026-03-03"]


def test_off_session_bar_never_becomes_a_feature_row_or_session_open():
    rows = _session()
    rows.append(_record(DAY, "09:14", price="50.00", volume=0, total=5))
    for offset, item in enumerate(reversed(rows[:-1])):
        item["v"] = str(5 + 10 * (offset + 1))
    fused = fuse_session(_report(rows=rows), {}, AXIS, POLICY)
    assert len(fused) == 30 and fused[0]["bar_label_ist"].endswith("09:15:00+05:30")
    assert fused[0]["return_from_session_open"] == "0.000000"


def test_missing_minute_blanks_only_windows_that_cross_it():
    rows = [item for item in _session() if not item["time"].endswith("09:20:00")]
    fused = {item["minute_of_session"]: item for item in fuse_session(_report(rows=rows), {}, AXIS, POLICY)}
    assert "5" not in fused
    assert fused["6"]["return_1m"] is None
    assert fused["7"]["return_1m"] is not None and fused["7"]["return_5m"] is None
    assert fused["11"]["return_5m"] is not None
    assert fused["6"]["session_volume"] is None and fused["6"]["session_vwap_typical"] is None
    assert fused["4"]["session_volume"] == "50"


def test_rejected_segment_cannot_be_fused():
    with pytest.raises(ValueError, match="rejected"):
        fuse_session(SegmentReport("RELIANCE-EQ", DAY, "REJECT"), {}, AXIS, POLICY)


def test_causal_guard_catches_same_day_and_early_availability():
    row = fuse_session(_report(), {"2026-02-27": _snapshot_row("2026-02-27")}, AXIS, POLICY)[0]
    with pytest.raises(LeakageError, match="not before"):
        assert_causal({**row, "daily_as_of_date": DAY})
    with pytest.raises(LeakageError, match="not available"):
        assert_causal({**row, "daily_as_of_date": "2026-03-01", "feature_available_utc": "2026-02-28T00:00:00Z"})
    with pytest.raises(LeakageError, match="carries daily"):
        assert_causal({**row, "daily_as_of_date": None, DAILY_COLUMNS[0]: "1"})


def test_causality_audit_passes_and_catches_a_future_reading_selector(monkeypatch):
    daily = {"2026-02-27": _snapshot_row("2026-02-27"), DAY: _snapshot_row(DAY, "5.00")}
    assert causality_audit(_report(), daily, AXIS, POLICY, prefix_step=10) == 4
    import stock_gplm.m3.fusion as fusion
    monkeypatch.setattr(fusion, "_prior_session", lambda axis, session: session if session in axis else None)
    with pytest.raises(LeakageError):
        causality_audit(_report(), daily, AXIS, POLICY)


# --- end to end -------------------------------------------------------------------------------


def _daily_publication(root: Path, symbol: str, days: list[str]) -> None:
    records = []
    for offset, day in enumerate(days):
        instant = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        label = date.fromisoformat(day).strftime("%d-%b-%Y").upper()
        price = 100 + offset
        records.append(json.dumps({
            "time": label, "into": f"{price}.00", "inth": f"{price + 40}.00", "intl": f"{price - 1}.00",
            "intc": f"{price}.00", "intv": "1000.00", "ssboe": str(instant),
        }))
    start, end = days[0], days[-1]
    target = root / symbol
    target.mkdir(parents=True, exist_ok=True)
    stem = f"{start}_{end}"
    raw = gzip.compress(json.dumps(records).encode(), mtime=0)
    digest = hashlib.sha256(raw).hexdigest()
    (target / f"{stem}.json.gz").write_bytes(raw)
    (target / f"{stem}.meta.json").write_text(json.dumps({
        "source": "shoonya", "symbol": symbol, "from": start, "to": end, "quality_status": "UNVALIDATED_RAW",
        "sha256_compressed": digest, "records": len(records), "fetched_at_utc": "2026-10-30T00:00:00+00:00",
    }))
    (target / f"{stem}.json.gz.done").write_text(json.dumps({
        "status": "COMPLETE", "file": f"{stem}.json.gz", "bytes": len(raw), "records": len(records),
        "sha256": digest, "quality_status": "UNVALIDATED_RAW", "published_at_utc": "2026-10-30T00:00:00+00:00",
    }))


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    days = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(60)]
    days = [item for item in days if date.fromisoformat(item).weekday() < 5]
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    _daily_publication(daily_root, "RELIANCE-EQ", days)
    _daily_publication(daily_root, "TCS-EQ", days)
    master = _master(tmp_path / "master.zip")
    readiness = build_readiness_run(raw_root=daily_root, master=master, output_root=tmp_path / "m31")
    research = build_research_run(readiness_run=readiness, output_root=tmp_path / "m32")
    session_days = days[-3:]
    triplets = {
        ("RELIANCE-EQ", item): _triplet("RELIANCE-EQ", item, _session(item, base=130 + index))
        for index, item in enumerate(session_days)
    }
    audit, raw = _import(tmp_path, triplets, master=master)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1, "exchange": "NSE", "timezone": "Asia/Kolkata", "regular_first_bar": "09:15",
        "regular_last_bar": "09:44", "status": "OBSERVED_NOT_AUTHORITATIVE", "provenance": "synthetic",
        "valid_through": None,
    }))
    return {
        "minute_root": raw, "daily_root": daily_root, "master": master, "policy_path": policy,
        "readiness_run": readiness, "research_run": research, "import_audit": audit, "output_root": tmp_path / "m34",
    }


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_end_to_end_run_is_causal_reproducible_and_never_ready(world, tmp_path):
    before = {path: path.read_bytes() for path in world["minute_root"].rglob("*.json*")}
    run = build_minute_run(**world, source_label="synthetic")
    gates = json.loads((run / "quality-gates.json").read_text())
    assert gates["training_readiness"] == "NOT_READY" and gates["model_training"] == "NOT_PERFORMED"
    assert gates["intraday_horizon"] == "BLOCKED"
    assert gates["gates"]["minute_publication"]["status"] == "PASS"
    assert gates["gates"]["minute_universe_coverage"]["status"] == "BLOCKED"
    assert gates["gates"]["minute_session_policy"]["status"] == "BLOCKED"
    assert gates["gates"]["daily_raw_candle_acceptance"]["status"] in {"PASS", "BLOCKED"}
    confirmation = json.loads((run / "daily-confirmation.json").read_text())
    assert confirmation["status"] == "CONFIRMED_PUBLISHED_LOCALLY" and confirmation["re_extraction_performed"] is False
    fused = _rows(run / "fused-features.csv")
    assert len(fused) == 90
    assert all(item["daily_as_of_date"] < item["session_date"] for item in fused)
    audit = json.loads((run / "leakage-audit.json").read_text())
    assert audit["status"] == "PASS" and audit["daily_truncation_checks"] == 3
    assert before == {path: path.read_bytes() for path in world["minute_root"].rglob("*.json*")}
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["raw_inputs_mutated"] is False
    for name, digest in manifest["outputs"].items():
        assert hashlib.sha256((run / name).read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError, match="already exists"):
        build_minute_run(**world, source_label="synthetic")
    second = build_minute_run(**{**world, "output_root": tmp_path / "other"}, source_label="synthetic")
    assert second.name == run.name
    for name in manifest["outputs"]:
        assert (second / name).read_bytes() == (run / name).read_bytes()


def test_reconciliation_and_coverage_report_gaps_without_repair(world):
    run = build_minute_run(**world, source_label="synthetic")
    reconciliation = _rows(run / "daily-reconciliation.csv")
    assert len(reconciliation) == 3 and all("HIGH" in item["findings"] for item in reconciliation)
    assert all("VOLUME_GAP_OVER_1_PERCENT" in item["findings"] for item in reconciliation)
    coverage = {item["symbol"]: item for item in _rows(run / "symbol-coverage.csv")}
    assert coverage["TCS-EQ"]["minute_sessions"] == "0"
    assert coverage["TCS-EQ"]["coverage_reason"] == "PARTIAL_OR_ABSENT_REASON_UNKNOWN"
    assert coverage["RELIANCE-EQ"]["minute_sessions"] == "3"


def test_changed_inputs_and_outputs_inside_raw_are_refused(world, tmp_path):
    with pytest.raises(ValueError, match="outside"):
        build_minute_run(**{**world, "output_root": world["minute_root"] / "x"}, source_label="s")
    with pytest.raises(ValueError, match="separate"):
        build_minute_run(**{**world, "minute_root": world["daily_root"]}, source_label="s")
    features = world["research_run"] / "features.csv"
    os.chmod(features, 0o644)
    features.write_text("tampered\n")
    with pytest.raises(ValueError, match="manifest"):
        build_minute_run(**world, source_label="s")


def test_tampered_daily_tree_is_not_confirmed(world):
    payload = next(world["daily_root"].rglob("*.meta.json"))
    payload.write_text(payload.read_text() + " ")
    run = build_minute_run(**world, source_label="s")
    confirmation = json.loads((run / "daily-confirmation.json").read_text())
    assert confirmation["status"] == "NOT_CONFIRMED" and confirmation["mismatched_files"]


def test_quarantined_segment_is_reported_reconciled_and_not_fused(world):
    day = next(iter(sorted(item.name[:10] for item in (world["minute_root"] / "RELIANCE-EQ").glob("*.json.gz"))))
    rows = _session(day, base=130)
    rows[5] = {**rows[5], "intv": "-200"}
    target = world["minute_root"] / "RELIANCE-EQ"
    for suffix, data in _triplet("RELIANCE-EQ", day, rows).items():
        (target / f"{day}{suffix}").unlink()
        (target / f"{day}{suffix}").write_bytes(data)
    run = build_minute_run(**world, source_label="s")
    assert all(item["session_date"] != day for item in _rows(run / "fused-features.csv"))
    assert any(item["session_date"] == day for item in _rows(run / "minute-bars.csv"))
    row = next(item for item in _rows(run / "daily-reconciliation.csv") if item["session_date"] == day)
    assert "VOLUME_NOT_COMPARABLE_NEGATIVE_BARS" in row["findings"] and row["minute_volume"] == ""
    quality = json.loads((run / "coverage-quality.json").read_text())
    assert quality["symbols"]["RELIANCE-EQ"]["quarantined_sessions"] == [day]
    gates = json.loads((run / "quality-gates.json").read_text())["gates"]
    assert gates["minute_schema"]["status"] == "PASS" and gates["minute_content"]["status"] == "BLOCKED"


def test_rejected_minute_segment_is_visible_and_excluded(world, tmp_path):
    day = "2026-02-27"
    rows = _session(day, base=130)
    rows.insert(1, dict(rows[1]))
    target = world["minute_root"] / "RELIANCE-EQ"
    for suffix, data in _triplet("RELIANCE-EQ", day, rows).items():
        (target / f"{day}{suffix}").unlink(missing_ok=True)
        (target / f"{day}{suffix}").write_bytes(data)
    run = build_minute_run(**world, source_label="s")
    segment = next(item for item in _rows(run / "minute-segments.csv") if item["session_date"] == day)
    assert segment["content_decision"] == "REJECT" and "DUPLICATE" in segment["issues"]
    assert all(item["session_date"] != day for item in _rows(run / "fused-features.csv"))
    gates = json.loads((run / "quality-gates.json").read_text())["gates"]
    assert gates["minute_schema"]["status"] == "BLOCKED"
