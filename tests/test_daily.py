from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stock_gplm.ingestion import ShoonyaDailyV1
from stock_gplm.ingestion import daily


def _row_for_day(base: dict[str, str], day: int) -> dict[str, str]:
    epoch = int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp())
    return dict(base, time=f"{day:02d}-OCT-2026", ssboe=str(epoch))


def test_sanitized_reliance_regression_record_is_accepted(publish, row):
    path = publish([row])
    before = {item: item.read_bytes() for item in path.parent.iterdir()}
    report = daily.validate_daily(path)
    candle = report.content_quality.normalized_candles[0]

    assert report.publication_integrity.valid
    assert report.publication_integrity.metadata_quality_status == "UNVALIDATED_RAW"
    assert report.content_quality.valid
    assert report.content_quality.schema == "ShoonyaDailyV1"
    assert report.content_quality.schema_confirmed_from_real_sample
    assert report.content_quality.timestamp_policy == (
        "ssboe_posix_seconds_utc_date_equals_trading_date"
    )
    assert candle.trading_date == "2026-10-07"
    assert candle.source_timestamp_utc == "2026-10-07T00:00:00Z"
    assert candle.open == Decimal("1212.00")
    assert candle.high == Decimal("1220.80")
    assert candle.low == Decimal("1203.50")
    assert candle.close == Decimal("1207.70")
    assert candle.volume == 11880731
    assert report.ready_for_normalization
    assert report.disposition == "ACCEPT"
    assert {item: item.read_bytes() for item in path.parent.iterdir()} == before


def test_decimal_values_are_json_reported_as_exact_strings(publish, row):
    candle = daily.validate_daily(publish([row])).to_dict()["content_quality"][
        "normalized_candles"
    ][0]
    assert candle["open"] == "1212.00"
    assert candle["high"] == "1220.80"
    assert candle["volume"] == 11880731


def test_reverse_chronology_is_normalized_ascending(publish, row):
    rows = [_row_for_day(row, day) for day in (7, 6, 5)]
    report = daily.validate_daily(publish(rows))
    assert report.content_quality.valid
    assert report.content_quality.chronology == "descending"
    assert [item.trading_date for item in report.content_quality.normalized_candles] == [
        "2026-10-05", "2026-10-06", "2026-10-07"
    ]


def test_ascending_chronology_remains_ascending(publish, row):
    rows = [_row_for_day(row, day) for day in (5, 6, 7)]
    report = daily.validate_daily(publish(rows))
    assert report.content_quality.valid
    assert report.content_quality.chronology == "ascending"
    assert [item.trading_date for item in report.content_quality.normalized_candles] == [
        "2026-10-05", "2026-10-06", "2026-10-07"
    ]


def test_mixed_chronology_is_rejected(publish, row):
    report = daily.validate_daily(publish([_row_for_day(row, day) for day in (5, 7, 6)]))
    assert not report.content_quality.valid
    assert report.content_quality.chronology == "mixed"
    assert report.content_quality.normalized_candles == []
    assert report.disposition == "QUARANTINE_CONTENT"


def test_duplicate_date_is_rejected(publish, row):
    report = daily.validate_daily(publish([row, dict(row, intc="1208.00")]))
    assert not report.content_quality.valid
    assert any(issue.code == "duplicate_date" for issue in report.content_quality.issues)
    assert report.content_quality.normalized_candles == []


@pytest.mark.parametrize("sidecar", ["meta", "done"])
def test_missing_sidecar_rejects_publication(publish, row, sidecar):
    path = publish([row])
    target = (path.with_name("2026-10-01_2026-10-07.meta.json") if sidecar == "meta"
              else path.with_name(f"{path.name}.done"))
    target.unlink()
    report = daily.validate_daily(path)
    assert not report.publication_integrity.valid
    assert report.content_quality.status == "NOT_RUN"


@pytest.mark.parametrize("change", [
    {"status": "IN_PROGRESS"}, {"file": "other.json.gz"}, {"sha256": "0" * 64},
    {"bytes": 1}, {"records": 2}, {"bytes": True}, {"records": True},
])
def test_manifest_mismatch_rejects_publication(publish, row, change):
    report = daily.validate_daily(publish([row], manifest_changes=change))
    assert not report.publication_integrity.valid
    assert report.disposition == "REJECT_PUBLICATION"


@pytest.mark.parametrize("change", [
    {"records": 2}, {"records": True}, {"sha256_compressed": "0" * 64},
    {"quality_status": 123},
])
def test_present_metadata_integrity_fields_must_match(publish, row, change):
    assert not daily.validate_daily(
        publish([row], metadata_changes=change)
    ).publication_integrity.valid


def test_optional_metadata_integrity_fields_may_be_absent(publish, row):
    path = publish([row])
    meta = path.with_name("2026-10-01_2026-10-07.meta.json")
    value = json.loads(meta.read_text())
    value.pop("records")
    value.pop("sha256_compressed")
    meta.write_text(json.dumps(value))
    assert daily.validate_daily(path).publication_integrity.valid


def test_unvalidated_raw_does_not_imply_content_quality(publish, row):
    report = daily.validate_daily(publish([dict(row, intc="9999.00")]))
    assert report.publication_integrity.valid
    assert not report.content_quality.valid
    assert report.disposition == "QUARANTINE_CONTENT"


def test_compressed_tampering_is_detected(publish, row):
    path = publish([row])
    path.write_bytes(path.read_bytes() + b"x")
    assert not daily.validate_daily(path).publication_integrity.valid


@pytest.mark.parametrize("name", [
    "SYNTHETIC.json.gz", "2026-10-07_2026-10-01.json.gz",
    "2026-02-31_2026-10-07.json.gz",
])
def test_segment_filename_contract(publish, row, name):
    path = publish([row])
    renamed = path.with_name(name)
    path.rename(renamed)
    assert not daily.validate_daily(renamed).publication_integrity.valid


@pytest.mark.parametrize("payload", [b"[]", b"{}", b'"not a list"', b"{", b"[NaN]"])
def test_payload_must_be_nonempty_json_list(publish, row, payload):
    assert not daily.validate_daily(publish([row], payload=payload)).publication_integrity.valid


@pytest.mark.parametrize("row_change", [
    {"stat": "Ok"}, {"other": "value"}, {"ssboe": 1791331200},
])
def test_other_record_schemas_remain_quarantined(publish, row, row_change):
    changed = dict(row, **row_change)
    report = daily.validate_daily(publish([changed]))
    assert report.publication_integrity.valid
    assert not report.content_quality.schema_confirmed_from_real_sample
    assert report.content_quality.schema == "unrecognized"
    assert report.disposition == "QUARANTINE_UNVERIFIED_SCHEMA"


def test_dictionary_rows_are_not_mistaken_for_serialized_v1(publish, row):
    report = daily.validate_daily(publish([row], serialize_rows=False))
    assert report.publication_integrity.valid
    assert report.disposition == "QUARANTINE_UNVERIFIED_SCHEMA"


@pytest.mark.parametrize("field", sorted(ShoonyaDailyV1.fields))
def test_missing_v1_field_is_unrecognized_schema(publish, row, field):
    del row[field]
    report = daily.validate_daily(publish([row]))
    assert report.disposition == "QUARANTINE_UNVERIFIED_SCHEMA"
    assert field in report.content_quality.issues[0].message


@pytest.mark.parametrize("serialized", [
    "not-json", "__import__('os').system('false')", json.dumps([1, 2]),
    '{"time":"07-OCT-2026","time":"06-OCT-2026"}',
])
def test_unsafe_or_unknown_serialized_schema_is_quarantined(publish, serialized):
    report = daily.validate_daily(publish([serialized], serialize_rows=False))
    assert report.disposition == "QUARANTINE_UNVERIFIED_SCHEMA"


@pytest.mark.parametrize("field,value", [
    ("into", "NaN"), ("inth", "Infinity"), ("intl", "-1.00"),
    ("intc", "0.00"), ("into", " 1212.00"), ("into", "1e3"),
    ("intv", "-1.00"), ("intv", "1.50"), ("intv", "NaN"),
])
def test_invalid_numeric_strings_fail_v1_content(publish, row, field, value):
    report = daily.validate_daily(publish([dict(row, **{field: value})]))
    assert report.content_quality.schema_confirmed_from_real_sample
    assert not report.content_quality.valid
    assert report.disposition == "QUARANTINE_CONTENT"


@pytest.mark.parametrize("change", [
    {"inth": "1200.00"}, {"intl": "1213.00"}, {"intc": "1221.00"},
])
def test_invalid_ohlc_relationships_fail(publish, row, change):
    assert not daily.validate_daily(publish([dict(row, **change)])).content_quality.valid


def test_zero_volume_is_accepted_only_when_explicit(publish, row):
    report = daily.validate_daily(publish([dict(row, intv="0.00")]))
    assert report.content_quality.valid
    assert report.content_quality.normalized_candles[0].volume == 0


@pytest.mark.parametrize("value", [
    "07-oct-2026", "7-OCT-2026", "07-10-2026", "31-FEB-2026",
])
def test_time_must_match_confirmed_format(publish, row, value):
    assert not daily.validate_daily(publish([dict(row, time=value)])).content_quality.valid


def test_observed_timestamp_policy_uses_utc(publish, row):
    report = daily.validate_daily(publish([row]))
    candle = report.content_quality.normalized_candles[0]
    assert candle.source_timestamp_utc == "2026-10-07T00:00:00Z"


@pytest.mark.parametrize("value", ["1791244800", "-1", "1.5", "not-epoch"])
def test_timestamp_mismatch_or_invalid_epoch_fails(publish, row, value):
    report = daily.validate_daily(publish([dict(row, ssboe=value)]))
    assert report.content_quality.schema_confirmed_from_real_sample
    assert not report.content_quality.valid


def test_session_date_must_be_inside_segment_range(publish, row):
    outside = dict(row, time="30-SEP-2026", ssboe="1790726400")
    report = daily.validate_daily(publish([outside]))
    assert report.publication_integrity.valid
    assert not report.content_quality.valid
    assert "outside the filename segment range" in report.content_quality.issues[0].message


def test_truncated_gzip_rejected_even_with_updated_sidecars(publish, row):
    path = publish([row])
    raw = path.read_bytes()[:-5]
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    meta_path = path.with_name("2026-10-01_2026-10-07.meta.json")
    metadata = json.loads(meta_path.read_text())
    metadata["sha256_compressed"] = digest
    meta_path.write_text(json.dumps(metadata))
    done_path = path.with_name(f"{path.name}.done")
    manifest = json.loads(done_path.read_text())
    manifest.update(sha256=digest, bytes=len(raw))
    done_path.write_text(json.dumps(manifest))
    assert not daily.validate_daily(path).publication_integrity.valid


@pytest.mark.parametrize("limit", ["MAX_COMPRESSED", "MAX_EXPANDED", "MAX_RECORDS"])
def test_resource_limits(publish, row, monkeypatch, limit):
    path = publish([row])
    monkeypatch.setattr(daily, limit, 0)
    assert not daily.validate_daily(path).publication_integrity.valid


def test_report_states_missing_history_is_unassessed(publish, row):
    output = daily.validate_daily(publish([row])).to_dict()
    assert "absence is not a zero-volume candle" in output["missing_history"]
    assert "current instrument masters" in output["missing_history"]


def test_cli_accepts_valid_v1_and_serializes_decimals(publish, row):
    result = subprocess.run(
        [sys.executable, "-m", "stock_gplm.cli", str(publish([row]))],
        capture_output=True, text=True, check=False,
    )
    output = json.loads(result.stdout)["reports"][0]
    assert result.returncode == 0
    assert output["ready_for_normalization"]
    assert output["disposition"] == "ACCEPT"
    assert output["content_quality"]["normalized_candles"][0]["open"] == "1212.00"


def test_cli_rejects_missing_file(tmp_path: Path):
    path = tmp_path / "2026-10-01_2026-10-07.json.gz"
    result = subprocess.run(
        [sys.executable, "-m", "stock_gplm.cli", str(path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["reports"][0]["disposition"] == "REJECT_PUBLICATION"
