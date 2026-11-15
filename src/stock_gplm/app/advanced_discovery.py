"""Local ML strategy discovery beyond indicator-grid enumeration.

Gradient boosting selects nonlinear predictive features; a shallow decision tree then turns the
selected interactions into portable ``strategy.v3`` paths.  Models are only hypothesis generators:
paths still face the normal cost-aware trade simulator, chronological validation, multiple-testing
correction and a gated final segment.  No pickle is persisted and no remote service is contacted.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from ..m54.backtest import ExitPolicy, baseline, bh_adjust, fold_metrics, metrics, simulate
from .compute import forward_matrix
from .lanes import Lane
from .library import validate


def _tree_paths(model: Any, names: list[str], max_conditions: int = 6) -> list[dict[str, Any]]:
    tree = model.tree_
    rows: list[dict[str, Any]] = []

    def visit(node: int, path: list[dict[str, Any]]) -> None:
        left, right = int(tree.children_left[node]), int(tree.children_right[node])
        if left == right:
            counts = tree.value[node][0]
            total = float(counts.sum())
            probability = float(counts[1] / total) if total and len(counts) > 1 else 0.0
            if probability >= 0.55 and total >= 10 and 0 < len(path) <= max_conditions:
                rows.append({"conditions": path, "leaf_probability": probability, "leaf_samples": int(total)})
            return
        feature = names[int(tree.feature[node])]
        threshold = float(tree.threshold[node])
        visit(left, path + [{"feature": feature, "op": "lt", "threshold": threshold}])
        visit(right, path + [{"feature": feature, "op": "gt", "threshold": threshold}])

    visit(0, [])
    rows.sort(key=lambda x: (x["leaf_probability"], x["leaf_samples"]), reverse=True)
    return rows


def discover_tree_strategies(lane: Lane, cfg: Mapping[str, Any], *, seed: int = 56) -> dict[str, Any]:
    """Return independently learned, declarative tree-path strategies for one lane."""
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.tree import DecisionTreeClassifier
    except ImportError as exc:  # optional local dependency; absence is explicit
        return {"status": "BLOCKED_MISSING_DEPENDENCY", "reason": str(exc), "candidates": []}
    n = len(lane.market)
    horizon = int(cfg.get("tree_horizon_bars", 1 if not lane.intraday else 4))
    purge = max(horizon, int(cfg.get("tree_purge_bars", horizon)))
    if n < max(180, 8 * purge):
        return {"status": "BLOCKED_INSUFFICIENT_HISTORY", "bars": n, "candidates": []}
    a, b = int(n * 0.60), int(n * 0.80)
    segments = {"discovery": (0, a), "validation": (min(n, a + purge), b), "test": (min(n, b + purge), n)}
    round_trip = lane.costs.buy + lane.costs.sell
    ret, valid = forward_matrix(lane.market.open, lane.market.close, [horizon], lane.market.session_id,
                                lane.intraday, round_trip)
    y = (ret[:, 0] > 0).astype(np.int8)
    finite_cols = np.sum(np.isfinite(lane.X[:a]), axis=0) >= max(30, a // 4)
    indices = np.nonzero(finite_cols)[0]
    train_rows = np.nonzero(valid[:a, 0] & ~lane.market.no_signal[:a])[0]
    if len(indices) < 2 or len(train_rows) < 80 or len(np.unique(y[train_rows])) < 2:
        return {"status": "BLOCKED_INSUFFICIENT_LABELS", "rows": len(train_rows), "candidates": []}
    X = lane.X[:, indices].copy()
    med = np.nanmedian(X[train_rows], axis=0)
    med[~np.isfinite(med)] = 0.0
    X = np.where(np.isfinite(X), X, med)
    cap = int(cfg.get("tree_max_train_rows", 100_000))
    fit_rows = train_rows[-cap:]
    boost = GradientBoostingClassifier(n_estimators=int(cfg.get("boosting_estimators", 80)),
                                       max_depth=int(cfg.get("boosting_depth", 2)),
                                       learning_rate=float(cfg.get("boosting_learning_rate", 0.05)),
                                       subsample=float(cfg.get("boosting_subsample", 0.8)), random_state=seed)
    boost.fit(X[fit_rows], y[fit_rows])
    keep_n = min(int(cfg.get("boosting_top_features", 20)), len(indices))
    rank = np.argsort(boost.feature_importances_, kind="stable")[::-1][:keep_n]
    selected = indices[rank]
    selected_names = [lane.names[i] for i in selected]
    # Rebuild from the original matrix so the tree's feature indices match selected_names.
    SX = lane.X[:, selected].copy()
    smed = np.nanmedian(SX[train_rows], axis=0)
    smed[~np.isfinite(smed)] = 0.0
    SX = np.where(np.isfinite(SX), SX, smed)
    tree = DecisionTreeClassifier(max_depth=int(cfg.get("tree_max_depth", 4)),
                                  min_samples_leaf=max(int(cfg.get("tree_min_leaf", 30)), len(fit_rows) // 100),
                                  class_weight="balanced", random_state=seed)
    tree.fit(SX[fit_rows], y[fit_rows])
    paths = _tree_paths(tree, selected_names)[: int(cfg.get("tree_max_candidates", 30))]
    policy = ExitPolicy(horizon, float(cfg.get("tree_stop_atr", 1.5)),
                        float(cfg.get("tree_target_atr", 2.5)), None)
    evaluated: list[dict[str, Any]] = []
    for rank_no, path in enumerate(paths, 1):
        mask = np.ones(n, dtype=bool)
        for condition in path["conditions"]:
            col = lane.names.index(condition["feature"])
            raw = lane.X[:, col]
            if condition["op"] == "gt":
                mask &= raw > condition["threshold"]
            else:
                mask &= raw < condition["threshold"]
        trades = simulate(lane.market, mask, segments["validation"], policy, lane.costs)
        base = baseline(lane.market, segments["validation"], policy, lane.costs)
        met = metrics(trades, lane.market, segments["validation"], base)
        met["folds"] = fold_metrics(trades, segments["validation"], int(cfg.get("walk_forward_folds", 4)), base)
        evaluated.append({**path, "rank": rank_no, "mask": mask, "validation": met})
    qvals = bh_adjust([x["validation"].get("p_value") for x in evaluated], max(1, len(evaluated)))
    min_trades = int(cfg.get("min_oos_trades", 20))
    q_limit = float(cfg.get("fdr_q", 0.05))
    max_dd = float(cfg.get("max_drawdown", 0.30))
    candidates = []
    for item, qv in zip(evaluated, qvals):
        vm = item["validation"]
        vm["q_value"] = qv
        folds = vm["folds"]
        stable = sum(1 for f in folds if f["mean_excess"] is not None and f["mean_excess"] > 0) >= max(2, len(folds) - 1)
        promoted = (vm.get("effective_n", 0) >= min_trades and qv is not None and qv <= q_limit
                    and (vm.get("mean_net") or 0) > 0 and (vm.get("max_drawdown") or -1) >= -max_dd and stable)
        test_met = None
        classification = "REJECTED"
        if promoted:
            trades = simulate(lane.market, item["mask"], segments["test"], policy, lane.costs)
            base = baseline(lane.market, segments["test"], policy, lane.costs)
            test_met = metrics(trades, lane.market, segments["test"], base)
            passed = (test_met.get("effective_n", 0) >= min_trades and (test_met.get("mean_net") or 0) > 0
                      and (test_met.get("max_drawdown") or -1) >= -max_dd)
            classification = "VALIDATED" if passed else "REJECTED"
        doc = validate({"name": f"{lane.symbol} {lane.timeframe} learned tree path {item['rank']}",
                        "description": "Locally learned gradient-boosting-selected decision-tree interaction",
                        "timeframe": lane.timeframe, "scope": {"symbols": [lane.symbol],
                                                               "timeframes": [lane.timeframe]},
                        "direction": "long", "entry": {"logic": "AND", "conditions": item["conditions"]},
                        "exit": policy.to_dict(),
                        "provenance": {"method": "gradient_boosting_plus_decision_tree", "seed": seed,
                                       "parents": [], "forecast_horizon_bars": horizon},
                        "evidence": {"validation": vm, "test": test_met}})
        candidates.append({"document": doc, "classification": classification, "validation": vm, "test": test_met,
                           "lineage": doc["provenance"]})
    return {"status": "COMPLETED", "method": "gradient_boosting_plus_decision_tree", "device": "cpu (scikit-learn)",
            "train_rows": len(fit_rows), "selected_features": selected_names, "generated": len(paths),
            "candidates": candidates, "segments": segments}


__all__ = ["discover_tree_strategies"]
