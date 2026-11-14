"""Reproducible historical backtests of immutable V1 strategies on adapter-released data.

The runner never decides eligibility. A ``STRICT_RESEARCH`` run accepts only an
adapter-issued :class:`UniverseAssessment`, or datasets whose decisions carry this
process's adapter seal (see :mod:`.issuance`). Caller-supplied datasets can only
produce ``DIAGNOSTIC_ONLY`` non-investable runs. The runner evaluates signals with
the shared V1 evaluator, simulates with the generic portfolio simulator and writes
an immutable run directory. Runs are read back only through :mod:`.run_store`.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from stock_gplm.m39b import AdjustmentMode, CandleSeries, RawCandle, adjust_series
from stock_gplm.v1.evaluator import evaluate_strategy
from stock_gplm.v1.metrics import TERMINAL_VALUATION_POLICY, equal_weight_buy_and_hold, performance_metrics
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

from .models import (
    EligibilityMode,
    UniverseAssessment,
    UniverseRequest,
    VerifiedHistoricalDataset,
    canonical_hash,
)
from .provenance import content_identity, environment_provenance

RUN_CONTRACT = "stock-gplm.m5-historical-backtest-run.v2"
LEGACY_RUN_CONTRACT = "stock-gplm.m5-historical-backtest-run.v1"
MANIFEST_CONTRACT = "stock-gplm.m5-run-manifest.v2"
ADAPTER_ISSUED = "ADAPTER_ISSUED"
CALLER_SUPPLIED = "CALLER_SUPPLIED_UNVERIFIED"
SETTINGS_CONTRACT = "stock-gplm.m5-backtest-settings.v1"
STRICT_CLASSIFICATION = "STRICT_RESEARCH_HISTORICAL"
DIAGNOSTIC_CLASSIFICATION = "DIAGNOSTIC_ONLY_NON_INVESTABLE"
MAX_INSTRUMENTS = 200
# NSE closes at 15:30 Asia/Kolkata (10:00 UTC); evidence must be available by then.
DECISION_TIME_UTC = "T10:00:00Z"
ANALYTICAL_FILES = ("run.json", "signals.json", "orders.json", "trades.json", "equity-curve.json", "report.md")
ENVIRONMENT_FILE = "environment.json"  # hashed in the manifest but non-analytical; never compared on replay
# M3.2 boundaries (unchanged): five shorter horizons 2026-04-29, 6-12m 2025-10-21; M5 uses the earliest.
HOLDOUT_POLICY = "EARLIEST_M32_LOCKED_HOLDOUT_FIRST_AS_OF_ACROSS_HORIZONS"


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
    """Backward-compatible alias for :func:`stock_gplm.m5.provenance.content_identity`."""
    return content_identity()


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
        manifest = {"contract": MANIFEST_CONTRACT, "run_contract": RUN_CONTRACT, "run_id": run_id,
                    "file_sha256": hashes, "file_inventory": sorted(hashes),
                    "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "non_analytical_fields": ["created_at_utc"],
                    "non_analytical_files": sorted(set(hashes) - set(ANALYTICAL_FILES))}
        (staging / "run-manifest.json").write_bytes(_encode(manifest))
        staging.rename(target)
    except BaseException:
        for child in staging.iterdir():
            child.unlink()
        staging.rmdir()
        raise
    return target


def _resolve_release(
    release: UniverseAssessment | Sequence[VerifiedHistoricalDataset],
) -> tuple[tuple[VerifiedHistoricalDataset, ...], EligibilityMode, dict[str, Any], str]:
    """Return released datasets, mode, the requested-universe record and the issuance class."""
    if isinstance(release, UniverseAssessment):
        release.verify_consistency()
        if not release.adapter_issued:
            raise ValueError("universe assessment was not issued by the adapter in this process")
        if not release.released:
            raise ValueError(f"universe {release.status.value}: no data released; nothing is run on a subset")
        datasets = release.released
        mode = release.universe.mode
        if mode == EligibilityMode.STRICT_RESEARCH and not release.investable:
            raise ValueError("STRICT_RESEARCH run requires an ELIGIBLE, adapter-issued universe")
        if mode == EligibilityMode.STRICT_RESEARCH:
            for decision in release.decisions:  # atomic: one stale member invalidates the run
                decision.revalidate()
        universe = release.to_dict()
        return tuple(datasets), mode, {"source": "UNIVERSE_ASSESSMENT", **universe}, ADAPTER_ISSUED
    datasets = tuple(release)
    if not datasets or len(datasets) > MAX_INSTRUMENTS:
        raise ValueError(f"between 1 and {MAX_INSTRUMENTS} datasets are required")
    if not all(isinstance(d, VerifiedHistoricalDataset) for d in datasets):
        raise ValueError("datasets must be VerifiedHistoricalDataset objects")
    modes = {d.decision.request.mode for d in datasets}
    if len(modes) != 1:
        raise ValueError("a run cannot mix STRICT_RESEARCH and DIAGNOSTIC_ONLY datasets")
    mode = modes.pop()
    for dataset in datasets:
        dataset.verify()
        if mode == EligibilityMode.STRICT_RESEARCH and not dataset.decision.investable:
            raise ValueError(f"{dataset.decision.request.symbol}: STRICT_RESEARCH run requires an ELIGIBLE decision "
                             "issued by HistoricalDataAdapter in this process")
    issued = all(d.decision.adapter_issued for d in datasets)
    if mode == EligibilityMode.STRICT_RESEARCH:
        ranges = {(d.decision.request.start, d.decision.request.end) for d in datasets}
        evidence = {d.decision.evidence_sha256 for d in datasets}
        if len(ranges) != 1 or len(evidence) != 1:
            raise ValueError("STRICT_RESEARCH datasets must share one assessed range and one evidence set")
        for dataset in datasets:
            dataset.decision.revalidate()
    symbols = [d.decision.request.symbol for d in datasets]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate symbols in the dataset list")
    request = datasets[0].decision.request
    universe = UniverseRequest(tuple(symbols), request.start, request.end, mode) if len(
        {(d.decision.request.start, d.decision.request.end) for d in datasets}) == 1 else None
    record = {"source": "DATASET_LIST", "universe": universe.canonical() if universe else None,
              "universe_sha256": universe.universe_sha256 if universe else None,
              "members": [{"symbol": d.decision.request.symbol, "status": d.decision.status.value,
                           "decision_id": d.decision.decision_id} for d in datasets], "excluded": []}
    return datasets, mode, record, ADAPTER_ISSUED if issued else CALLER_SUPPLIED


def _strict_cost_profile(
    datasets: Sequence[VerifiedHistoricalDataset],
    mode: EligibilityMode,
    strategy_config: Mapping[str, Any],
    settings: BacktestSettings,
) -> dict[str, Any] | None:
    """Require exact registered costs for strict execution; diagnostics remain assumptions."""
    if mode != EligibilityMode.STRICT_RESEARCH:
        return None
    profiles = {canonical_hash(d.decision.cost_profile): d.decision.cost_profile for d in datasets
                if d.decision.cost_profile is not None}
    if len(profiles) != 1 or any(d.decision.cost_profile is None for d in datasets):
        raise ValueError("STRICT_RESEARCH requires one approved cost profile covering every dataset")
    profile = next(iter(profiles.values()))
    assert profile is not None
    requested = {
        "commission_bps": strategy_config["trading_costs"]["commission_bps"],
        "slippage_bps": strategy_config["trading_costs"]["slippage_bps"],
        "buy_statutory_bps": settings.buy_statutory_bps,
        "sell_statutory_bps": settings.sell_statutory_bps,
    }
    if requested != {key: profile[key] for key in requested}:
        raise ValueError("STRICT_RESEARCH cost components do not match the approved registered profile")
    if settings.cost_basis != profile["profile_id"]:
        raise ValueError("STRICT_RESEARCH cost_basis must be the approved registered profile_id")
    return profile


def build_run(
    strategy: StrategyVersion,
    release: UniverseAssessment | Sequence[VerifiedHistoricalDataset],
    settings: BacktestSettings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute a run and its analytical files in memory without writing anything."""
    validated = validate_strategy(strategy.config)
    if validated.config_hash != strategy.config_hash:
        raise ValueError("strategy configuration does not match its immutable version hash")
    datasets, mode, universe, issuance = _resolve_release(release)
    for dataset in datasets:
        request = dataset.decision.request
        if not (request.start <= settings.evaluation_start and settings.evaluation_end <= request.end):
            raise ValueError(f"{request.symbol}: evaluation range lies outside the assessed range")
    symbols = [d.decision.request.symbol for d in datasets]
    allowed = validated.config["universe"]["symbols"]
    if allowed and set(symbols) - set(allowed):
        raise ValueError(f"symbols outside the strategy universe: {sorted(set(symbols) - set(allowed))}")
    registered_cost_profile = _strict_cost_profile(datasets, mode, validated.config, settings)
    costs = CostModel(validated.config["trading_costs"]["commission_bps"],
                      validated.config["trading_costs"]["slippage_bps"],
                      settings.buy_statutory_bps, settings.sell_statutory_bps)
    portfolio = PortfolioSettings(float(settings.initial_capital), settings.max_open_positions,
                                  float(settings.max_position_fraction), float(settings.max_gross_exposure),
                                  settings.evaluation_start, settings.evaluation_end)
    ordered = sorted(datasets, key=lambda d: d.decision.request.symbol)
    instruments = [instrument_signals(validated, d) for d in ordered]
    sessions = sorted({str(bar["date"]) for item in instruments for bar in item.bars})
    simulation = simulate_portfolio(validated.config, instruments, sessions, portfolio, costs)
    performance = performance_metrics(simulation, portfolio.initial_capital, costs)
    benchmark = equal_weight_buy_and_hold(instruments, sessions, settings.evaluation_start, settings.evaluation_end,
                                          portfolio.initial_capital, costs)
    fingerprint = canonical_hash(sorted(d.fingerprint for d in datasets))
    investable = mode == EligibilityMode.STRICT_RESEARCH and issuance == ADAPTER_ISSUED
    run_binding = {
        "contract": RUN_CONTRACT, "strategy_id": strategy.strategy_id, "strategy_version": strategy.version,
        "strategy_config_hash": strategy.config_hash, "dataset_fingerprint": fingerprint,
        "requested_universe_sha256": universe.get("universe_sha256"), "eligibility_issuance": issuance,
        "settings": settings.to_dict(), "simulator_version": SIMULATOR_VERSION, "code": content_identity(),
    }
    run_id = canonical_hash(run_binding)
    evaluated = [p["date"] for p in simulation.equity_curve]
    cutoff = ordered[0].decision.holdout_cutoff
    if len({d.decision.holdout_cutoff for d in datasets}) != 1:
        raise ValueError("datasets disagree on the locked-holdout boundary")
    released_protected = sum(1 for d in datasets for bar in d.bars if cutoff is None or bar.date >= cutoff)
    run = {
        **run_binding, "run_id": run_id,
        "classification": STRICT_CLASSIFICATION if investable else DIAGNOSTIC_CLASSIFICATION,
        "eligibility_mode": mode.value, "investable": investable, "ranking_eligible": investable,
        "promotion_eligible": False, "paper_trading_eligible": False, "ml_training_eligible": False,
        "requested_universe": universe,
        "strategy_config_snapshot": validated.config,
        "datasets": [{"symbol": d.decision.request.symbol, "decision": d.decision.to_dict(),
                      "fingerprint": d.fingerprint, "basis": d.basis, "share_events": list(d.share_events)}
                     for d in ordered],
        "evidence_versions": sorted({canonical_hash(d.decision.evidence_sources or {}): {
            "evidence_sha256": d.decision.evidence_sha256, "sources": d.decision.evidence_sources}
            for d in ordered}.values(), key=lambda item: str(item["evidence_sha256"])),
        "corporate_action_policy": "Prices are used in each dataset's stated basis without repair. Validated "
                                   "M3.9-B share events, reconciled field by field with the M2.1 catalog and "
                                   "applied only on a registered RAW_UNADJUSTED basis, re-adjust signals per "
                                   "as-of regime and rescale held quantities; cash dividends are excluded "
                                   "(price return).",
        "price_basis": sorted({d.basis for d in datasets}),
        "cost_model": asdict(costs) | {"basis": settings.cost_basis},
        "registered_cost_profile": registered_cost_profile,
        "portfolio_settings": asdict(portfolio),
        "evaluation_range": {"requested": [settings.evaluation_start, settings.evaluation_end],
                             "actual": [evaluated[0], evaluated[-1]], "sessions": len(evaluated),
                             "session_axis": "UNION_OF_RELEASED_OBSERVATIONS"},
        "execution": {"policy": simulation.execution_policy, "intrabar": simulation.intrabar_policy},
        "terminal_valuation": TERMINAL_VALUATION_POLICY,
        "performance": performance, "benchmark": benchmark,
        "open_positions_at_end": list(simulation.open_positions), "warnings": list(simulation.warnings),
        "holdout_cutoff": cutoff,
        "holdout_access": {
            "policy": HOLDOUT_POLICY, "cutoff": cutoff,
            "protected_rows_decoded_during_source_validation": 0,
            "protected_source_byte_integrity_checked": True,
            "protected_rows_released_to_execution": released_protected,
            "protected_rows_scored_or_trained": 0,
        },
        "broker_routing": "NONE", "network_used": False,
    }
    if released_protected:
        raise AssertionError("locked-holdout rows reached execution")
    files = {"run.json": run, "signals.json": list(simulation.signals), "orders.json": list(simulation.orders),
             "trades.json": list(simulation.trades), "equity-curve.json": list(simulation.equity_curve),
             "report.md": render_markdown(run)}
    return run, files


def run_historical_backtest(
    strategy: StrategyVersion,
    datasets: UniverseAssessment | Sequence[VerifiedHistoricalDataset],
    settings: BacktestSettings,
    output_root: Path,
    *,
    forbidden_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Backtest one immutable strategy version and publish the run directory.

    ``datasets`` is preferably an adapter-issued :class:`UniverseAssessment`. A plain
    list of datasets is still accepted. For ``STRICT_RESEARCH`` every decision in it
    must carry the adapter's seal. Caller-supplied datasets only run as diagnostics.
    """
    run, files = build_run(strategy, datasets, settings)
    files[ENVIRONMENT_FILE] = {"run_id": run["run_id"], "environment": environment_provenance(),
                               "part_of_run_identity": False}
    _publish(output_root, run["run_id"], files, forbidden_roots)
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
        f"- Code identity: `{run['code']['source_sha256'][:16]}` (M1, M2, M3.9-B, V1 and M5 sources; "
        f"Python {run['code']['python']['version']})",
        f"- Eligibility issuance: {run['eligibility_issuance']}; requested universe "
        f"`{str(run['requested_universe'].get('universe_sha256'))[:16]}`",
        f"- Locked holdout from {run['holdout_access']['cutoff']}: "
        f"{run['holdout_access']['protected_rows_decoded_during_source_validation']} protected row(s) decoded "
        f"during source validation; full source-byte integrity checked, "
        f"{run['holdout_access']['protected_rows_released_to_execution']} released "
        f"to execution, {run['holdout_access']['protected_rows_scored_or_trained']} scored or trained", "",
        "| Metric | Value |", "| --- | --- |",
    ]
    lines += [f"| {name} | {_fmt(value)}" + (f" ({undefined[name]})" if name in undefined else "") + " |"
              for name, value in sorted(metrics.items())]
    bench = run["benchmark"]
    lines += ["", f"Terminal valuation: {run['terminal_valuation']} (strategy and benchmark alike).",
              f"Benchmark ({bench['name']}): total return {_fmt(bench.get('total_return'))}."]
    missing = sorted({code for d in run["datasets"] for code in d["decision"]["missing_evidence"]})
    if missing:
        lines += ["", "Absent evidence: " + ", ".join(f"`{code}`" for code in missing) + "."]
    if run["warnings"]:
        lines += ["", f"Simulator warnings: {len(run['warnings'])} (see run.json)."]
    return "\n".join(lines) + "\n"


def compare_runs(run_dirs: Sequence[Path], *, replay: Any = None) -> dict[str, Any]:
    """Compatibility wrapper for :func:`stock_gplm.m5.run_store.compare_runs`."""
    from .run_store import compare_runs as verified_compare

    return verified_compare(run_dirs, replay=replay)
