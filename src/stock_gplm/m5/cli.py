"""Command line for M5 discovery, eligibility assessment and historical backtests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.schema import validate_strategy

from .adapter import HistoricalDataAdapter
from .discovery import discover
from .evidence import load_etf_heuristic, load_reference_evidence, read_json
from .models import DataEligibilityRequest, UniverseRequest, universe_problems
from .run_store import RunIntegrityError, compare_runs, load_run, make_replayer
from .runner import BacktestSettings, run_historical_backtest

DEFAULT_RAW_ROOT = Path("data/raw/shoonya/NSE")


def _write_new(path: Path, value: Any, raw_root: Path) -> None:
    path = Path(path).resolve()
    raw = Path(raw_root).resolve().parent.parent
    if path == raw or raw in path.parents:
        raise ValueError("outputs must be outside the raw-data tree")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def _strategy(args: argparse.Namespace):
    registry = StrategyRegistry(args.registry)
    if args.strategy_file:
        validated = validate_strategy(read_json(args.strategy_file, 256 * 1024)[0])
        return registry.find(validated.strategy_id, validated.config_hash) or registry.save(validated)
    return registry.get(args.strategy_id, args.version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)

    disc = commands.add_parser("discover", help="read-only coverage inventory of published segments")
    disc.add_argument("--etf-heuristic", type=Path, default=Path("config/m37/etf-symbol-heuristic.json"))
    disc.add_argument("--verify", action="store_true", help="re-run validate_daily on every segment")
    disc.add_argument("--output", type=Path, required=True)

    for name in ("assess", "backtest"):
        command = commands.add_parser(name)
        command.add_argument("--evidence", type=Path, required=True, help="stock-gplm.m5-evidence-paths.v1 file")
        command.add_argument("--mode", choices=["STRICT_RESEARCH", "DIAGNOSTIC_ONLY"], required=True)
        command.add_argument("--start", required=True)
        command.add_argument("--end", required=True)
    assess = commands.choices["assess"]
    assess.add_argument("--symbol", required=True)
    assess.add_argument("--expected-token")
    assess.add_argument("--output", type=Path)
    backtest = commands.choices["backtest"]
    backtest.add_argument("--symbols", nargs="+", required=True)
    backtest.add_argument("--settings", type=Path, required=True)
    backtest.add_argument("--registry", type=Path, required=True)
    source = backtest.add_mutually_exclusive_group(required=True)
    source.add_argument("--strategy-file", type=Path)
    source.add_argument("--strategy-id")
    backtest.add_argument("--version", type=int)
    backtest.add_argument("--output-root", type=Path, required=True)
    backtest.add_argument("--decisions-output", type=Path, help="write every eligibility decision here")
    backtest.add_argument("--diagnostic-subset", action="store_true",
                          help="DIAGNOSTIC_ONLY: explicitly analyse the usable members and record the excluded ones")

    compare = commands.add_parser("compare", help="compare verified runs; ranking needs --evidence and --registry")
    compare.add_argument("runs", nargs="+", type=Path)
    compare.add_argument("--evidence", type=Path, help="replay-verify runs against this evidence-paths file")
    compare.add_argument("--registry", type=Path, help="strategy registry used to replay-verify runs")
    export = commands.add_parser("export", help="write the verified report of one run to a new file")
    export.add_argument("run", type=Path)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "discover":
        report = discover(args.raw_root, load_etf_heuristic(args.etf_heuristic), verify=args.verify)
        _write_new(args.output, report, args.raw_root)
        print(json.dumps({k: v for k, v in report.items() if k != "per_symbol"}, indent=2))
        return 0
    if args.command == "compare":
        replay = None
        if (args.evidence is None) != (args.registry is None):
            parser.error("--evidence and --registry are required together")
        if args.evidence is not None:
            replay = make_replayer(HistoricalDataAdapter(args.raw_root, load_reference_evidence(args.evidence)),
                                   StrategyRegistry(args.registry))
        try:
            print(json.dumps(compare_runs(args.runs, replay=replay), indent=2, allow_nan=False))
        except RunIntegrityError as exc:
            print(json.dumps({"status": "INTEGRITY_FAILED", "error": str(exc)}, indent=2))
            return 3
        return 0
    if args.command == "export":
        try:
            verified = load_run(args.run)
        except RunIntegrityError as exc:
            print(json.dumps({"status": "INTEGRITY_FAILED", "error": str(exc)}, indent=2))
            return 3
        _write_new(args.output, {"run_id": verified.run_id, "verification": verified.verification,
                                 "markdown": verified.markdown}, args.raw_root)
        print(json.dumps(verified.summary(), indent=2))
        return 0
    adapter = HistoricalDataAdapter(args.raw_root, load_reference_evidence(args.evidence))
    if args.command == "assess":
        decision, _ = adapter.assess(DataEligibilityRequest(args.symbol, args.start, args.end, args.mode,
                                                            args.expected_token))
        if args.output:
            _write_new(args.output, decision.to_dict(), args.raw_root)
        print(json.dumps(decision.to_dict(), indent=2, allow_nan=False))
        return 0 if decision.usable else 2
    if args.strategy_id and args.version is None:
        parser.error("--strategy-id requires --version")
    settings = BacktestSettings.from_dict(read_json(args.settings, 64 * 1024)[0])
    problems = universe_problems(args.symbols)
    if problems:
        print(json.dumps({"status": "INVALID_UNIVERSE", "problems": problems}, indent=2))
        return 2
    assessment = adapter.assess_universe(UniverseRequest(tuple(args.symbols), args.start, args.end, args.mode,
                                                         args.diagnostic_subset))
    summary = assessment.to_dict()
    if args.decisions_output:
        _write_new(args.decisions_output, [d.to_dict() for d in assessment.decisions], args.raw_root)
    decisions = {member["symbol"]: member["status"] for member in summary["members"]}
    if not assessment.released:
        print(json.dumps({"status": summary["status"], "decisions": decisions, "excluded": summary["excluded"]},
                         indent=2))
        return 2
    run = run_historical_backtest(_strategy(args), assessment, settings, args.output_root,
                                  forbidden_roots=(args.raw_root.resolve().parent.parent,))
    print(json.dumps({"run_id": run["run_id"], "classification": run["classification"], "decisions": decisions,
                      "excluded": summary["excluded"], "metrics": run["performance"]["metrics"],
                      "benchmark": run["benchmark"]}, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
