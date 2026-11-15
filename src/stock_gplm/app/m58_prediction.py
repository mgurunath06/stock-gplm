"""Frozen Stock-DNA forecasts, separate reveal, and walk-forward evaluation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .db import sha, utc_now
from .ingest import Ingestor, normalize_symbol, window_data
from .lanes import all_bars, build_lane
from .models import PlattCalibrator
from .stock_dna import CONTRACT as DNA_CONTRACT, discover_lane

CONTRACT = "stock-gplm.m58-prediction.v1"


def _vote(patterns: Sequence[Mapping[str, Any]], lane: Any, row: int) -> tuple[float, list[dict[str, Any]]]:
    cols = {name: i for i, name in enumerate(lane.names)}
    weighted = total = 0.0
    support = []
    for pattern in patterns:
        j = cols.get(pattern["feature"])
        if j is None or not np.isfinite(lane.X[row, j]):
            continue
        value = float(lane.X[row, j])
        holds = value <= pattern["threshold"] if pattern["threshold_side"] == "LOW" else value >= pattern["threshold"]
        if not holds:
            continue
        rate = float(pattern.get("success_rate") or 0.5)
        p_up = rate if pattern["direction"] == "UP" else 1 - rate
        weight = max(1e-6, abs(float(pattern.get("improvement") or 0))) * math.sqrt(pattern["observations"])
        weighted += p_up * weight
        total += weight
        support.append({"pattern_id": pattern["pattern_id"], "condition": pattern["condition"],
                        "direction": pattern["direction"], "status": pattern["status"],
                        "observations": pattern["observations"], "success_rate": rate,
                        "improvement": pattern["improvement"], "q_value": pattern["q_value"],
                        "current_value": value, "weight": weight})
    support.sort(key=lambda x: -x["weight"])
    return (weighted / total if total else 0.5), support


def _atomic_json(path: Path, doc: Mapping[str, Any]) -> None:
    text = json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old != json.loads(text):
            raise ValueError(f"frozen prediction report already exists with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def predict(app: Any, symbol: str, as_of: str, target_session: str, timeframe: str, horizon: int,
            out: Path) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    if target_session <= as_of:
        raise ValueError("target session must be after the as-of cutoff")
    raw = Ingestor(app.s, app.db).ingest(symbol, use_minute=True)
    data = window_data(raw, end=as_of)  # the only data object from which lanes/features/DNA are built
    bars = all_bars(data)
    if timeframe not in bars or not len(bars[timeframe]):
        raise ValueError(f"no accepted {timeframe} candles at or before {as_of}")
    lane = build_lane(data, timeframe, app.s.section("costs"), bars=bars,
                      cache_dir=app.s.cache_dir / "features")
    if lane is None or len(lane.market) <= horizon + 20:
        raise ValueError("insufficient accepted history for prediction")
    dna_cfg = app.s.section("m58").get("stock_dna", {})
    dna = discover_lane(lane, dna_cfg, [horizon])
    patterns = [p for p in dna["patterns"] if p["status"] in {"VALIDATED", "EXPERIMENTAL"}]
    cal_start = int(dna["calibration_start_index"])
    cal_p, cal_y, cal_ret = [], [], []
    cost = lane.costs.buy + lane.costs.sell
    for row in range(cal_start, len(lane.market) - horizon):
        p, support = _vote(patterns, lane, row)
        if not support:
            continue
        ret = float(lane.market.close[row + horizon] / lane.market.open[row + 1] - 1 - cost)
        if np.isfinite(ret):
            cal_p.append(p); cal_y.append(int(ret > 0)); cal_ret.append(ret)
    pcfg = app.s.section("m58").get("prediction", {})
    calibrator = PlattCalibrator(int(pcfg.get("minimum_calibration_outcomes", 30))).fit(cal_p, cal_y)
    raw_p, support = _vote(patterns, lane, len(lane.market) - 1)
    calibrated = calibrator.status.startswith("PLATT") and bool(support)
    probability = float(calibrator.apply(np.array([raw_p]))[0]) if calibrated else None
    min_p = float(app.s.section("prediction").get("min_probability", 0.56))
    direction = "UNCERTAIN" if probability is None or max(probability, 1 - probability) < min_p else \
        ("UP" if probability >= 0.5 else "DOWN")
    comparable = np.asarray(cal_ret, dtype=float)
    expected = float(np.mean(comparable)) if calibrated and len(comparable) >= 20 else None
    interval = ([float(np.quantile(comparable, 0.1)), float(np.quantile(comparable, 0.9))]
                if calibrated and len(comparable) >= 20 else None)
    decision = len(lane.market) - 1
    decision_session = str(lane.market.sessions[decision])
    natural = json.dumps({"contract": CONTRACT, "symbol": symbol, "timeframe": timeframe, "as_of": as_of,
                          "decision": decision_session, "target": target_session, "horizon": horizon,
                          "data": lane.data_fingerprint, "dna": dna["feature_config_sha"]}, sort_keys=True)
    prediction_id = sha(natural)[:24]
    payload = {"contract": CONTRACT, "requested_as_of": as_of, "decision_session": decision_session,
               "target_session": target_session, "cutoff_enforcement": "all data windowed before lane construction",
               "data_last_session": data.last_session, "dna_contract": DNA_CONTRACT,
               "dna_counts": dna["counts"], "dna_patterns_considered": len(patterns),
               "supporting_stock_dna": support[:20], "calibration_outcomes": len(cal_y),
               "calibration": calibrator.status, "raw_probability_internal": raw_p,
               "probability_exposed": probability, "expected_net_return": expected,
               "forecast_return_range_p10_p90": interval, "forecast_price_range":
               ([float(lane.market.close[-1] * (1 + interval[0])), float(lane.market.close[-1] * (1 + interval[1]))]
                if interval else None), "abstained": direction == "UNCERTAIN",
               "evidence_quality": "CALIBRATED" if calibrated else "INSUFFICIENT_CALIBRATION",
               "risk": ["research forecast, not investment advice", "daily delivery shorting is not modelled",
                        "costs are configured assumptions"], "created_utc": utc_now()}
    inserted = app.db.add_prediction({
        "prediction_id": prediction_id, "natural_key": natural, "mode": "HISTORICAL_FROZEN",
        "replay_id": "m58-" + sha(natural)[:16], "symbol": symbol, "timeframe": timeframe,
        "lane_key": lane.key, "source_kind": "STOCK_DNA", "source_id": "DNA_ENSEMBLE",
        "source_version": CONTRACT, "horizon_bars": horizon, "decision_label": decision_session,
        "decision_ns": int(lane.market.end_ns[-1]), "target_session": target_session, "direction": direction,
        "prob_raw": raw_p, "prob_calibrated": probability, "calibration": calibrator.status,
        "actionable": int(direction == "UP" and calibrated), "entry_ref": float(lane.market.close[-1]),
        "stop": None, "target": None, "cost_bps": cost * 1e4, "data_fingerprint": lane.data_fingerprint,
        "feature_config_sha": lane.feature_config_sha, "payload": payload})
    frozen = app.db.one("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,))
    assert frozen is not None
    stable = {"prediction_id": prediction_id,
              "symbol": symbol, "as_of": as_of, "decision_session": decision_session,
              "target_session": target_session, "direction": direction, "probability": probability,
              "expected_net_return": expected, "forecast_price_range": payload["forecast_price_range"],
              "action": "BUY" if direction == "UP" and calibrated else "NO_TRADE",
              "evidence_quality": payload["evidence_quality"], "supporting_patterns": support[:10],
              "data_fingerprint": lane.data_fingerprint}
    _atomic_json(out / f"{prediction_id}.json", stable)
    return {"status": "FROZEN" if inserted else "ALREADY_FROZEN", **stable,
            "report": str(out / f"{prediction_id}.json")}


def reveal(app: Any, prediction_id: str) -> dict[str, Any]:
    pred = app.db.one("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,))
    if pred is None or pred["mode"] != "HISTORICAL_FROZEN":
        raise ValueError(f"frozen M5.8 prediction not found: {prediction_id}")
    existing = app.db.one("SELECT * FROM evaluations WHERE prediction_id=?", (prediction_id,))
    if existing is not None:
        return {"status": "ALREADY_REVEALED", "prediction_id": prediction_id, **existing}
    data = Ingestor(app.s, app.db).ingest(pred["symbol"], use_minute=True)
    bars = all_bars(data)
    lane = build_lane(data, pred["timeframe"], app.s.section("costs"), bars=bars,
                      cache_dir=app.s.cache_dir / "features")
    if lane is None:
        raise ValueError("prediction lane is unavailable")
    target = str(pred["target_session"])
    idx = np.flatnonzero(lane.market.sessions == target)
    if not len(idx):
        raise ValueError(f"actual target session {target} is not available; reveal remains blocked")
    t = int(idx[-1])
    decision_idx = np.flatnonzero(lane.market.sessions == str(pred["decision_label"]))
    if not len(decision_idx) or int(decision_idx[-1]) + 1 > t:
        raise ValueError("prediction decision/target sessions are not ordered in the accepted lane")
    entry = int(decision_idx[-1]) + 1
    gross = float(lane.market.close[t] / lane.market.open[entry] - 1)
    net = gross - lane.costs.buy - lane.costs.sell
    actual = "UP" if net > 0 else "DOWN" if net < 0 else "FLAT"
    direction = pred["direction"]
    correct = None if direction == "UNCERTAIN" else int(direction == actual)
    prob = pred["prob_calibrated"]
    brier = None if prob is None else (float(prob) - int(net > 0)) ** 2
    trade_net = net if pred["actionable"] and direction == "UP" else None
    payload = {"actual_open": float(lane.market.open[entry]), "actual_close": float(lane.market.close[t]),
               "gross_return": gross, "net_return_after_configured_costs": net, "actual_direction": actual,
               "frozen_prediction": {"direction": direction, "probability": prob},
               "revealed_from_data_fingerprint": lane.data_fingerprint}
    app.db.add_evaluation({"prediction_id": prediction_id, "outcome_label": target,
                           "outcome_ns": int(lane.market.end_ns[t]), "realized_return": net,
                           "up": int(net > 0), "correct": correct, "brier": brier,
                           "trade_net_return": trade_net, "exit_reason": "TARGET_SESSION_CLOSE",
                           "bars_held": int(pred["horizon_bars"]), "payload_json": payload})
    return {"status": "REVEALED", "prediction_id": prediction_id, "target_session": target,
            "predicted_direction": direction, "actual_direction": actual, "correct": correct,
            "probability": prob, "brier": brier, "gross_return": gross, "net_return": net,
            "actual_open": payload["actual_open"], "actual_close": payload["actual_close"]}


def evaluate(app: Any, symbol: str | None, out: Path) -> dict[str, Any]:
    where, params = "WHERE p.mode='HISTORICAL_FROZEN'", []
    if symbol:
        where += " AND p.symbol=?"; params.append(normalize_symbol(symbol))
    rows = app.db.q("SELECT p.*, e.correct, e.brier, e.trade_net_return, e.realized_return FROM predictions p "
                    "LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id " + where + " ORDER BY p.decision_ns",
                    params)
    revealed = [r for r in rows if r["realized_return"] is not None]
    directional = [r for r in revealed if r["direction"] != "UNCERTAIN" and r["correct"] is not None]
    probs = [r for r in revealed if r["prob_calibrated"] is not None and r["brier"] is not None]
    trades = [float(r["trade_net_return"]) for r in revealed if r["trade_net_return"] is not None]
    bins = []
    for lo in np.arange(0, 1, 0.1):
        group = [r for r in probs if lo <= float(r["prob_calibrated"]) < lo + 0.1]
        if group:
            bins.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "n": len(group),
                         "mean_probability": float(np.mean([r["prob_calibrated"] for r in group])),
                         "observed_up_rate": float(np.mean([r["realized_return"] > 0 for r in group]))})
    result = {"contract": CONTRACT, "predictions": len(rows), "revealed": len(revealed),
              "directional_predictions": len(directional),
              "directional_accuracy": float(np.mean([r["correct"] for r in directional])) if directional else None,
              "abstention_rate": float(np.mean([r["direction"] == "UNCERTAIN" for r in rows])) if rows else None,
              "mean_brier": float(np.mean([r["brier"] for r in probs])) if probs else None,
              "calibration_bins": bins, "trades": len(trades),
              "mean_trade_net_return": float(np.mean(trades)) if trades else None,
              "total_compounded_trade_return": float(np.prod(1 + np.asarray(trades)) - 1) if trades else None}
    out.mkdir(parents=True, exist_ok=True)
    (out / "prediction-evaluation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return {**result, "report": str(out / "prediction-evaluation.json")}


def walk_forward(app: Any, symbol: str, start: str, end: str, timeframe: str, horizon: int, out: Path,
                 max_steps: int | None = None) -> dict[str, Any]:
    """Chronological freeze-then-reveal loop. Natural keys make reruns a deterministic resume."""
    symbol = normalize_symbol(symbol)
    data = Ingestor(app.s, app.db).ingest(symbol, use_minute=True)
    sessions = [str(x) for x in data.daily["date"].tolist()]
    plan = [(sessions[i], sessions[i + horizon]) for i in range(len(sessions) - horizon)
            if start <= sessions[i] <= end]
    if max_steps is not None:
        plan = plan[:max_steps]
    done = frozen = revealed = 0
    errors = []
    for as_of, target in plan:
        try:
            pred = predict(app, symbol, as_of, target, timeframe, horizon, out / "frozen")
            frozen += int(pred["status"] == "FROZEN")
            actual = reveal(app, pred["prediction_id"])
            revealed += int(actual["status"] == "REVEALED")
            done += 1
        except Exception as exc:
            errors.append({"as_of": as_of, "target": target, "error": f"{type(exc).__name__}: {exc}"})
    summary = evaluate(app, symbol, out)
    result = {"status": "COMPLETED" if not errors else "PARTIAL", "symbol": symbol, "planned": len(plan),
              "steps_done": done, "newly_frozen": frozen, "newly_revealed": revealed,
              "errors": errors, "evaluation": summary}
    (out / "walk-forward-summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


__all__ = ["CONTRACT", "evaluate", "predict", "reveal", "walk_forward"]
