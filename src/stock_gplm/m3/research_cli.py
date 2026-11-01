"""Build M3.2 features, labels, split plans and gates from an M3.1 run."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from stock_gplm.m2.models import InstrumentInventory
from stock_gplm.m2.reference_data import import_corporate_actions_v1, import_nse_calendar_v1

from .evidence import ReferenceEvidence, SessionAxis
from .research import build_research_run


def _pair(parser: argparse.ArgumentParser, first: Path | None, second: Path | None, name: str) -> None:
    if (first is None) != (second is None):
        parser.error(f"--{name} and --{name}-manifest must be supplied together")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--calendar", type=Path)
    parser.add_argument("--calendar-manifest", type=Path)
    parser.add_argument("--corporate-actions", type=Path)
    parser.add_argument("--corporate-actions-manifest", type=Path)
    parser.add_argument("--inventory", type=Path, help="merged M2.1 inventory JSON with per-field provenance")
    args = parser.parse_args()
    _pair(parser, args.calendar, args.calendar_manifest, "calendar")
    _pair(parser, args.corporate_actions, args.corporate_actions_manifest, "corporate-actions")
    try:
        hashes: dict[str, str] = {}
        axis = None
        catalogs = {}
        inventory = None
        if args.calendar is not None:
            axis = SessionAxis.from_calendar(
                import_nse_calendar_v1(args.calendar, args.calendar_manifest).calendar
            )
            hashes["calendar"] = hashlib.sha256(args.calendar.read_bytes()).hexdigest()
        if args.corporate_actions is not None:
            imported = import_corporate_actions_v1(args.corporate_actions, args.corporate_actions_manifest)
            catalogs = {item.symbol: item for item in imported.catalogs}
            hashes["corporate_actions"] = hashlib.sha256(args.corporate_actions.read_bytes()).hexdigest()
        if args.inventory is not None:
            inventory = InstrumentInventory.load(args.inventory)
            hashes["inventory"] = hashlib.sha256(args.inventory.read_bytes()).hexdigest()
        run = build_research_run(
            readiness_run=args.readiness_run, output_root=args.output_root,
            evidence=ReferenceEvidence(inventory, catalogs), axis=axis, evidence_hashes=hashes,
        )
        gates = json.loads((run / "quality-gates.json").read_text(encoding="utf-8"))
    except (FileExistsError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"M3.2 research error: {exc}", file=sys.stderr)
        return 3
    print(f"M3.2 run: {run}")
    print(f"Training readiness: {gates['training_readiness']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
