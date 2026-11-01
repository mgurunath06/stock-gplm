from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stock_gplm import acceptance


FIXED_TIME = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_acceptance_reports_accept_and_preserves_triplet(publish, row):
    source = publish([row])
    before = {path: path.read_bytes() for path in source.parent.iterdir()}
    report = acceptance.run_acceptance(source, evaluated_at=FIXED_TIME)

    assert report.decision == "ACCEPT"
    assert report.source_unchanged_during_run is True
    assert len(report.triplet_before) == 3
    assert all(item.exists and item.sha256 and item.bytes for item in report.triplet_before)
    assert report.triplet_before == report.triplet_after
    assert report.validation["publication_integrity_valid"] is True
    assert report.validation["content_quality_valid"] is True
    assert {path: path.read_bytes() for path in source.parent.iterdir()} == before


def test_human_summary_contains_decision_and_checks(publish, row):
    report = acceptance.run_acceptance(publish([row]), evaluated_at=FIXED_TIME)
    summary = acceptance.render_summary(report)
    assert "Decision: ACCEPT" in summary
    assert "Publication integrity: PASS" in summary
    assert "Content quality: PASS" in summary
    assert "Schema: ShoonyaDailyV1" in summary
    assert "Records: 1" in summary


def test_bad_content_is_quarantined_with_reason(publish, row):
    report = acceptance.run_acceptance(
        publish([dict(row, intc="9999.00")]), evaluated_at=FIXED_TIME
    )
    assert report.decision == "QUARANTINE"
    assert any("OHLC bounds" in reason for reason in report.reasons)


def test_unknown_schema_is_quarantined_with_reason(publish, row):
    report = acceptance.run_acceptance(
        publish([dict(row, stat="Ok")]), evaluated_at=FIXED_TIME
    )
    assert report.decision == "QUARANTINE"
    assert any("unsupported_schema" in reason for reason in report.reasons)


def test_publication_failure_is_rejected_with_reason(publish, row):
    report = acceptance.run_acceptance(
        publish([row], manifest_changes={"sha256": "0" * 64}), evaluated_at=FIXED_TIME
    )
    assert report.decision == "REJECT"
    assert any("manifest.sha256" in reason for reason in report.reasons)


def test_missing_triplet_is_rejected(publish, row):
    source = publish([row])
    source.with_name("2026-10-01_2026-10-07.meta.json").unlink()
    report = acceptance.run_acceptance(source, evaluated_at=FIXED_TIME)
    assert report.decision == "REJECT"
    assert report.source_unchanged_during_run is None
    assert any(not item.exists for item in report.triplet_before)
    assert report.reasons[0].startswith("source_triplet_incomplete")


def test_source_change_during_acceptance_forces_reject(publish, row, monkeypatch):
    source = publish([row])
    real_validate = acceptance.validate_daily

    def validate_then_change(path):
        result = real_validate(path)
        Path(f"{path}.done").write_text(Path(f"{path}.done").read_text() + " ")
        return result

    monkeypatch.setattr(acceptance, "validate_daily", validate_then_change)
    report = acceptance.run_acceptance(source, evaluated_at=FIXED_TIME)
    assert report.decision == "REJECT"
    assert report.source_unchanged_during_run is False
    assert report.reasons[0].startswith("source_changed_during_acceptance")


def test_artifacts_are_machine_and_human_readable(publish, row, tmp_path):
    source = publish([row])
    report = acceptance.run_acceptance(source, evaluated_at=FIXED_TIME)
    json_path, summary_path = acceptance.write_acceptance_artifacts(
        report, tmp_path / "reports/acceptance"
    )
    payload = json.loads(json_path.read_text())
    assert payload["decision"] == "ACCEPT"
    assert payload["triplet_before"][0]["sha256"]
    assert summary_path.read_text().startswith("Decision: ACCEPT\n")


def test_artifacts_refuse_raw_directory_or_overwrite(publish, row, tmp_path):
    source = publish([row])
    report = acceptance.run_acceptance(source, evaluated_at=FIXED_TIME)
    with pytest.raises(ValueError, match="outside"):
        acceptance.write_acceptance_artifacts(report, source.parent)
    output = tmp_path / "reports"
    acceptance.write_acceptance_artifacts(report, output)
    with pytest.raises(FileExistsError, match="refusing"):
        acceptance.write_acceptance_artifacts(report, output)


def _run_cli(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_gplm.acceptance_cli",
            str(source),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_acceptance_cli_writes_both_artifacts(publish, row, tmp_path):
    result = _run_cli(publish([row]), tmp_path / "acceptance")
    assert result.returncode == 0
    assert "Decision: ACCEPT" in result.stdout
    files = sorted(path.name for path in (tmp_path / "acceptance").iterdir())
    assert files == [
        "RELIANCE-EQ_2026-10-01_2026-10-07.acceptance.json",
        "RELIANCE-EQ_2026-10-01_2026-10-07.acceptance.txt",
    ]


@pytest.mark.parametrize("kind,exit_code,decision", [
    ("quarantine", 1, "QUARANTINE"),
    ("reject", 2, "REJECT"),
])
def test_acceptance_cli_exit_codes(publish, row, tmp_path, kind, exit_code, decision):
    source = (
        publish([dict(row, intc="9999.00")])
        if kind == "quarantine"
        else publish([row], manifest_changes={"status": "IN_PROGRESS"})
    )
    result = _run_cli(source, tmp_path / f"reports-{kind}")
    assert result.returncode == exit_code
    assert f"Decision: {decision}" in result.stdout


def test_acceptance_cli_refuses_reports_inside_raw_directory(publish, row):
    source = publish([row])
    result = _run_cli(source, source.parent / "reports")
    assert result.returncode == 3
    assert "outside the raw segment directory" in result.stderr
