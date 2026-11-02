"""M3.5 research-only daily training run.

A run either trains (every required daily gate passes for a horizon) or runs
only the policy's exploratory baselines. Exploratory runs never fit LightGBM and
never score the locked holdout. Trials are reserved in an append-only ledger
before any fit, and a holdout is evaluated at most once per M3.2 run and horizon.
Outputs are research artefacts: no order, signal feed or Oracle write exists here.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..m3.labels import CONTRACTS_BY_NAME
from ..m3.splits import Fold
from .data import (
    MODEL_FEATURES, Inputs, build_panel, final_fold, horizon_frame, load_inputs,
    raw_acceptance_scoped, select, static_universe,
)
from .metrics import evaluate
from .models import Calibrator, LightGBMModel, make_model
from .policy import Mode, TrainingPolicy, evaluate_daily_gates, load_policy

M35_VERSION = 2
RUN_CONTRACT = "stock-gplm.m35-daily-training-run.v1"
TRIAL_LEDGER = "trial-ledger.jsonl"
# A ranking probability is shown as supported only when its bin has this much out-of-sample
# evidence and the observed rate there stayed within the tolerance of the predicted rate.
SUPPORT_MIN_ROWS = 1000
SUPPORT_TOLERANCE = 0.05
HOLDOUT_LEDGER = "holdout-ledger.jsonl"
DISCLAIMER = ("RESEARCH_ONLY: not investment advice, not a trading signal; prices unadjusted; "
              "probabilities are calibrated on past validation data and are not guarantees")


class TrialBudgetExceeded(RuntimeError):
    """The run would exceed the policy's per-horizon trial budget."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, str)):
        data = payload.encode() if isinstance(payload, str) else payload
    else:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)


def _ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def environment() -> dict[str, str]:
    versions = {name: metadata.version(name) for name in ("numpy", "pandas", "scikit-learn", "lightgbm")}
    return {"python": platform.python_version(), **versions}


def _fit_fold(
    name: str, policy: TrainingPolicy, train: pd.DataFrame, validation: pd.DataFrame,
) -> tuple[Any, Calibrator]:
    model = make_model(name, policy.model_config(name), policy.seed)
    model.fit(train, validation)
    calibrator = Calibrator.fit(model.score(validation), validation["target"].to_numpy(), policy.calibration)
    return model, calibrator


def _partitions(
    frame: pd.DataFrame, fold: Fold, inputs: Inputs, embargo: int, holdout_first: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    axis = inputs.axis
    train = select(frame, fold.train, next_first=fold.validation.first_as_of, axis=axis, embargo=embargo)
    validation = select(frame, fold.validation, next_first=fold.test.first_as_of, axis=axis, embargo=embargo)
    test = select(frame, fold.test, next_first=holdout_first, axis=axis, embargo=embargo)
    # Train-only label-quality filter; evaluation partitions keep every row.
    return train[~train["flag_in_label_window"]], validation, test


def pooled_reliability(folds: list[dict[str, Any]]) -> dict[int, tuple[int, float, float]]:
    """Per probability bin, pooled over test folds: rows, mean predicted and observed rate."""
    pooled: dict[int, list[float]] = {}
    for fold in folds:
        for item in fold["reliability"]:
            entry = pooled.setdefault(item["bin"], [0, 0.0, 0.0])
            entry[0] += item["count"]
            entry[1] += item["mean_probability"] * item["count"]
            entry[2] += item["observed_rate"] * item["count"]
    return {b: (int(n), p / n, o / n) for b, (n, p, o) in pooled.items()}


def _support(entry: tuple[int, float, float] | None) -> str:
    if entry is None or entry[0] < SUPPORT_MIN_ROWS or abs(entry[1] - entry[2]) > SUPPORT_TOLERANCE:
        return "NOT_SUPPORTED_BY_WALK_FORWARD_EVIDENCE"
    return "SUPPORTED"


def _rank(
    panel: pd.DataFrame, model: Any, calibrator: Calibrator, *, horizon: str, model_name: str,
    mode: Mode, fit_window: Fold, evidence: dict[int, tuple[int, float, float]], bins: int,
) -> tuple[pd.DataFrame, str]:
    as_of = panel.loc[panel["eligible"], "as_of_date"].max()
    rows = panel[(panel["as_of_date"] == as_of) & panel["eligible"]].copy()
    raw = model.score(rows)
    rows["score"] = raw
    rows["probability_outperform_median"] = calibrator(raw)
    rows = rows.sort_values(["probability_outperform_median", "score", "symbol"],
                            ascending=[False, False, True], ignore_index=True)
    rows["rank"] = np.arange(1, len(rows) + 1)
    contract = CONTRACTS_BY_NAME[horizon]
    index = np.clip((rows["probability_outperform_median"].to_numpy() * bins).astype(int), 0, bins - 1)
    out = pd.DataFrame({
        "rank": rows["rank"], "symbol": rows["symbol"], "price_date": rows["as_of_date"],
        "close_unadjusted_inr": rows["close"].round(2),
        "median_turnover_20_inr": rows["median_turnover"].round(0),
        "score": rows["score"].round(6),
        "probability_outperform_median": rows["probability_outperform_median"].round(4),
        "walk_forward_observed_rate_in_bin": [
            round(evidence[b][2], 4) if b in evidence else None for b in index],
        "walk_forward_rows_in_bin": [evidence[b][0] if b in evidence else 0 for b in index],
        "probability_support": [_support(evidence.get(b)) for b in index],
        "horizon": horizon,
        "entry_policy": contract.entry_policy, "exit_sessions_after_as_of": contract.max_offset,
        "model": model_name, "mode": mode,
        "model_train_window": f"{fit_window.train.first_as_of}..{fit_window.train.last_as_of}",
        "calibration_window": f"{fit_window.validation.first_as_of}..{fit_window.validation.last_as_of}",
        "status": DISCLAIMER if mode == "MODEL_TRAINING" else "EXPLORATORY_BASELINE_GATES_BLOCKED; " + DISCLAIMER,
    })
    return out, as_of


def build_training_run(*, m31_run: Path, m32_run: Path, policy_path: Path, output_root: Path) -> Path:
    """Run gates, walk-forward evaluation, (gated) holdout and ranking; return the run directory."""
    policy = load_policy(policy_path)
    output_root = Path(output_root).resolve()
    inputs = load_inputs(m31_run, m32_run, policy.horizons)
    for protected in (inputs.raw_root, Path(m31_run).resolve(), Path(m32_run).resolve()):
        if output_root == protected or protected in output_root.parents:
            raise ValueError(f"output root must be outside {protected}")
    binding = {
        "contract": RUN_CONTRACT, "m35_version": M35_VERSION, "m31_run_id": inputs.m31_run_id,
        "m32_run_id": inputs.m32_run_id, "inputs": inputs.input_hashes,
        "policy_sha256": _hash(dict(policy.raw)), "environment": environment(),
        "model_features": list(MODEL_FEATURES),
    }
    run_id = _hash(binding)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(f"M3.5 run already exists: {run_id}")
    output_root.mkdir(parents=True, exist_ok=True)

    universe = static_universe(inputs.coverage, policy)
    eligible = set(universe.loc[universe["static_status"] == "ELIGIBLE", "symbol"])
    gate_report = evaluate_daily_gates(
        inputs.m32_gates, scoped_raw_acceptance=raw_acceptance_scoped(inputs, eligible),
        folds={h: len(p.folds) for h, p in inputs.plans.items()}, horizons=policy.horizons,
    )
    modes: dict[str, Mode] = {h: gate_report["horizons"][h]["mode"] for h in policy.horizons}

    # Reserve every trial before fitting anything; refuse rather than exceed the budget.
    ledger_path = output_root / TRIAL_LEDGER
    spent = _ledger(ledger_path)
    budget = int(policy.evaluation["trial_budget_per_horizon"])
    reservations = []
    for horizon in policy.horizons:
        used = sum(1 for e in spent if e["m32_run_id"] == inputs.m32_run_id and e["horizon"] == horizon)
        wanted = policy.models_for(modes[horizon])
        if used + len(wanted) > budget:
            raise TrialBudgetExceeded(f"{horizon}: {used} trials used, {len(wanted)} more exceed budget {budget}")
        reservations += [{"m32_run_id": inputs.m32_run_id, "horizon": horizon, "model": m,
                          "mode": modes[horizon], "run_id": run_id, "trial": used + i + 1}
                         for i, m in enumerate(wanted)]
    for entry in reservations:
        _append(ledger_path, entry)

    # A holdout is reserved before evaluation, so a crash cannot make it reusable.
    holdout_path = output_root / HOLDOUT_LEDGER
    holdout_used = {(e["m32_run_id"], e["horizon"]): e["run_id"] for e in _ledger(holdout_path)}
    for horizon in policy.horizons:
        if modes[horizon] == "MODEL_TRAINING" and (inputs.m32_run_id, horizon) not in holdout_used:
            _append(holdout_path, {"m32_run_id": inputs.m32_run_id, "horizon": horizon, "run_id": run_id})
    partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        panel = build_panel(inputs, policy, eligible)
        cost = policy.round_trip_cost()
        top, bins = float(policy.evaluation["top_fraction"]), int(policy.evaluation["calibration_bins"])
        positions = {d: i for i, d in enumerate(inputs.axis.sessions)}
        metrics: dict[str, Any] = {}
        fold_rows: list[dict[str, Any]] = []
        rankings: dict[str, Any] = {}
        for horizon in policy.horizons:
            plan, mode = inputs.plans[horizon], modes[horizon]
            embargo = plan.policy.embargo_sessions
            holdout_first = plan.locked_holdout.first_as_of if plan.locked_holdout else None
            frame = horizon_frame(panel, inputs.labels[horizon], horizon)
            step = CONTRACTS_BY_NAME[horizon].max_offset
            horizon_metrics: dict[str, Any] = {"mode": mode, "labelled_eligible_rows": int(len(frame)), "models": {}}
            for name in policy.models_for(mode):
                per_fold = []
                for fold in plan.folds:
                    train, validation, test = _partitions(frame, fold, inputs, embargo, holdout_first)
                    model, calibrator = _fit_fold(name, policy, train, validation)
                    raw = model.score(test)
                    result = evaluate(test, raw, calibrator(raw), base_rate=float(train["target"].mean()),
                                      cost=cost, top_fraction=top, bins=bins, step_sessions=step,
                                      axis_positions=positions)
                    result |= {"fold": fold.index, "train_rows": int(len(train)),
                               "validation_rows": int(len(validation)), "test": [fold.test.first_as_of, fold.test.last_as_of]}
                    if isinstance(model, LightGBMModel):
                        result["best_iteration"] = int(model.booster.best_iteration)
                    per_fold.append(result)
                    fold_rows.append({"horizon": horizon, "model": name, **{
                        k: v for k, v in result.items() if not isinstance(v, (dict, list))}})
                summary = {key: round(float(np.mean([f[key] for f in per_fold if f[key] is not None])), 6)
                           for key in ("auc", "log_loss", "log_loss_base_rate", "brier", "brier_base_rate", "ece",
                                       "rank_ic_mean", "top_excess_net_mean", "top_excess_net_positive_share")}
                summary["folds_with_positive_top_excess_net"] = sum(f["top_excess_net_mean"] > 0 for f in per_fold)
                summary["folds"] = len(per_fold)
                # Final fit: everything purged before the last test span, calibrated on that span.
                last = final_fold(plan, inputs.axis)
                train, validation, holdout = _partitions(frame, last, inputs, embargo, None)
                model, calibrator = _fit_fold(name, policy, train, validation)
                kind, blob = model.artefact()
                _write(partial / "models" / horizon / f"{name}.{kind}", blob)
                _write(partial / "models" / horizon / f"{name}.calibration.json", calibrator.artefact())
                if isinstance(model, LightGBMModel):
                    _write(partial / "models" / horizon / f"{name}.importance.json", model.importance())
                holdout_result: Any = "NOT_EVALUATED_EXPLORATORY_MODE"
                if mode == "MODEL_TRAINING":
                    previous = holdout_used.get((inputs.m32_run_id, horizon))
                    if previous is not None:
                        holdout_result = f"ALREADY_CONSUMED_BY_RUN {previous}"
                    else:
                        raw = model.score(holdout)
                        holdout_result = evaluate(
                            holdout, raw, calibrator(raw), base_rate=float(train["target"].mean()),
                            cost=cost, top_fraction=top, bins=bins, step_sessions=step, axis_positions=positions)
                horizon_metrics["models"][name] = {"walk_forward": summary, "folds": per_fold,
                                                   "locked_holdout": holdout_result,
                                                   "final_fit": {"train_rows": int(len(train)),
                                                                 "calibration_rows": int(len(validation))}}
                if name == policy.ranking_model(mode):
                    ranking, as_of = _rank(panel, model, calibrator, horizon=horizon, model_name=name,
                                           mode=mode, fit_window=last,
                                           evidence=pooled_reliability(per_fold), bins=bins)
                    _write(partial / f"ranking-{horizon}.csv", ranking.to_csv(index=False, lineterminator="\n"))
                    rankings[horizon] = {
                        "model": name, "as_of": as_of, "symbols": int(len(ranking)),
                        "probability_supported_rows": int((ranking["probability_support"] == "SUPPORTED").sum()),
                    }
            metrics[horizon] = horizon_metrics

        _write(partial / "gates.json", gate_report)
        _write(partial / "universe.csv", universe.to_csv(index=False, lineterminator="\n"))
        latest = panel["as_of_date"].max()
        _write(partial / "universe-summary.json", {
            "static_status_counts": universe["static_status"].value_counts().sort_index().to_dict(),
            "static_eligible_symbols": len(eligible),
            "rows_eligible": int(panel["eligible"].sum()), "rows_total": int(len(panel)),
            "rows_excluded_illiquid": int((panel["static_eligible"] & ~panel["liquid"]).sum()),
            "rows_excluded_flag_in_lookback": int((panel["static_eligible"] & panel["liquid"] & panel["flag_in_lookback"]).sum()),
            "latest_session": latest,
            "eligible_symbols_on_latest_session": int(panel.loc[panel["as_of_date"] == latest, "eligible"].sum()),
            "survivorship": "current-master symbols only; point-in-time membership NOT_ESTABLISHED",
        })
        _write(partial / "fold-metrics.csv", pd.DataFrame(fold_rows).to_csv(index=False, lineterminator="\n"))
        _write(partial / "metrics.json", metrics)
        _write(partial / "trials.json", reservations)
        status = {h: ("MODEL_TRAINED" if modes[h] == "MODEL_TRAINING" else "BLOCKED_EXPLORATORY_BASELINES_ONLY")
                  for h in policy.horizons}
        outputs = {str(p.relative_to(partial)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(partial.rglob("*")) if p.is_file()}
        _write(partial / "run-manifest.json", {
            **binding, "run_id": run_id, "policy": dict(policy.raw), "training_status": status,
            "round_trip_cost": cost, "rankings": rankings, "outputs": outputs,
            "raw_inputs_read": False, "orders_placed": False, "remote_calls": False,
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final
