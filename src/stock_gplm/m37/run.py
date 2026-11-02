"""EXPLORATORY_BIASED fixed-universe LightGBM research on existing daily Shoonya data.

This pipeline exists because the M3.5 reference gates cannot pass yet (see M3.6). It
reuses the manifest-verified M3.1/M3.2 inputs, the leak-safe features and labels, the
stored walk-forward plans and their purge/embargo boundaries, and the M3.5 models and
metrics. It deliberately differs from M3.5 in one way only: it fits LightGBM while the
reference gates are blocked, so every artefact is tagged EXPLORATORY_BIASED.

It never touches the locked holdout: every row whose as-of date lies after the last
walk-forward test span is dropped before any frame is built, so holdout labels are
never read. It never changes eligibility gates, writes no ranking, places no order and
promotes nothing. Trials are reserved in the shared research ledger before any fit and
count against the same per-horizon budget as M3.5.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..m3.labels import CONTRACTS_BY_NAME
from ..m35.data import MODEL_FEATURES, Inputs, build_panel, horizon_frame, load_inputs, static_universe
from ..m35.metrics import evaluate
from ..m35.models import LightGBMModel
from ..m35.policy import TrainingPolicy, evaluate_daily_gates
from ..m35.run import _append, _fit_fold, _ledger, _partitions, environment
from ..m35.data import raw_acceptance_scoped

M37_VERSION = 1
POLICY_CONTRACT = "stock-gplm.m37-exploratory-biased-policy.v1"
HEURISTIC_CONTRACT = "stock-gplm.m37-etf-symbol-heuristic.v1"
RUN_CONTRACT = "stock-gplm.m37-exploratory-biased-run.v1"
TAG = "EXPLORATORY_BIASED"
BIAS_REASONS = (
    "SURVIVORSHIP: universe is today's Shoonya master; securities delisted in the span are absent",
    "NO_CORPORATE_ACTION_CATALOG: prices unadjusted; only flagged >=20% moves are filtered",
    "NO_POINT_IN_TIME_LISTING_HISTORY: membership on each date is not established",
    "NO_ALIAS_HISTORY: symbol renames are not reconciled",
    "EMPIRICAL_SESSION_AXIS: sessions are the observed union, not an NSE calendar",
    "HEURISTIC_ETF_SEPARATION: ETFs identified by symbol naming conventions, not exchange evidence",
    "COSTS_UNVERIFIED: cost schedule is an assumption",
)
MODELS = ("heuristic_momentum", "logistic", "lightgbm")


class TrialBudgetExceeded(RuntimeError):
    """The run would exceed the shared per-horizon research trial budget."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload if isinstance(payload, bytes) else (
        payload.encode() if isinstance(payload, str) else (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    with path.open("xb") as stream:
        stream.write(data)


def load_policy(path: Path) -> TrainingPolicy:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("contract") != POLICY_CONTRACT or raw.get("tag") != TAG:
        raise ValueError("unsupported M3.7 policy contract")
    for name in raw["horizons"]:
        contract = CONTRACTS_BY_NAME.get(name)
        if contract is None or contract.granularity != "DAILY":
            raise ValueError(f"M3.7 trains daily horizons only: {name}")
    if tuple(raw["trial_models"]) != MODELS:
        raise ValueError(f"trial_models must be exactly {list(MODELS)}")
    budget = raw["evaluation"]["trial_budget_per_horizon"]
    if type(budget) is not int or budget != 20:
        raise ValueError("the research trial budget is fixed at 20 per horizon")
    if raw["universe"].get("instrument_classes") != ["EQUITY"]:
        raise ValueError("M3.7 trains ordinary equities only; ETFs stay separate")
    return TrainingPolicy(raw)


def etf_classes(symbols: list[str], path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Classify by the frozen naming heuristic; returns classes and the heuristic's record."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("contract") != HEURISTIC_CONTRACT or raw.get("status") != "HEURISTIC_UNVERIFIED":
        raise ValueError("unsupported ETF heuristic")
    patterns = [re.compile(p) for p in raw["etf_patterns"]]
    excluded = set(raw["company_exclusions"])
    classes = {}
    for symbol in symbols:
        base = symbol[:-3] if symbol.endswith("-EQ") else symbol
        etf = base not in excluded and any(p.search(base) for p in patterns)
        classes[symbol] = "ETF" if etf else "EQUITY"
    return classes, raw


def _drawdown(curve: np.ndarray) -> float:
    if not len(curve):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], curve]))[1:]
    return float((curve / peak - 1.0).min())


def portfolio(frame: pd.DataFrame, score: np.ndarray, *, cost: float, top_fraction: float, step: int,
              positions: Mapping[str, int]) -> tuple[dict[str, Any], pd.DataFrame]:
    """Non-overlapping top-fraction holding periods over pooled out-of-sample test dates.

    Each period buys the top fraction by score at the next open after the as-of session and
    sells at the bucket's last close, paying the full round trip. The benchmark is the
    same-date equal-weight mean of eligible rows, held without cost.
    """
    work = frame[["as_of_date", "target", "gross_return_at_max_offset", "benchmark_return"]].copy()
    work["score"] = score
    ranks = work.groupby("as_of_date")["score"].rank(ascending=False, method="first")
    sizes = work.groupby("as_of_date")["score"].transform("size")
    top = work[ranks <= np.maximum(1, np.floor(sizes * top_fraction))]
    per_date = top.groupby("as_of_date").agg(
        top_gross=("gross_return_at_max_offset", "mean"), benchmark=("benchmark_return", "first"),
        hits=("target", "mean"), names=("target", "size"))
    pos = np.array([positions[d] for d in per_date.index])
    periods = per_date[(pos - pos.min()) % step == 0].copy()
    periods["net"] = periods["top_gross"] - cost
    periods["excess_net"] = periods["net"] - periods["benchmark"]
    equity = np.cumprod(1 + periods["net"].to_numpy())
    bench = np.cumprod(1 + periods["benchmark"].to_numpy())
    relative = equity / bench
    summary = {
        "periods": int(len(periods)), "holding_sessions": step,
        "names_per_period_mean": round(float(periods["names"].mean()), 2) if len(periods) else None,
        "precision_top_fraction": round(float(top["target"].mean()), 6),
        "top_mean_net_per_period": round(float(periods["net"].mean()), 6) if len(periods) else None,
        "benchmark_mean_per_period": round(float(periods["benchmark"].mean()), 6) if len(periods) else None,
        "excess_net_mean_per_period": round(float(periods["excess_net"].mean()), 6) if len(periods) else None,
        "excess_net_positive_share": round(float((periods["excess_net"] > 0).mean()), 6) if len(periods) else None,
        "cumulative_net_return": round(float(equity[-1] - 1), 6) if len(periods) else None,
        "cumulative_benchmark_return": round(float(bench[-1] - 1), 6) if len(periods) else None,
        "max_drawdown_net": round(_drawdown(equity), 6),
        "max_drawdown_relative_to_benchmark": round(_drawdown(relative), 6),
    }
    periods = periods.reset_index()
    periods["cumulative_net"] = equity - 1
    periods["cumulative_benchmark"] = bench - 1
    return summary, periods


def precision_at_half(frame: pd.DataFrame, probability: np.ndarray) -> dict[str, Any]:
    chosen = probability > 0.5
    return {"rows_predicted_positive": int(chosen.sum()),
            "precision": round(float(frame["target"].to_numpy()[chosen].mean()), 6) if chosen.any() else None}


def _fingerprint(frame: pd.DataFrame) -> str:
    keys = frame[["symbol", "as_of_date", "target"]].astype(str).agg("|".join, axis=1)
    return hashlib.sha256("\n".join(keys.sort_values()).encode()).hexdigest()


def build_exploratory_run(*, m31_run: Path, m32_run: Path, policy_path: Path, etf_heuristic: Path,
                          trial_ledger: Path, output_root: Path) -> Path:
    """Train and evaluate every daily horizon with enough folds; return the run directory."""
    policy = load_policy(policy_path)
    output_root, trial_ledger = Path(output_root).resolve(), Path(trial_ledger).resolve()
    inputs: Inputs = load_inputs(m31_run, m32_run, policy.horizons)
    for protected in (inputs.raw_root, Path(m31_run).resolve(), Path(m32_run).resolve()):
        if output_root == protected or protected in output_root.parents:
            raise ValueError(f"output root must be outside {protected}")
    classes, heuristic = etf_classes(sorted(inputs.coverage["symbol"]), etf_heuristic)
    minimum = int(policy.raw["minimum_folds"])
    trainable = [h for h in policy.horizons if len(inputs.plans[h].folds) >= minimum]
    skipped = {h: f"{len(inputs.plans[h].folds)} walk-forward folds; {minimum} required"
               for h in policy.horizons if h not in trainable}
    binding = {
        "contract": RUN_CONTRACT, "m37_version": M37_VERSION, "tag": TAG,
        "m31_run_id": inputs.m31_run_id, "m32_run_id": inputs.m32_run_id, "inputs": inputs.input_hashes,
        "policy_sha256": _hash(dict(policy.raw)), "etf_heuristic_sha256": _hash(heuristic),
        "environment": environment(), "model_features": list(MODEL_FEATURES),
    }
    experiment_id = _hash(binding)
    final = output_root / experiment_id
    if final.exists():
        raise FileExistsError(f"M3.7 experiment already exists: {experiment_id}")
    output_root.mkdir(parents=True, exist_ok=True)

    universe = static_universe(inputs.coverage, policy, classes)
    eligible = set(universe.loc[universe["static_status"] == "ELIGIBLE", "symbol"])
    # Recorded for disclosure only; M3.7 never edits or overrides these gates.
    gates = evaluate_daily_gates(
        inputs.m32_gates, scoped_raw_acceptance=raw_acceptance_scoped(inputs, eligible),
        folds={h: len(p.folds) for h, p in inputs.plans.items()}, horizons=policy.horizons)

    # Shared research budget: every earlier trial in the ledger counts, whichever pipeline ran it.
    spent = _ledger(trial_ledger)
    budget = int(policy.evaluation["trial_budget_per_horizon"])
    reservations = []
    for horizon in trainable:
        used = sum(1 for e in spent if e["horizon"] == horizon)
        if used + len(MODELS) > budget:
            raise TrialBudgetExceeded(f"{horizon}: {used} trials used; {len(MODELS)} more exceed budget {budget}")
        reservations += [{"pipeline": "m37", "experiment_id": experiment_id, "run_id": experiment_id,
                          "m31_run_id": inputs.m31_run_id, "m32_run_id": inputs.m32_run_id, "horizon": horizon,
                          "model": m, "mode": TAG, "trial": used + i + 1} for i, m in enumerate(MODELS)]
    trial_ledger.parent.mkdir(parents=True, exist_ok=True)
    for entry in reservations:
        _append(trial_ledger, entry)

    partial = Path(tempfile.mkdtemp(prefix=f".{experiment_id}.partial-", dir=output_root))
    try:
        panel = build_panel(inputs, policy, eligible)
        cost = policy.round_trip_cost()
        top, bins = float(policy.evaluation["top_fraction"]), int(policy.evaluation["calibration_bins"])
        positions = {d: i for i, d in enumerate(inputs.axis.sessions)}
        metrics: dict[str, Any] = {}
        fold_rows: list[dict[str, Any]] = []
        fingerprints: dict[str, Any] = {}
        for horizon in trainable:
            plan = inputs.plans[horizon]
            embargo = plan.policy.embargo_sessions
            holdout_first = plan.locked_holdout.first_as_of
            last_test = plan.folds[-1].test.last_as_of
            # The locked holdout is never read: rows after the last test span are dropped first.
            labels = inputs.labels[horizon]
            labels = labels[labels["as_of_date"] <= last_test]
            frame = horizon_frame(panel[panel["as_of_date"] <= last_test], labels, horizon)
            if (frame["as_of_date"] >= holdout_first).any():
                raise AssertionError("a holdout row reached a model frame")
            step = CONTRACTS_BY_NAME[horizon].max_offset
            fingerprints[horizon] = {"rows": int(len(frame)), "symbols": int(frame["symbol"].nunique()),
                                     "first_as_of": frame["as_of_date"].min(), "last_as_of": frame["as_of_date"].max(),
                                     "rows_sha256": _fingerprint(frame), "folds": []}
            horizon_metrics: dict[str, Any] = {
                "tag": TAG, "folds": len(plan.folds), "labelled_eligible_rows": int(len(frame)),
                "locked_holdout": {"window": [holdout_first, plan.locked_holdout.last_as_of],
                                   "status": "NOT_ACCESSED", "rows_read": 0},
                "models": {}}
            for name in MODELS:
                per_fold, pooled_parts = [], []
                for fold in plan.folds:
                    train, validation, test = _partitions(frame, fold, inputs, embargo, holdout_first)
                    model, calibrator = _fit_fold(name, policy, train, validation)
                    raw = model.score(test)
                    probability = calibrator(raw)
                    result = evaluate(test, raw, probability, base_rate=float(train["target"].mean()), cost=cost,
                                      top_fraction=top, bins=bins, step_sessions=step, axis_positions=positions)
                    result |= {"fold": fold.index, "train_rows": int(len(train)),
                               "validation_rows": int(len(validation)), "test_rows": int(len(test)),
                               "train": [fold.train.first_as_of, fold.train.last_as_of],
                               "test": [fold.test.first_as_of, fold.test.last_as_of],
                               "precision_at_0_5": precision_at_half(test, probability)}
                    if isinstance(model, LightGBMModel):
                        result["best_iteration"] = int(model.booster.best_iteration)
                    per_fold.append(result)
                    fold_rows.append({"horizon": horizon, "model": name, **{
                        k: v for k, v in result.items() if not isinstance(v, (dict, list))}})
                    pooled_parts.append(test.assign(_score=raw, _probability=probability))
                    if name == "lightgbm":
                        fingerprints[horizon]["folds"].append({
                            "fold": fold.index, "train_sha256": _fingerprint(train),
                            "validation_sha256": _fingerprint(validation), "test_sha256": _fingerprint(test)})
                    if fold is plan.folds[-1]:
                        kind, blob = model.artefact()
                        stem = partial / "models" / horizon / f"{name}.fold{fold.index}"
                        _write(Path(f"{stem}.{kind}"), blob)
                        _write(Path(f"{stem}.calibration.json"), calibrator.artefact())
                        if isinstance(model, LightGBMModel):
                            _write(Path(f"{stem}.importance.json"), model.importance())
                pooled = pd.concat(pooled_parts, ignore_index=True)
                summary, periods = portfolio(pooled, pooled["_score"].to_numpy(), cost=cost, top_fraction=top,
                                             step=step, positions=positions)
                pooled_eval = evaluate(pooled, pooled["_score"].to_numpy(), pooled["_probability"].to_numpy(),
                                       base_rate=float(pooled["target"].mean()), cost=cost, top_fraction=top,
                                       bins=bins, step_sessions=step, axis_positions=positions)
                walk = {key: round(float(np.mean([f[key] for f in per_fold if f[key] is not None])), 6)
                        for key in ("auc", "log_loss", "log_loss_base_rate", "brier", "brier_base_rate", "ece",
                                    "rank_ic_mean", "top_excess_net_mean")}
                walk["folds_with_positive_top_excess_net"] = sum(f["top_excess_net_mean"] > 0 for f in per_fold)
                horizon_metrics["models"][name] = {
                    "fold_mean": walk, "pooled_out_of_sample": {
                        **{k: pooled_eval[k] for k in ("rows", "dates", "positive_rate", "auc", "log_loss", "brier",
                                                       "ece", "reliability", "rank_ic_mean")},
                        "precision_at_0_5": precision_at_half(pooled, pooled["_probability"].to_numpy()),
                        "portfolio": summary},
                    "folds": per_fold}
                _write(partial / "portfolio" / f"{horizon}.{name}.csv",
                       periods.to_csv(index=False, lineterminator="\n"))
            metrics[horizon] = horizon_metrics

        etf = universe[universe["instrument_class"] == "ETF"]
        _write(partial / "universe.csv", universe.to_csv(index=False, lineterminator="\n"))
        _write(partial / "etf-separated.csv", etf[["symbol", "observed_candles", "static_status"]]
               .to_csv(index=False, lineterminator="\n"))
        _write(partial / "gates-disclosure.json", gates)
        _write(partial / "feature-schema.json", {
            "features": list(MODEL_FEATURES), "dtype": "float32", "missing": "NaN (LightGBM native; logistic "
            "uses train-median imputation)", "source_contracts": "stock-gplm.m32-features.v1 plus M3.5 derived "
            "and same-date percentile columns", "target": policy.raw["target"]})
        _write(partial / "dataset-fingerprints.json", {"universe_sha256": _hash(sorted(eligible)),
                                                        "inputs": inputs.input_hashes, "horizons": fingerprints})
        _write(partial / "config" / "policy.json", dict(policy.raw))
        _write(partial / "config" / "etf-symbol-heuristic.json", heuristic)
        _write(partial / "metrics.json", metrics)
        _write(partial / "fold-metrics.csv", pd.DataFrame(fold_rows).to_csv(index=False, lineterminator="\n"))
        _write(partial / "trials.json", reservations)
        outputs = {str(p.relative_to(partial)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(partial.rglob("*")) if p.is_file()}
        _write(partial / "run-manifest.json", {
            **binding, "experiment_id": experiment_id, "bias_reasons": list(BIAS_REASONS),
            "trained_horizons": trainable, "skipped_horizons": skipped,
            "intraday": "DISABLED: no validated minute data",
            "universe": {"static_status_counts": universe["static_status"].value_counts().sort_index().to_dict(),
                         "eligible_symbols": len(eligible), "etf_heuristic_symbols": int(len(etf))},
            "trial_ledger": str(trial_ledger), "trials_reserved": len(reservations),
            "round_trip_cost": cost, "outputs": outputs, "locked_holdout_accessed": False,
            "promotion": "NEVER: exploratory-biased research artefact", "rankings_written": False,
            "raw_inputs_read": False, "orders_placed": False, "remote_calls": False,
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final
