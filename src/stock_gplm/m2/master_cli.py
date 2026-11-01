"""Validate one local Shoonya NSE instrument-master ZIP without modifying it."""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

from .master_validation import validate_shoonya_nse_master_v1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        source = args.source.resolve()
        output_dir = args.output_dir.resolve()
        if output_dir == source.parent or source.parent in output_dir.parents:
            raise ValueError("output directory must be outside the immutable source directory")
        report = validate_shoonya_nse_master_v1(source)
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "NSE_symbols.validation.json"
        summary_path = output_dir / "NSE_symbols.validation.txt"
        if report_path.exists() or summary_path.exists():
            raise FileExistsError("M2.2 validation report exists; refusing to overwrite")
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        counts = report["counts"]
        quality = report["identifier_quality"]
        summary = (
            f"Decision: {report['decision']}\n"
            f"SHA-256: {report['source_integrity']['sha256']}\n"
            f"Rows: {counts['records']}\n"
            f"EQ instruments: {counts['eq_instruments']}\n"
            f"Unique EQ symbols: {counts['unique_eq_symbols']}\n"
            f"Unique EQ trading symbols: {counts['unique_eq_trading_symbols']}\n"
            f"Unique EQ tokens: {counts['unique_eq_tokens']}\n"
            f"Available EQ ISINs: {counts['available_eq_isins']}\n"
            f"Unique all-series base symbols: {counts['unique_all_base_symbols']}\n"
            f"Unique all-series trading symbols: {counts['unique_all_trading_symbols']}\n"
            f"Unique all-series tokens: {counts['unique_all_tokens']}\n"
            f"Invalid identifiers: {quality['invalid_count']}\n"
            f"Duplicate tokens: {len(quality['duplicate_tokens'])}\n"
            f"Duplicate trading symbols: {len(quality['duplicate_trading_symbols'])}\n"
            f"Duplicate EQ base symbols: {len(quality['duplicate_eq_symbols'])}\n"
            "Historical membership: NOT_ESTABLISHED\n"
            "Reference readiness: INCOMPLETE\n"
        )
        summary_path.write_text(summary, encoding="utf-8")
    except (FileExistsError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"M2.2 master validation error: {exc}", file=sys.stderr)
        return 3
    print(summary, end="")
    print(f"Machine report: {report_path}")
    print(f"Summary: {summary_path}")
    return 0 if report["decision"] == "ACCEPT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
