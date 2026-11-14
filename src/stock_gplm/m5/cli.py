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
from .models import DataEligibilityRequest
from .runner import BacktestSettings, compare_runs, run_historical_backtest

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

    compare = commands.add_parser("compare")
    compare.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args(argv)

    if args.command == "discover":
        report = discover(args.raw_root, load_etf_heuristic(args.etf_heuristic), verify=args.verify)
        _write_new(args.output, report, args.raw_root)
        print(json.dumps({k: v for k, v in report.items() if k != "per_symbol"}, indent=2))
        return 0
    if args.command == "compare":
        print(json.dumps(compare_runs(args.runs), indent=2, allow_nan=False))
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
    decisions, datasets = [], []
    for symbol in sorted(set(args.symbols)):
        decision, dataset = adapter.assess(DataEligibilityRequest(symbol, args.start, args.end, args.mode))
        decisions.append(decision.to_dict())
        if dataset is not None:
            datasets.append(dataset)
    if args.decisions_output:
        _write_new(args.decisions_output, decisions, args.raw_root)
    summary = {d["request"]["symbol"]: d["status"] for d in decisions}
    if not datasets:
        print(json.dumps({"status": "BLOCKED", "decisions": summary}, indent=2))
        return 2
    run = run_historical_backtest(_strategy(args), datasets, settings, args.output_root,
                                  forbidden_roots=(args.raw_root.resolve().parent.parent,))
    print(json.dumps({"run_id": run["run_id"], "classification": run["classification"], "decisions": summary,
                      "metrics": run["performance"]["metrics"], "benchmark": run["benchmark"]}, indent=2,
                     allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
