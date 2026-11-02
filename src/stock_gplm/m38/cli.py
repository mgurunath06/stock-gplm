"""Command line for M3.8 signal discovery and data-quality diagnostics (no new model trials)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .run import build_diagnostics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3.8 diagnostics over development data only")
    parser.add_argument("--readiness-run", required=True, type=Path)
    parser.add_argument("--research-run", required=True, type=Path)
    parser.add_argument("--experiment", required=True, type=Path, help="M3.7 experiment directory to replay")
    parser.add_argument("--trial-ledger", required=True, type=Path, help="verified unchanged; never written")
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args(argv)
    run = build_diagnostics(m31_run=args.readiness_run, m32_run=args.research_run, experiment=args.experiment,
                            trial_ledger=args.trial_ledger, output_root=args.output_root)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"run": str(run), "replay_verified": manifest["replay_verified"],
                      "new_model_trials": manifest["new_model_trials"],
                      "hypothesis_tests": manifest["hypothesis_tests"]}, indent=2))
    return 0 if manifest["replay_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
