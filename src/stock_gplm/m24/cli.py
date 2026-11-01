"""M2.4 secure, resumable Oracle-to-local synchronization commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

from .models import SyncRequestV1
from .sync import build_plan, discover_catalog, pull_plan, reconcile_run, validate_run
from .transport import TransportConfigV1, TransportError, WindowsOpenSSHTransport


def _request(path: Path) -> SyncRequestV1:
    return SyncRequestV1.load(path, require_current_universe=True)


def _transport(path: Path) -> WindowsOpenSSHTransport:
    return WindowsOpenSSHTransport(TransportConfigV1.load(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--request", required=True, type=Path)
    preflight.add_argument("--master", required=True, type=Path)
    preflight.add_argument("--transport-config", required=True, type=Path)
    preflight.add_argument("--output", required=True, type=Path)

    discover = commands.add_parser("discover")
    discover.add_argument("--request", required=True, type=Path)
    discover.add_argument("--master", required=True, type=Path)
    discover.add_argument("--transport-config", required=True, type=Path)
    discover.add_argument("--output-root", required=True, type=Path)

    plan = commands.add_parser("plan")
    plan.add_argument("--request", required=True, type=Path)
    plan.add_argument("--catalog", required=True, type=Path)
    plan.add_argument("--output-root", required=True, type=Path)

    pull = commands.add_parser("pull")
    pull.add_argument("--request", required=True, type=Path)
    pull.add_argument("--plan", required=True, type=Path)
    pull.add_argument("--transport-config", required=True, type=Path)
    pull.add_argument("--checkpoint-root", required=True, type=Path)
    pull.add_argument("--audit-root", required=True, type=Path)
    pull.add_argument("--execute-plan", required=True)

    resume = commands.add_parser("resume")
    resume.add_argument("--request", required=True, type=Path)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--transport-config", required=True, type=Path)
    resume.add_argument("--checkpoint-root", required=True, type=Path)
    resume.add_argument("--audit-root", required=True, type=Path)

    validate = commands.add_parser("validate")
    validate.add_argument("--request", required=True, type=Path)
    validate.add_argument("--run", required=True, type=Path)
    validate.add_argument("--output-root", required=True, type=Path)

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--request", required=True, type=Path)
    reconcile.add_argument("--master", required=True, type=Path)
    reconcile.add_argument("--catalog", required=True, type=Path)
    reconcile.add_argument("--output-root", required=True, type=Path)
    reconcile.add_argument("--m23-summary", type=Path)
    reconcile.add_argument("--run", type=Path)
    reconcile.add_argument("--validation", type=Path)

    args = parser.parse_args()
    try:
        request = _request(args.request)
        if args.command == "preflight":
            if Path(args.output).exists():
                raise FileExistsError("preflight output exists; refusing to overwrite")
            if Path(args.master).name != request.master_file:
                raise ValueError("master filename does not match the sync request")
            if hashlib.sha256(Path(args.master).read_bytes()).hexdigest() != request.master_sha256:
                raise ValueError("master bytes do not match the pinned checksum")
            inventory = read_shoonya_nse_eq_inventory_v1(args.master)
            if len(inventory) != 2715:
                raise ValueError("preflight master does not contain 2715 EQ symbols")
            report = _transport(args.transport_config).preflight()
            report.update({
                "master_file": request.master_file, "master_sha256": request.master_sha256,
                "eq_symbols": len(inventory), "collector_windows": 5,
                "oracle_connection_attempted": False, "transfer_attempted": False,
            })
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
            output = args.output
        elif args.command == "discover":
            output = discover_catalog(
                request=request, master_path=args.master,
                transport=_transport(args.transport_config), output_root=args.output_root,
            )
        elif args.command == "plan":
            output = build_plan(request=request, catalog_dir=args.catalog, output_root=args.output_root)
        elif args.command == "pull":
            output = pull_plan(
                request=request, plan_dir=args.plan, transport=_transport(args.transport_config),
                checkpoint_root=args.checkpoint_root, audit_root=args.audit_root,
                execute_plan_id=args.execute_plan,
            )
        elif args.command == "resume":
            checkpoint = Path(args.checkpoint_root).resolve() / args.run_id / "state.json"
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            output = pull_plan(
                request=request, plan_dir=Path(state["plan"]),
                transport=_transport(args.transport_config),
                checkpoint_root=args.checkpoint_root, audit_root=args.audit_root,
                execute_plan_id=args.run_id,
            )
        elif args.command == "validate":
            output = validate_run(request=request, run_dir=args.run, output_root=args.output_root)
        else:
            output = reconcile_run(
                request=request, master_path=args.master, catalog_dir=args.catalog,
                output_root=args.output_root, m23_summary=args.m23_summary,
                run_dir=args.run, validation_dir=args.validation,
            )
    except (
        FileExistsError, OSError, RuntimeError, TransportError, TypeError,
        ValueError, json.JSONDecodeError,
    ) as exc:
        print(f"M2.4 error: {exc}", file=sys.stderr)
        return 3
    print(f"M2.4 {args.command}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
