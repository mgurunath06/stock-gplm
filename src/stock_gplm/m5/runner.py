"""Reproducible historical backtests of immutable V1 strategies on adapter-released data.

The runner never decides eligibility. It accepts only ``VerifiedHistoricalDataset``
objects whose decisions re-verify, evaluates signals with the shared V1 evaluator,
simulates with the generic portfolio simulator and writes an immutable run directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from stock_gplm.m39b import AdjustmentMode, CandleSeries, RawCandle, adjust_series
from stock_gplm.v1.evaluator import evaluate_strategy
from stock_gplm.v1.metrics import comparable, equal_weight_buy_and_hold, performance_metrics
from stock_gplm.v1.portfolio import (
    SIMULATOR_VERSION,
    CostModel,
    InstrumentSignals,
    PortfolioSettings,
    ShareEvent,
    simulate_portfolio,
)
from stock_gplm.v1.registry import StrategyVersion
from stock_gplm.v1.schema import validate_strategy

from .models import EligibilityMode, VerifiedHistoricalDataset, canonical_hash

RUN_CONTRACT = "stock-gplm.m5-historical-backtest-run.v1"
SETTINGS_CONTRACT = "stock-gplm.m5-backtest-settings.v1"
STRICT_CLASSIFICATION = "STRICT_RESEARCH_HISTORICAL"
DIAGNOSTIC_CLASSIFICATION = "DIAGNOSTIC_ONLY_NON_INVESTABLE"
MAX_INSTRUMENTS = 200
# NSE closes at 15:30 Asia/Kolkata (10:00 UTC); evidence must be available by then.
DECISION_TIME_UTC = "T10:00:00Z"
ANALYTICAL_FILES = ("run.json", "signals.json", "orders.json", "trades.json", "equity-curve.json", "report.md")


@dataclass(frozen=True)
class BacktestSettings:
    """Explicit run settings; costs beyond the strategy's own are statutory charges."""

    evaluation_start: str
    evaluation_end: str
    initial_capital: float
    max_open_positions: int
    max_position_fraction: float
    max_gross_exposure: float
    buy_statutory_bps: float
    sell_statutory_bps: float
    cost_basis: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BacktestSettings":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or value.get("contract") != SETTINGS_CONTRACT or set(value) != fields | {"contract"}:
            raise ValueError(f"settings must use {SETTINGS_CONTRACT} with exactly {sorted(fields)}")
        settings = cls(**{key: value[key] for key in fields})
        for name in ("initial_capital", "max_position_fraction", "max_gross_exposure",
                     "buy_statutory_bps", "sell_statutory_bps"):
            number = getattr(settings, name)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"settings.{name} must be a finite number")
        if not isinstance(settings.cost_basis, str) or not settings.cost_basis.strip():
            raise ValueError("settings.cost_basis must describe where the cost assumptions come from")
        return settings

    def to_dict(self) -> dict[str, Any]:
        return {"contract": SETTINGS_CONTRACT, **asdict(self)}


def code_identity() -> dict[str, Any]:
    """Git commit (when available) plus a hash of the executing V1/M5 sources."""
    package = Path(__file__).resolve().parents[1]
    sources = sorted([*package.joinpath("v1").glob("*.py"), *package.joinpath("m5").glob("*.py")])
    source_hash = canonical_hash({path.relative_to(package).as_posix(): path.read_text(encoding="utf-8")
                                  for path in sources})
    commit = dirty = None
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=package, capture_output=True, text=True,
                                check=True, timeout=10).stdout.strip() or None
        status = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=package, capture_output=True,
                                text=True, check=True, timeout=10).stdout
        dirty = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"git_commit": commit, "git_worktree_dirty": dirty, "source_sha256": source_hash}


def instrument_signals(validated: Any, dataset: VerifiedHistoricalDataset) -> InstrumentSignals:
    """Evaluate one dataset causally, re-adjusting per as-of corporate-action regime.

    For each decision date only events effective (ex-date reached) and known
    (``available_at`` no later than that session's close) are applied, so later
    corporate actions never rescale earlier decisions.
    """
    rows = [bar.evaluator_row() for bar in dataset.bars]
    symbol = dataset.decision.request.symbol
    raw = evaluate_strategy(validated, rows)
    entries, exits = list(raw.entry_signals), list(raw.exit_signals)
    events = dataset.adjustment_events
    if events:
        def active(day: str) -> tuple[Any, ...]:
            cutoff = day + DECISION_TIME_UTC
            return tuple(sorted((e for e in events if e.ex_date <= day and _utc(e.source.available_at) <= cutoff),
                                key=lambda e: e.event_id))
        groups: list[tuple[tuple[Any, ...], list[int]]] = []
        for position, bar in enumerate(dataset.bars):
            current = active(bar.date)
            if groups and groups[-1][0] == current:
                groups[-1][1].append(position)
            else:
                groups.append((current, [position]))
        for current, positions in groups:
            if not current:
                continue
            last = positions[-1]
            series = CandleSeries(symbol, dataset.decision.decision_id, tuple(
                RawCandle(b.date, b.open, b.high, b.low, b.close, b.volume) for b in dataset.bars[: last + 1]))
            adjusted = adjust_series(series, current, mode=AdjustmentMode.PRICE_RETURN,
                                     knowledge_at=dataset.bars[last].date + DECISION_TIME_UTC)
            adjusted_rows = [{"date": c.trading_date, "open": float(c.adjusted_open), "high": float(c.adjusted_high),
                              "low": float(c.adjusted_low), "close": float(c.adjusted_close),
                              "volume": float(c.adjusted_volume)} for c in adjusted.candles]
            regime = evaluate_strategy(validated, adjusted_rows)
            for position in positions:
                entries[position], exits[position] = regime.entry_signals[position], regime.exit_signals[position]
    share_events = tuple(ShareEvent(e["ex_date"], float(Fraction(e["price_factor"])), e["event_id"])
                         for e in dataset.share_events)
    return InstrumentSignals(symbol, tuple(rows), tuple(entries), tuple(exits), share_events)


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _publish(output_root: Path, run_id: str, files: dict[str, Any], forbidden_roots: Sequence[Path]) -> Path:
    """Write every file into a private staging directory, then rename it into place once."""
    output_root = Path(output_root).resolve()
    for root in forbidden_roots:
        resolved = Path(root).resolve()
        if output_root == resolved or resolved in output_root.parents:
            raise ValueError("M5 reports must be written outside raw-data directories")
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / run_id
    if target.exists():
        raise FileExistsError(f"run {run_id} already exists; reports are never overwritten")
    staging = output_root / f".staging-{run_id}-{os.getpid()}"
    staging.mkdir(mode=0o700)
    try:
        hashes: dict[str, str] = {}
        for name, value in files.items():
            payload = value.encode("utf-8") if isinstance(value, str) else _encode(value)
            (staging / name).write_bytes(payload)
            hashes[name] = hashlib.sha256(payload).hexdigest()
        manifest = {"contract": RUN_CONTRACT, "run_id": run_id, "file_sha256": hashes,
                    "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "non_analytical_fields": ["created_at_utc"]}
        (staging / "run-manifest.json").write_bytes(_encode(manifest))
        staging.rename(target)
    except BaseException:
        for child in staging.iterdir():
            child.unlink()
        staging.rmdir()
        raise
    return target


def run_historical_backtest(
    strategy: StrategyVersion,
    datasets: Sequence[VerifiedHistoricalDataset],
    settings: BacktestSettings,
    output_root: Path,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Backtest one immutable strategy version over adapter-released datasets."""
    validated = validate_strategy(strategy.config)
    if validated.config_hash != strategy.config_hash:
        raise ValueError("strategy configuration does not match its immutable version hash")
    if not datasets or len(datasets) > MAX_INSTRUMENTS:
        raise ValueError(f"between 1 and {MAX_INSTRUMENTS} datasets are required")
    modes = {d.decision.request.mode for d in datasets}
    if len(modes) != 1:
        raise ValueError("a run cannot mix STRICT_RESEARCH and DIAGNOSTIC_ONLY datasets")
    mode = modes.pop()
    for dataset in datasets:
        dataset.verify()
        request = dataset.decision.request
        if mode == EligibilityMode.STRICT_RESEARCH and not dataset.decision.investable:
            raise ValueError(f"{request.symbol}: STRICT_RESEARCH run requires an ELIGIBLE decision")
        if not (request.start <= settings.evaluation_start and settings.evaluation_end <= request.end):
            raise ValueError(f"{request.symbol}: evaluation range lies outside the assessed range")
    symbols = [d.decision.request.symbol for d in datasets]
    allowed = validated.config["universe"]["symbols"]
    if allowed and set(symbols) - set(allowed):
        raise ValueError(f"symbols outside the strategy universe: {sorted(set(symbols) - set(allowed))}")
    costs = CostModel(validated.config["trading_costs"]["commission_bps"],
                      validated.config["trading_costs"]["slippage_bps"],
                      settings.buy_statutory_bps, settings.sell_statutory_bps)
    portfolio = PortfolioSettings(float(settings.initial_capital), settings.max_open_positions,
                                  float(settings.max_position_fraction), float(settings.max_gross_exposure),
                                  settings.evaluation_start, settings.evaluation_end)
    instruments = [instrument_signals(validated, d) for d in sorted(datasets, key=lambda d: d.decision.request.symbol)]
    sessions = sorted({str(bar["date"]) for item in instruments for bar in item.bars})
    simulation = simulate_portfolio(validated.config, instruments, sessions, portfolio, costs)
    performance = performance_metrics(simulation, portfolio.initial_capital)
    benchmark = equal_weight_buy_and_hold(instruments, sessions, settings.evaluation_start, settings.evaluation_end,
                                          portfolio.initial_capital, costs)
    identity = code_identity()
    fingerprint = canonical_hash(sorted(d.fingerprint for d in datasets))
    investable = mode == EligibilityMode.STRICT_RESEARCH
    run_binding = {
        "contract": RUN_CONTRACT, "strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
        "strategy_config_hash": strategy.config_hash, "dataset_fingerprint": fingerprint,
        "settings": settings.to_dict(), "simulator_version": SIMULATOR_VERSION, "code": identity,
    }
    run_id = canonical_hash(run_binding)
    evaluated = [p["date"] for p in simulation.equity_curve]
    run = {
        **run_binding, "run_id": run_id,
        "classification": STRICT_CLASSIFICATION if investable else DIAGNOSTIC_CLASSIFICATION,
        "eligibility_mode": mode.value, "investable": investable, "ranking_eligible": investable,
        "promotion_eligible": False, "paper_trading_eligible": False, "ml_training_eligible": False,
        "strategy_config_snapshot": validated.config,
        "datasets": [{"symbol": d.decision.request.symbol, "decision": d.decision.to_dict(),
                      "fingerprint": d.fingerprint, "basis": d.basis, "share_events": list(d.share_events)}
                     for d in sorted(datasets, key=lambda d: d.decision.request.symbol)],
        "corporate_action_policy": "Prices are used in each dataset's stated basis without repair. Validated "
                                   "M3.9-B share events (only on a verified RAW_UNADJUSTED basis) re-adjust signals "
                                   "per as-of regime and rescale held quantities; cash dividends are excluded "
                                   "(price return).",
        "price_basis": sorted({d.basis for d in datasets}),
        "cost_model": asdict(costs) | {"basis": settings.cost_basis},
        "portfolio_settings": asdict(portfolio),
        "evaluation_range": {"requested": [settings.evaluation_start, settings.evaluation_end],
                             "actual": [evaluated[0], evaluated[-1]], "sessions": len(evaluated),
                             "session_axis": "UNION_OF_RELEASED_OBSERVATIONS"},
        "execution": {"policy": simulation.execution_policy, "intrabar": simulation.intrabar_policy},
        "performance": performance, "benchmark": benchmark,
        "open_positions_at_end": list(simulation.open_positions), "warnings": list(simulation.warnings),
        "holdout_cutoff": datasets[0].decision.holdout_cutoff, "locked_holdout_accessed": False,
        "broker_routing": "NONE", "network_used": False,
    }
    files = {"run.json": run, "signals.json": list(simulation.signals), "orders.json": list(simulation.orders),
             "trades.json": list(simulation.trades), "equity-curve.json": list(simulation.equity_curve),
             "report.md": render_markdown(run)}
    _publish(output_root, run_id, files, forbidden_roots)
    return run


def _fmt(value: Any) -> str:
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def render_markdown(run: Mapping[str, Any]) -> str:
    """Human-readable summary; every number is copied from ``run.json``."""
    metrics = run["performance"]["metrics"]
    undefined = run["performance"]["undefined"]
    lines = [
        f"# Historical backtest `{run['run_id'][:12]}`", "",
        f"Classification: **{run['classification']}**. "
        + ("Eligible for research comparison under STRICT_RESEARCH."
           if run["investable"] else "Non-investable diagnostic. It cannot be ranked, promoted, paper traded "
           "or used for training, and it is not validated historical performance."), "",
        f"- Strategy: `{run['strategy_id']}` v{run['strategy_version']} (`{run['strategy_config_hash']}`)",
        f"- Evaluation: {run['evaluation_range']['actual'][0]} to {run['evaluation_range']['actual'][1]} "
        f"({run['evaluation_range']['sessions']} sessions); holdout withheld from {run['holdout_cutoff']}",
        f"- Instruments: {len(run['datasets'])}; dataset fingerprint `{run['dataset_fingerprint']}`",
        f"- Costs (bps): commission {run['cost_model']['commission_bps']}, slippage {run['cost_model']['slippage_bps']}, "
        f"statutory buy {run['cost_model']['buy_statutory_bps']} / sell {run['cost_model']['sell_statutory_bps']} "
        f"({run['cost_model']['basis']})",
        f"- Execution: {run['execution']['policy']}; {run['execution']['intrabar']}",
        f"- Code: {run['code']['git_commit']} (dirty={run['code']['git_worktree_dirty']}), "
        f"sources `{run['code']['source_sha256'][:16]}`", "",
        "| Metric | Value |", "| --- | --- |",
    ]
    lines += [f"| {name} | {_fmt(value)}" + (f" ({undefined[name]})" if name in undefined else "") + " |"
              for name, value in metrics.items()]
    bench = run["benchmark"]
    lines += ["", f"Benchmark ({bench['name']}): total return {_fmt(bench.get('total_return'))}."]
    missing = sorted({code for d in run["datasets"] for code in d["decision"]["missing_evidence"]})
    if missing:
        lines += ["", "Absent evidence: " + ", ".join(f"`{code}`" for code in missing) + "."]
    if run["warnings"]:
        lines += ["", f"Simulator warnings: {len(run['warnings'])} (see run.json)."]
    return "\n".join(lines) + "\n"


def compare_runs(run_dirs: Sequence[Path]) -> dict[str, Any]:
    """Side-by-side comparison; a ranking is produced only for comparable strict runs."""
    runs = [json.loads((Path(d) / "run.json").read_text(encoding="utf-8")) for d in run_dirs]
    fair, reasons = comparable(runs)
    rows = [{"run_id": r["run_id"], "strategy_id": r["strategy_id"], "strategy_version": r["strategy_version"],
             "classification": r["classification"], "total_return": r["performance"]["metrics"]["total_return"],
             "max_drawdown": r["performance"]["metrics"]["max_drawdown"], "trades": r["performance"]["metrics"]["trades"],
             "benchmark_total_return": r["benchmark"].get("total_return")} for r in runs]
    ranking = [row["run_id"] for row in sorted(rows, key=lambda row: -row["total_return"])] if fair else None
    return {"comparable": fair, "reasons": reasons, "rows": rows, "ranking_by_total_return": ranking}
