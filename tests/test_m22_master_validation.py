from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from stock_gplm.m2 import validate_shoonya_nse_master_v1


HEADER = "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n"


def _archive(path: Path, rows: list[str], member: str = "NSE_symbols.txt") -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, HEADER + "".join(rows))
    return path


def test_observed_v1_schema_and_eq_counts_are_accepted(tmp_path):
    source = _archive(tmp_path / "NSE_symbols.txt.zip", [
        "NSE,1,1,ALPHA,ALPHA-EQ,EQ,0.05,\n",
        "NSE,2,1,BETA,BETA-EQ,EQ,0.1,\n",
        "NSE,3,1,Nifty 50,NIFTY INDEX,INDEX,0,\n",
    ])
    before = source.read_bytes()
    report = validate_shoonya_nse_master_v1(
        source, observed_at_utc="2026-10-31T21:00:00Z"
    )
    assert report["decision"] == "ACCEPT"
    assert report["counts"] == {
        "records": 3,
        "eq_instruments": 2,
        "unique_eq_symbols": 2,
        "unique_eq_trading_symbols": 2,
        "unique_eq_tokens": 2,
        "available_eq_isins": 0,
        "unique_all_base_symbols": 3,
        "unique_all_trading_symbols": 3,
        "unique_all_tokens": 3,
        "available_all_isins": 0,
    }
    assert report["observed_schema"]["isin_column_present"] is False
    assert report["historical_membership"]["decision"] == "NOT_ESTABLISHED"
    assert source.read_bytes() == before


def test_duplicate_token_and_trading_symbol_reject_master(tmp_path):
    source = _archive(tmp_path / "NSE_symbols.txt.zip", [
        "NSE,1,1,ALPHA,ALPHA-EQ,EQ,0.05,\n",
        "NSE,1,1,BETA,ALPHA-EQ,EQ,0.05,\n",
    ])
    report = validate_shoonya_nse_master_v1(source)
    assert report["decision"] == "REJECT"
    assert report["identifier_quality"]["duplicate_tokens"][0]["value"] == "1"
    assert report["identifier_quality"]["duplicate_trading_symbols"][0]["value"] == "ALPHA-EQ"
    assert any(
        item["code"] == "INVALID_EQ_TRADING_SYMBOL"
        for item in report["identifier_quality"]["invalid_identifiers"]
    )


def test_unknown_header_is_rejected_instead_of_inferred(tmp_path):
    source = tmp_path / "NSE_symbols.txt.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("NSE_symbols.txt", "Exchange,Token,TradingSymbol\nNSE,1,A-EQ\n")
    with pytest.raises(ValueError, match="unsupported instrument-master header"):
        validate_shoonya_nse_master_v1(source)


def test_master_cli_writes_reports_outside_source(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = _archive(source_dir / "NSE_symbols.txt.zip", [
        "NSE,1,1,ALPHA,ALPHA-EQ,EQ,0.05,\n",
    ])
    output = tmp_path / "reports"
    result = subprocess.run([
        sys.executable, "-m", "stock_gplm.m2.master_cli", str(source),
        "--output-dir", str(output),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert (output / "NSE_symbols.validation.json").exists()
    assert "Historical membership: NOT_ESTABLISHED" in (
        output / "NSE_symbols.validation.txt"
    ).read_text()


def test_real_master_regression_if_present():
    source = Path("data/reference/source/shoonya/NSE_symbols.txt.zip")
    if not source.exists():
        pytest.skip("local real Shoonya NSE instrument master is not present")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = validate_shoonya_nse_master_v1(source)
    assert before == "129b0eb840d854bceb181da6f0dca67fc5f25a355f6f1325677b95f005f4aee9"
    assert report["decision"] == "ACCEPT"
    assert report["counts"]["records"] == 10029
    assert report["counts"]["eq_instruments"] == 2715
    assert report["counts"]["unique_eq_symbols"] == 2715
    assert report["counts"]["unique_eq_tokens"] == 2715
    assert report["counts"]["available_eq_isins"] == 0
    assert report["counts"]["unique_all_base_symbols"] == 10013
    assert report["counts"]["unique_all_trading_symbols"] == 10029
    assert report["counts"]["unique_all_tokens"] == 10029
    assert report["identifier_quality"]["invalid_count"] == 0
    assert len(report["identifier_quality"]["nonunique_base_symbols_across_series"]) == 15
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
