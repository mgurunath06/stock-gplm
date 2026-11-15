"""Config-driven, resumable diagnostic experiments over V1 strategies.

An experiment file (JSON or the V1 YAML subset) names symbols, strategy files,
horizon profiles, portfolio limits and costs. Each (strategy, horizon) pair is one
run with a deterministic ``run_id`` over code, configuration and input hashes.
Completed runs are skipped on re-execution, so an interrupted batch resumes.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..v1.codec import load_document
from ..v1.evaluator import evaluate_strategy
from ..v1.metrics import equal_weight_buy_and_hold, performance_metrics
from ..v1.portfolio import SIMULATOR_VERSION, CostModel, InstrumentSignals, PortfolioSettings, simulate_portfolio
from ..v1.registry import StrategyRegistry
from ..v1.schema import ValidatedStrategy, validate_strategy
from .data import HoldoutPin, SymbolData, load_symbol, pin_holdout

EXPERIMENT_CONTRACT = "stock-gplm.mvp-experiment.v1"
RUN_CONTRACT = "stock-gplm.mvp-run.v1"
CLASSIFICATION = "DIAGNOSTIC_ONLY_NON_INVESTABLE"
_KEYS = {"contract", "name", "m32_run", "m32_run_id", "split_plan_sha256", "raw_root", "symbols",
         "strategies", "horizons", "evaluation_start", "evaluation_end", "portfolio", "costs"}
_CODE_PACKAGES = ("v1", "mvp", "ingestion")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def code_sha256() -> str:
    """Hash of the source files that determine results (paths relative to the package)."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for package in _CODE_PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class Experiment:
    name: str
    base: Path
    config: dict[str, Any]
    config_sha256: str

    def path(self, key: str) -> Path:
        return (self.base / self.config[key]).resolve()


def load_experiment(path: Path) -> Experiment:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    config = load_document(text, "yaml" if path.suffix in {".yaml", ".yml"} else "json")
    unknown = set(config) - _KEYS
    missing = _KEYS - set(config)
    if config.get("contract") != EXPERIMENT_CONTRACT or unknown or missing:
        raise ValueError(f"invalid experiment file: unknown={sorted(unknown)} missing={sorted(missing)}")
    if not config["symbols"] or len(set(config["symbols"])) != len(config["symbols"]):
        raise ValueError("symbols must be a nonempty list without duplicates")
    horizons = config["horizons"]
    if not isinstance(horizons, dict) or not horizons or any(
            not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in horizons.values()):
        raise ValueError("horizons must map names to positive max_holding_sessions")
    if not config["evaluation_start"] <= config["evaluation_end"]:
        raise ValueError("evaluation_start must not follow evaluation_end")
    return Experiment(str(config["name"]), path.resolve().parent, config, canonical_hash(config))


def horizon_variant(validated: ValidatedStrategy, horizon: str, max_hold: int) -> ValidatedStrategy:
    """Re-validate the strategy with ``exit.max_holding_sessions`` set by the horizon profile."""
    config = copy.deepcopy(validated.config)
    config["exit"]["max_holding_sessions"] = max_hold
    return validate_strategy(config)


def _csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
    buffer = io.StringIO()
    columns = list(columns or (sorted({k for r in rows for k in r}) if rows else []))
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
                         for k, v in row.items()})
    return buffer.getvalue()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _publish(target: Path, files: Mapping[str, str]) -> None:
    """Write a run directory atomically: a temp sibling renamed into place."""
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)  # a crashed earlier attempt; never a completed run
    staging.mkdir(parents=True)
    for name, content in files.items():
        (staging / name).write_text(content, encoding="utf-8")
    os.replace(staging, target)


def _per_symbol(trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for trade in trades:
        row = rows.setdefault(trade["symbol"], {"symbol": trade["symbol"], "trades": 0, "wins": 0,
                                                "net_pnl": 0.0, "sum_net_return": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if trade["net_pnl"] > 0 else 0
        row["net_pnl"] += float(trade["net_pnl"])
        row["sum_net_return"] += float(trade["net_return"])
    result = []
    for row in sorted(rows.values(), key=lambda r: r["symbol"]):
        result.append({"symbol": row["symbol"], "trades": row["trades"],
                       "win_rate": round(row["wins"] / row["trades"], 6),
                       "net_pnl": round(row["net_pnl"], 2),
                       "avg_net_return": round(row["sum_net_return"] / row["trades"], 6)})
    return result


def _symbol_buy_and_hold(instruments: Sequence[InstrumentSignals], sessions: Sequence[str], start: str, end: str,
                         capital: float, costs: CostModel) -> dict[str, Any]:
    out = {}
    for item in instruments:
        result = equal_weight_buy_and_hold([item], sessions, start, end, capital, costs)
        out[item.symbol] = result.get("total_return")
    return out


def run_experiment(experiment_path: Path, output_root: Path, log: Any = print) -> dict[str, Any]:
    """Execute every pending (strategy, horizon) run and rebuild the summary reports."""
    experiment = load_experiment(experiment_path)
    cfg = experiment.config
    output_root = Path(output_root)
    raw_root = experiment.path("raw_root")
    if output_root.resolve().is_relative_to(raw_root):
        raise ValueError("reports must live outside the raw root")
    output_root.mkdir(parents=True, exist_ok=True)
    pin = pin_holdout(experiment.path("m32_run"), cfg["m32_run_id"], cfg["split_plan_sha256"])
    if cfg["evaluation_end"] >= pin.cutoff:
        raise ValueError(f"evaluation_end {cfg['evaluation_end']} reaches the locked holdout {pin.cutoff}")

    datasets: dict[str, SymbolData] = {s: load_symbol(raw_root, s, pin.cutoff) for s in sorted(cfg["symbols"])}
    accepted = {s: d for s, d in datasets.items() if d.status == "ACCEPTED"}
    data_manifest = {"holdout_pin": pin.to_dict(), "raw_root": str(raw_root),
                     "symbols": [d.summary() for d in datasets.values()]}
    (output_root / "data-manifest.json").write_text(_json(data_manifest), encoding="utf-8")
    for symbol, data in datasets.items():
        if data.status != "ACCEPTED":
            log(f"quarantined {symbol}: {data.status} {data.issues[:3]}")
    if not accepted:
        raise ValueError("no symbol passed integrity checks")

    portfolio_cfg, costs_cfg = cfg["portfolio"], cfg["costs"]
    code = code_sha256()
    registry = StrategyRegistry(output_root / "strategy-registry.sqlite3")
    progress_path = output_root / "progress.json"
    rows: list[dict[str, Any]] = []
    for strategy_file in cfg["strategies"]:
        path = (experiment.base / strategy_file).resolve()
        base = validate_strategy(load_document(path.read_text(encoding="utf-8"),
                                               "yaml" if path.suffix in {".yaml", ".yml"} else "json"))
        registered = registry.find(base.strategy_id, base.config_hash) or registry.save(base)
        allowed = base.config["universe"]["symbols"]
        symbols = sorted(s for s in accepted if not allowed or s in allowed)
        skipped_symbols = sorted(set(accepted) - set(symbols))
        if not symbols:
            log(f"{base.strategy_id}: no applicable accepted symbol; skipped")
            continue
        for horizon, max_hold in sorted(cfg["horizons"].items(), key=lambda kv: kv[1]):
            variant = horizon_variant(base, horizon, max_hold)
            costs = CostModel(float(costs_cfg.get("commission_bps", variant.config["trading_costs"]["commission_bps"])),
                              float(costs_cfg.get("slippage_bps", variant.config["trading_costs"]["slippage_bps"])),
                              float(costs_cfg["buy_statutory_bps"]), float(costs_cfg["sell_statutory_bps"]))
            settings = PortfolioSettings(float(portfolio_cfg["initial_capital"]),
                                         int(portfolio_cfg["max_open_positions"]),
                                         float(portfolio_cfg["max_position_fraction"]),
                                         float(portfolio_cfg["max_gross_exposure"]),
                                         cfg["evaluation_start"], cfg["evaluation_end"])
            binding = {
                "contract": RUN_CONTRACT, "code_sha256": code, "simulator": SIMULATOR_VERSION,
                "strategy_id": base.strategy_id, "base_config_sha256": base.config_hash,
                "horizon": horizon, "variant_config_sha256": variant.config_hash,
                "inputs": {s: accepted[s].bars_sha256() for s in symbols}, "holdout_pin": pin.to_dict(),
                "portfolio": portfolio_cfg, "costs": {"commission_bps": costs.commission_bps,
                                                      "slippage_bps": costs.slippage_bps,
                                                      "buy_statutory_bps": costs.buy_statutory_bps,
                                                      "sell_statutory_bps": costs.sell_statutory_bps},
                "evaluation": [cfg["evaluation_start"], cfg["evaluation_end"]],
            }
            run_id = canonical_hash(binding)
            run_dir = output_root / "runs" / run_id
            if (run_dir / "run.json").is_file():
                stored = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                if stored["binding"] != json.loads(json.dumps(binding)):
                    raise ValueError(f"{run_id}: stored binding differs; refusing to reuse")
                log(f"skip {base.strategy_id}/{horizon} {run_id[:12]} (already complete)")
                rows.append(stored["summary"])
                continue
            instruments = []
            for symbol in symbols:
                bars = accepted[symbol].bars
                evaluated = evaluate_strategy(variant, bars)
                instruments.append(InstrumentSignals(symbol, bars, evaluated.entry_signals, evaluated.exit_signals))
            sessions = sorted({b["date"] for item in instruments for b in item.bars})
            simulation = simulate_portfolio(variant.config, instruments, sessions, settings, costs)
            metrics = performance_metrics(simulation, settings.initial_capital, costs)
            benchmark = equal_weight_buy_and_hold(instruments, sessions, settings.evaluation_start,
                                                  settings.evaluation_end, settings.initial_capital, costs)
            per_symbol_bh = _symbol_buy_and_hold(instruments, sessions, settings.evaluation_start,
                                                 settings.evaluation_end, settings.initial_capital, costs)
            headline = metrics["metrics"]
            summary = {
                "run_id": run_id, "strategy_id": base.strategy_id, "strategy_version": registered.version,
                "base_config_sha256": base.config_hash, "horizon": horizon, "max_holding_sessions": max_hold,
                "symbols": len(symbols), "trades": headline.get("trades"),
                "win_rate": headline.get("win_rate"), "total_return": headline.get("total_return"),
                "max_drawdown": headline.get("max_drawdown"), "profit_factor": headline.get("profit_factor"),
                "gross_pnl": headline.get("gross_pnl"), "net_pnl_realized": headline.get("net_pnl_realized"),
                "turnover": headline.get("turnover"), "sharpe_ratio_annualized": headline.get("sharpe_ratio_annualized"),
                "benchmark_total_return": benchmark.get("total_return"),
                "excess_vs_benchmark": (None if headline.get("total_return") is None
                                        or benchmark.get("total_return") is None
                                        else headline["total_return"] - benchmark["total_return"]),
                "classification": CLASSIFICATION,
            }
            run = {"binding": binding, "run_id": run_id, "summary": summary, "metrics": metrics,
                   "benchmark": benchmark, "per_symbol_buy_and_hold": per_symbol_bh,
                   "symbols_not_applicable": skipped_symbols, "warnings": list(simulation.warnings),
                   "execution_policy": simulation.execution_policy, "intrabar_policy": simulation.intrabar_policy,
                   "classification": CLASSIFICATION}
            _publish(run_dir, {
                "trades.csv": _csv(simulation.trades),
                "orders.csv": _csv(simulation.orders),
                "equity.csv": _csv(simulation.equity_curve),
                "per-symbol.csv": _csv(_per_symbol(simulation.trades),
                                       ["symbol", "trades", "win_rate", "net_pnl", "avg_net_return"]),
                "strategy-variant.json": _json(variant.config),
                "run.json": _json(run),  # written last inside staging; presence marks completion
            })
            rows.append(summary)
            progress_path.write_text(_json({"completed_run_ids": sorted(r["run_id"] for r in rows)}),
                                     encoding="utf-8")
            log(f"done {base.strategy_id}/{horizon} {run_id[:12]} trades={summary['trades']} "
                f"ret={summary['total_return']}")

    rows.sort(key=lambda r: (r["strategy_id"], r["max_holding_sessions"]))
    report = {"experiment": experiment.name, "experiment_sha256": experiment.config_sha256,
              "code_sha256": code, "holdout_pin": pin.to_dict(), "classification": CLASSIFICATION,
              "evaluation": [cfg["evaluation_start"], cfg["evaluation_end"]],
              "accepted_symbols": sorted(accepted),
              "quarantined_symbols": {s: d.issues for s, d in datasets.items() if d.status != "ACCEPTED"},
              "runs": rows}
    (output_root / "summary.json").write_text(_json(report), encoding="utf-8")
    columns = ["strategy_id", "strategy_version", "horizon", "max_holding_sessions", "symbols", "trades",
               "win_rate", "total_return", "max_drawdown", "profit_factor", "gross_pnl", "net_pnl_realized", "turnover",
               "sharpe_ratio_annualized",
               "benchmark_total_return", "excess_vs_benchmark", "run_id", "base_config_sha256", "classification"]
    (output_root / "summary.csv").write_text(_csv(rows, columns), encoding="utf-8")
    (output_root / "report.md").write_text(render_markdown(report, data_manifest), encoding="utf-8")
    return report


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):+.2f}%"


def render_markdown(report: Mapping[str, Any], data_manifest: Mapping[str, Any]) -> str:
    pin = report["holdout_pin"]
    lines = [
        f"# {report['experiment']} — {report['classification']}", "",
        "Diagnostic only: unverified provider price basis, no corporate-action adjustment, "
        "assumed costs, small non-random universe. Not evidence that any strategy works; not rankable.", "",
        f"- Evaluation: {report['evaluation'][0]} to {report['evaluation'][1]}",
        f"- Locked holdout from {pin['cutoff']} (M3.2 run `{pin['m32_run_id'][:16]}`, split plan "
        f"`{pin['split_plan_sha256'][:16]}`); only segments ending before it were opened",
        f"- Code `{report['code_sha256'][:16]}`, experiment `{report['experiment_sha256'][:16]}`",
        f"- Accepted: {', '.join(report['accepted_symbols'])}",
        f"- Quarantined: {report['quarantined_symbols'] or 'none'}", "",
        "## Data", "", "| Symbol | Status | Bars | First | Last | Withheld segments |", "|---|---|---:|---|---|---|",
    ]
    for item in data_manifest["symbols"]:
        lines.append(f"| {item['symbol']} | {item['status']} | {item['bars']} | {item['first']} | {item['last']} | "
                     f"{len(item['segments_withheld'])} |")
    lines += ["", "## Runs", "",
              "| Strategy | Horizon (max hold) | Trades | Win rate | Return | Max DD | Buy & hold | Excess |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in report["runs"]:
        lines.append(f"| {r['strategy_id']} v{r['strategy_version']} | {r['horizon']} ({r['max_holding_sessions']}) | "
                     f"{r['trades']} | {_pct(r['win_rate'])} | {_pct(r['total_return'])} | {_pct(r['max_drawdown'])} | "
                     f"{_pct(r['benchmark_total_return'])} | {_pct(r['excess_vs_benchmark'])} |")
    lines += ["", "Buy & hold = equal-weight same-universe, same sessions and costs. Per-run trades, orders, "
              "equity and per-symbol breakdowns are under `runs/<run_id>/`.", ""]
    return "\n".join(lines)
