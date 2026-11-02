"""Command line for the M3.5 research-only daily training run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .run import build_training_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only daily training (M3.5)")
    parser.add_argument("--readiness-run", required=True, type=Path, help="M3.1 run directory")
    parser.add_argument("--research-run", required=True, type=Path, help="M3.2 run directory")
    parser.add_argument("--policy", required=True, type=Path, help="M3.5 training policy JSON")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    run = build_training_run(m31_run=args.readiness_run, m32_run=args.research_run,
                             policy_path=args.policy, output_root=args.output_root)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"run": str(run), "training_status": manifest["training_status"],
                      "rankings": manifest["rankings"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
