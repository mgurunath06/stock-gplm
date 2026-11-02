"""Register manually downloaded NSE reference files and build M3.6 reference bundles.

This command never downloads anything. Download each file yourself in a browser from
the official NSE page, then register it with the URL and time you downloaded it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bundle import build_bundle
from .store import COMPONENTS, WEEKLY_RULE, load_store, register_source

DEFAULT_STORE = Path("data/reference/nse")
DEFAULT_PROTECTED = (Path("data/raw"),)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE, help="evidence store root")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="copy one downloaded file into the immutable store")
    register.add_argument("file", type=Path)
    register.add_argument("--component", required=True, choices=sorted(COMPONENTS))
    register.add_argument("--scope", required=True)
    register.add_argument("--source-url", help="the official NSE URL the file was downloaded from")
    register.add_argument("--downloaded-at", required=True, help="ISO 8601 time with timezone")
    register.add_argument("--applicable-start", required=True, help="first date the file speaks for")
    register.add_argument("--applicable-end", required=True, help="last date the file speaks for")
    register.add_argument("--publication-date")
    register.add_argument("--derived-from", action="append", default=[],
                          help="for transcriptions: SHA-256 of a registered nse-calendar-source (repeatable)")
    register.add_argument("--declared-rule", help=f"for nse-weekly-schedule: {WEEKLY_RULE}")
    register.add_argument("--notes")
    commands.add_parser("list", help="list registered sources and integrity errors")
    build = commands.add_parser("build", help="build a reference bundle and gap report for an M3.1 run")
    build.add_argument("--readiness-run", required=True, type=Path)
    build.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            record = register_source(
                args.file, store_root=args.store, component=args.component, protected_roots=DEFAULT_PROTECTED,
                metadata={"scope": args.scope, "source_url": args.source_url,
                          "downloaded_at_utc": args.downloaded_at, "applicable_start": args.applicable_start,
                          "applicable_end": args.applicable_end, "publication_date": args.publication_date,
                          "derived_from": args.derived_from or None, "declared_rule": args.declared_rule,
                          "notes": args.notes})
            print(json.dumps(record.summary(), indent=2))
            return 0
        if args.command == "list":
            records, errors = load_store(args.store)
            print(json.dumps({"sources": [r.summary() for r in records], "integrity_errors": errors}, indent=2))
            return 1 if errors else 0
        bundle = build_bundle(store_root=args.store, readiness_run=args.readiness_run,
                              output_root=args.output_root, protected_roots=DEFAULT_PROTECTED)
        manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
        report = json.loads((bundle / "coverage-report.json").read_text(encoding="utf-8"))
        print(json.dumps({"bundle": str(bundle), "components": manifest["components"],
                          "gap_count": report["gap_count"]}, indent=2))
        return 0 if all(v == "VALIDATED" for v in manifest["components"].values()) else 1
    except (FileExistsError, OSError, ValueError, KeyError) as exc:
        print(f"M3.6 evidence error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
