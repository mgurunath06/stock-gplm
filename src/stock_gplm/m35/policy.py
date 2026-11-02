"""M3.5 training policy, cost model and daily-only training gates.

The gates decide whether a model may be trained. They read only manifest-bound
M3.2 gate results and the M3.5 universe scope; nothing here can promote missing
reference evidence to a pass.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from ..m3.labels import CONTRACTS_BY_NAME

POLICY_CONTRACT = "stock-gplm.m35-daily-training-policy.v2"
# Daily-only training does not depend on minute data; every other M3.2 gate stays required.
REQUIRED_DAILY_GATES = (
    "raw_candle_acceptance", "authoritative_calendar", "point_in_time_membership",
    "alias_history", "corporate_action_coverage", "instrument_classification",
)
NOT_REQUIRED_FOR_DAILY = ("minute_data",)
MINIMUM_FOLDS = 3
Mode = Literal["MODEL_TRAINING", "EXPLORATORY_BASELINE_ONLY"]
MODEL_NAMES = frozenset({"heuristic_momentum", "logistic", "lightgbm"})
INSTRUMENT_CLASSES = frozenset({"EQUITY", "ETF"})
# Fixed before any gated model is evaluated; the locked holdout is scored only when all hold.
PROMOTION_KEYS = frozenset({
    "candidate_model", "baseline_model", "top_excess_net_mean_above", "positive_fold_share_at_least",
    "auc_mean_at_least", "auc_must_exceed_baseline", "log_loss_must_beat_base_rate", "ece_mean_at_most",
})


@dataclass(frozen=True)
class TrainingPolicy:
    """A parsed, validated policy file. `raw` is hashed into the run binding."""

    raw: Mapping[str, Any]

    @property
    def horizons(self) -> tuple[str, ...]:
        return tuple(self.raw["horizons"])

    @property
    def universe(self) -> Mapping[str, Any]:
        return self.raw["universe"]

    @property
    def evaluation(self) -> Mapping[str, Any]:
        return self.raw["evaluation"]

    @property
    def instrument_classes(self) -> frozenset[str]:
        return frozenset(self.raw["universe"]["instrument_classes"])

    @property
    def promotion(self) -> Mapping[str, Any]:
        return self.raw["promotion_criteria"]

    @property
    def seed(self) -> int:
        return int(self.raw["seed"])

    def model_config(self, name: str) -> Mapping[str, Any]:
        return self.raw["models"][name]

    def models_for(self, mode: Mode) -> tuple[str, ...]:
        key = "training_models" if mode == "MODEL_TRAINING" else "exploratory_models"
        return tuple(self.raw[key])

    def ranking_model(self, mode: Mode) -> str:
        return self.raw["ranking_model"]["training" if mode == "MODEL_TRAINING" else "exploratory"]

    @property
    def calibration(self) -> str:
        return "isotonic" if self.evaluation["calibration"].startswith("ISOTONIC") else "platt"

    def round_trip_cost(self) -> float:
        """Total buy plus sell cost as a fraction of traded value."""
        costs = self.raw["costs"]
        return (sum(costs["buy_bps"].values()) + sum(costs["sell_bps"].values())) / 10_000.0


def load_policy(path: Path) -> TrainingPolicy:
    """Parse a policy file and refuse anything outside the declared contract."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("contract") != POLICY_CONTRACT:
        raise ValueError("unsupported M3.5 training policy contract")
    horizons = raw.get("horizons")
    if not horizons or len(set(horizons)) != len(horizons):
        raise ValueError("policy horizons must be a non-empty unique list")
    for name in horizons:
        contract = CONTRACTS_BY_NAME.get(name)
        if contract is None or contract.granularity != "DAILY":
            raise ValueError(f"policy horizon is not a daily M3.2 contract: {name}")
    for key in ("exploratory_models", "training_models"):
        if not set(raw.get(key, ())) <= MODEL_NAMES or not raw.get(key):
            raise ValueError(f"policy {key} names an unknown model")
    if "lightgbm" in raw["exploratory_models"]:
        raise ValueError("LightGBM may not run as an exploratory baseline")
    for mode in ("training", "exploratory"):
        pool = raw["training_models" if mode == "training" else "exploratory_models"]
        if raw["ranking_model"][mode] not in pool:
            raise ValueError(f"ranking model for {mode} mode is not in its model list")
    if raw["evaluation"].get("calibration") not in {
        "ISOTONIC_FIT_ON_VALIDATION_PARTITION", "PLATT_FIT_ON_VALIDATION_PARTITION",
    }:
        raise ValueError("unsupported calibration method")
    fraction = raw["evaluation"]["top_fraction"]
    if not 0 < fraction < 0.5:
        raise ValueError("top_fraction must lie in (0, 0.5)")
    classes = raw["universe"].get("instrument_classes")
    if not isinstance(classes, list) or not classes or not set(classes) <= INSTRUMENT_CLASSES:
        raise ValueError("universe.instrument_classes must list EQUITY and/or ETF")
    promotion = raw.get("promotion_criteria")
    if not isinstance(promotion, dict) or set(promotion) != PROMOTION_KEYS:
        raise ValueError("promotion_criteria must declare exactly the supported keys")
    if promotion["candidate_model"] != raw["ranking_model"]["training"] or \
            promotion["baseline_model"] not in raw["training_models"] or \
            promotion["baseline_model"] == promotion["candidate_model"]:
        raise ValueError("promotion_criteria must compare the training ranking model with another training model")
    if not 0 < promotion["positive_fold_share_at_least"] <= 1 or not 0 < promotion["ece_mean_at_most"] < 0.5:
        raise ValueError("promotion_criteria thresholds are out of range")
    budget = raw["evaluation"]["trial_budget_per_horizon"]
    if type(budget) is not int or budget <= 0:
        raise ValueError("trial budget must be a positive integer")
    for side in ("buy_bps", "sell_bps"):
        if any(value < 0 for value in raw["costs"][side].values()):
            raise ValueError("cost components must be nonnegative")
    return TrainingPolicy(raw)


def evaluate_daily_gates(
    m32_gates: Mapping[str, Any], *, scoped_raw_acceptance: bool, folds: Mapping[str, int],
    horizons: tuple[str, ...],
) -> dict[str, Any]:
    """Decide the training mode from M3.2 evidence gates and per-horizon fold counts.

    `scoped_raw_acceptance` is true only when the eligible universe excludes every
    symbol with a non-accepted segment. It can satisfy the raw-candle gate for that
    universe; it cannot satisfy any reference-evidence gate.
    """
    gates: dict[str, Any] = {}
    for name in REQUIRED_DAILY_GATES:
        source = m32_gates.get(name)
        if source is None:
            source = {"status": "BLOCKED", "blocker": f"M3.2 run predates the {name} gate"}
        if source["status"] == "PASS":
            gates[name] = {"status": "PASS", "source": "M3.2"}
        elif name == "raw_candle_acceptance" and scoped_raw_acceptance:
            gates[name] = {
                "status": "PASS_SCOPED_TO_ELIGIBLE_UNIVERSE", "source": "M3.5",
                "note": "symbols with any non-accepted segment are excluded; M3.2 reported: "
                        + source.get("blocker", ""),
            }
        else:
            gates[name] = {"status": "BLOCKED", "source": "M3.2", "blocker": source.get("blocker", "")}
    for name in NOT_REQUIRED_FOR_DAILY:
        if name in m32_gates:
            gates[name] = {"status": "NOT_REQUIRED_FOR_DAILY_ONLY_TRAINING",
                           "m32_status": m32_gates[name]["status"]}
    evidence_blockers = [
        name for name in REQUIRED_DAILY_GATES if not gates[name]["status"].startswith("PASS")
    ]
    per_horizon: dict[str, Any] = {}
    for horizon in horizons:
        blockers = list(evidence_blockers)
        if folds.get(horizon, 0) < MINIMUM_FOLDS:
            blockers.append(f"FEWER_THAN_{MINIMUM_FOLDS}_WALK_FORWARD_FOLDS")
        per_horizon[horizon] = {
            "walk_forward_folds": folds.get(horizon, 0),
            "blockers": blockers,
            "mode": "MODEL_TRAINING" if not blockers else "EXPLORATORY_BASELINE_ONLY",
        }
    return {
        "required_daily_gates": list(REQUIRED_DAILY_GATES),
        "not_required_for_daily": list(NOT_REQUIRED_FOR_DAILY),
        "gates": gates,
        "horizons": per_horizon,
    }


def promotion_decision(criteria: Mapping[str, Any], summaries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the predefined criteria to walk-forward summaries only; the holdout is never consulted."""
    candidate = summaries[criteria["candidate_model"]]
    baseline = summaries[criteria["baseline_model"]]
    share = candidate["folds_with_positive_top_excess_net"] / max(candidate["folds"], 1)
    checks = {
        "top_excess_net_mean": [candidate["top_excess_net_mean"], candidate["top_excess_net_mean"]
                                > criteria["top_excess_net_mean_above"]],
        "positive_fold_share": [round(share, 6), share >= criteria["positive_fold_share_at_least"]],
        "auc_mean": [candidate["auc"], candidate["auc"] >= criteria["auc_mean_at_least"]],
        "auc_vs_baseline": [[candidate["auc"], baseline["auc"]],
                            not criteria["auc_must_exceed_baseline"] or candidate["auc"] > baseline["auc"]],
        "log_loss_vs_base_rate": [[candidate["log_loss"], candidate["log_loss_base_rate"]],
                                  not criteria["log_loss_must_beat_base_rate"]
                                  or candidate["log_loss"] < candidate["log_loss_base_rate"]],
        "ece_mean": [candidate["ece"], candidate["ece"] <= criteria["ece_mean_at_most"]],
    }
    return {"criteria": dict(criteria), "checks": {k: {"value": v, "passed": bool(ok)} for k, (v, ok) in checks.items()},
            "promoted": all(ok for _, ok in checks.values())}
