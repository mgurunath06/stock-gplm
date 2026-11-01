"""Command line interface for the local M3 data-readiness pipeline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stock_gplm.m24.models import SyncRequestV1

from .readiness import build_readiness_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", required=True, type=Path)
    parser.add_argument("--master", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--explore", action="append", dest="symbols",
        help="current-master EQ trading symbol; repeat for multiple symbols",
    )
    parser.add_argument(
        "--sync-request", type=Path,
        help="M2.4 request whose collector windows bound the dataset; others stay inventoried",
    )
    args = parser.parse_args()
    symbols = tuple(args.symbols or ("RELIANCE-EQ", "TCS-EQ"))
    try:
        windows = None
        if args.sync_request is not None:
            request = SyncRequestV1.load(args.sync_request)
            windows = tuple(sorted(request.window_ranges))
        run = build_readiness_run(
            raw_root=args.raw_root, master=args.master, output_root=args.output_root,
            exploration_symbols=symbols, collector_windows=windows,
        )
        quality = json.loads((run / "coverage-quality.json").read_text(encoding="utf-8"))
    except (FileExistsError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"M3 readiness error: {exc}", file=sys.stderr)
        return 3
    print(f"M3 run: {run}")
    print(f"Decision: {quality['decision']}")
    print(f"Training readiness: {quality['training_readiness']}")
    return 0 if quality["decision"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
