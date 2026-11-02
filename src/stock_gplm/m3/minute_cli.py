"""M3.4 commands: import a read-only minute snapshot, then build its evidence run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .minute_publish import audit_summary, import_snapshot
from .minute_run import build_minute_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    imported = commands.add_parser("import-snapshot", help="verify and immutably publish a snapshot tar")
    imported.add_argument("--archive", required=True, type=Path)
    imported.add_argument("--sha256", required=True)
    imported.add_argument("--source-hashes", required=True, type=Path)
    imported.add_argument("--master", required=True, type=Path)
    imported.add_argument("--raw-root", required=True, type=Path)
    imported.add_argument("--audit-root", required=True, type=Path)
    imported.add_argument("--source-label", required=True)
    report = commands.add_parser("report", help="build quality, coverage, fusion and gates")
    report.add_argument("--minute-root", required=True, type=Path)
    report.add_argument("--daily-root", required=True, type=Path)
    report.add_argument("--master", required=True, type=Path)
    report.add_argument("--session-policy", required=True, type=Path)
    report.add_argument("--readiness-run", required=True, type=Path)
    report.add_argument("--research-run", required=True, type=Path)
    report.add_argument("--import-audit", required=True, type=Path)
    report.add_argument("--output-root", required=True, type=Path)
    report.add_argument("--source-label", required=True)
    args = parser.parse_args()
    try:
        if args.command == "import-snapshot":
            audit = import_snapshot(
                archive=args.archive, archive_sha256=args.sha256, source_hashes=args.source_hashes,
                master=args.master, raw_root=args.raw_root, audit_root=args.audit_root,
                source_label=args.source_label,
            )
            print(f"M3.4 import audit: {audit}")
            print(json.dumps(audit_summary(audit), indent=2, sort_keys=True))
            return 0
        run = build_minute_run(
            minute_root=args.minute_root, daily_root=args.daily_root, master=args.master,
            policy_path=args.session_policy, readiness_run=args.readiness_run,
            research_run=args.research_run, import_audit=args.import_audit,
            output_root=args.output_root, source_label=args.source_label,
        )
        gates = json.loads((run / "quality-gates.json").read_text(encoding="utf-8"))
    except (FileExistsError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"M3.4 error: {exc}", file=sys.stderr)
        return 3
    print(f"M3.4 run: {run}")
    print(f"Blockers: {', '.join(gates['blockers']) or 'none'}")
    print(f"Training readiness: {gates['training_readiness']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
