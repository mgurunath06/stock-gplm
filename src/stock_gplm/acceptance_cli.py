"""Run read-only acceptance for exactly one local Shoonya publication triplet."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stock_gplm.acceptance import (
    render_summary,
    run_acceptance,
    write_acceptance_artifacts,
)

EXIT_CODES = {"ACCEPT": 0, "QUARANTINE": 1, "REJECT": 2}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="one local START_DATE_END_DATE.json.gz file")
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory outside the raw segment directory for acceptance artifacts",
    )
    args = parser.parse_args()

    report = run_acceptance(args.source)
    try:
        json_path, summary_path = write_acceptance_artifacts(report, args.output_dir)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"Acceptance artifact error: {exc}", file=sys.stderr)
        return 3

    print(render_summary(report), end="")
    print(f"Machine report: {json_path}")
    print(f"Human summary: {summary_path}")
    return EXIT_CODES[report.decision]


if __name__ == "__main__":
    raise SystemExit(main())
