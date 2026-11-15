"""``python -m stock_gplm.mvp.cli run --experiment <file> --output-root <dir>``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .experiment import run_experiment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-gplm-mvp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run every pending (strategy, horizon) pair; completed runs are skipped")
    run.add_argument("--experiment", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_experiment(args.experiment, args.output_root, log=lambda m: print(m, file=sys.stderr))
    print(json.dumps({"runs": len(report["runs"]), "accepted": report["accepted_symbols"],
                      "quarantined": sorted(report["quarantined_symbols"]),
                      "summary": str(args.output_root / "summary.csv")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
