"""Command line for the M3.7 EXPLORATORY_BIASED fixed-universe training run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .run import build_exploratory_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EXPLORATORY_BIASED daily training (M3.7); never promoted")
    parser.add_argument("--readiness-run", required=True, type=Path)
    parser.add_argument("--research-run", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=Path("config/m37/exploratory-biased-policy.json"))
    parser.add_argument("--etf-heuristic", type=Path, default=Path("config/m37/etf-symbol-heuristic.json"))
    parser.add_argument("--trial-ledger", required=True, type=Path,
                        help="the shared research ledger, e.g. reports/m35/trial-ledger.jsonl")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    run = build_exploratory_run(m31_run=args.readiness_run, m32_run=args.research_run, policy_path=args.policy,
                                etf_heuristic=args.etf_heuristic, trial_ledger=args.trial_ledger,
                                output_root=args.output_root)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"run": str(run), "tag": manifest["tag"], "trained": manifest["trained_horizons"],
                      "skipped": manifest["skipped_horizons"], "trials_reserved": manifest["trials_reserved"]},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
