"""Frozen Stock-DNA forecasts, separate reveal, and walk-forward evaluation."""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .db import sha, utc_now
from .ingest import Ingestor, SymbolData, normalize_symbol, window_data
from .lanes import all_bars, build_lane
from .m58_snapshot import build_snapshot
from .models import PlattCalibrator
from .stock_dna import CONTRACT as DNA_CONTRACT, discover_lane

# v2: directional classification, calibration, actual direction and Brier use the gross price move;
# configured costs apply only to the TRADE gate and trade P&L. v1 rows remain frozen and untouched.
CONTRACT = "stock-gplm.m58-prediction.v2"


def _as_of_cutoff_ns(as_of: str) -> int:
    """Date cutoffs mean the regular-session close; timestamps retain their exact instant."""
    if len(as_of) == 10:
        stamp = pd.Timestamp(f"{as_of} 15:30", tz="Asia/Kolkata")
    else:
        stamp = pd.Timestamp(as_of)
        stamp = stamp.tz_localize("Asia/Kolkata") if stamp.tzinfo is None else stamp
    return int(stamp.tz_convert("UTC").value)


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


SNAPSHOT_FROZEN = "FROZEN_WITH_PREDICTION"
SNAPSHOT_LEGACY = "RECONSTRUCTED_FOR_LEGACY_PREDICTION"


def _resolve_snapshot(app: Any, prediction_id: str,
                      computed: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    """Never replace a frozen snapshot. A reused prediction keeps the snapshot it was frozen with; a
    legacy prediction (frozen before snapshots existed) gets one first-write-wins reconstruction from the
    same as-of data, explicitly labelled as such."""
    existing = app.db.one("SELECT payload_json FROM predictions WHERE prediction_id=?", (prediction_id,))
    if existing is None:
        digest = app.db.add_feature_snapshot(computed)
        return dict(computed), digest, SNAPSHOT_FROZEN
    frozen_sha = json.loads(existing["payload_json"]).get("feature_snapshot_sha256")
    if frozen_sha:
        stored = app.db.get_feature_snapshot(str(frozen_sha))
        if stored is None:
            raise ValueError(f"frozen feature snapshot {frozen_sha} is missing for prediction {prediction_id}")
        return stored, str(frozen_sha), SNAPSHOT_FROZEN
    key = f"m58-legacy-snapshot:{prediction_id}"
    link = app.db.get_checkpoint(key)
    if link is not None:
        stored = app.db.get_feature_snapshot(str(link["snapshot_sha256"]))
        if stored is not None:
            return stored, str(link["snapshot_sha256"]), SNAPSHOT_LEGACY
    digest = app.db.add_feature_snapshot(computed)
    app.db.put_checkpoint(key, {"prediction_id": prediction_id, "snapshot_sha256": digest,
                                "origin": SNAPSHOT_LEGACY})
    return dict(computed), digest, SNAPSHOT_LEGACY


def predict(app: Any, symbol: str, as_of: str, target_session: str, timeframe: str, horizon: int,
            out: Path) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if ("T" in target_session and _as_of_cutoff_ns(target_session) <= _as_of_cutoff_ns(as_of)) or \
            ("T" not in target_session and target_session <= as_of[:10]):
        raise ValueError("target timestamp must be after the as-of cutoff")
    raw = Ingestor(app.s, app.db).ingest(symbol, use_minute=True)
    data = window_data(raw, end=as_of[:10])  # no later session can enter any feature
    cutoff_ns = _as_of_cutoff_ns(as_of)
    if len(data.minute):
        starts = pd.to_datetime(data.minute["start_ist"]).dt.tz_localize("Asia/Kolkata").astype("int64")
        minute = data.minute[starts.to_numpy(dtype=np.int64) + 60_000_000_000 <= cutoff_ns].reset_index(drop=True)
        if len(minute) != len(data.minute):
            data = SymbolData(data.symbol, data.daily, minute,
                              sha({"fp": data.fingerprint, "cutoff_ns": cutoff_ns})[:32],
                              {**data.summary, "prediction_cutoff_ns": cutoff_ns,
                               "minute_bars": len(minute)}, data.changed_files)
    bars = all_bars(data)
    if timeframe not in bars or not len(bars[timeframe]):
        raise ValueError(f"no accepted {timeframe} candles at or before {as_of}")
    lane = build_lane(data, timeframe, app.s.section("costs"), bars=bars,
                      cache_dir=app.s.cache_dir / "features")
    if lane is None or len(lane.market) <= horizon + 20:
        raise ValueError("insufficient accepted history for prediction")
    source_frame = bars[timeframe].frame
    clock = source_frame["avail_ns"] if timeframe == "1w" else source_frame["end_ns"]
    eligible = np.flatnonzero((clock.to_numpy(dtype=np.int64) <= cutoff_ns) &
                              ~source_frame["incomplete"].to_numpy(dtype=bool))
    if not len(eligible):
        raise ValueError(f"no completed {timeframe} candle at or before {as_of}")
    decision = int(eligible[-1])
    dna_cfg = app.s.section("m58").get("stock_dna", {})
    dna = discover_lane(lane, dna_cfg, [horizon])
    patterns = [p for p in dna["patterns"] if p["status"] in {"VALIDATED", "EXPERIMENTAL"}]
    cal_start = int(dna["calibration_start_index"])
    cal_p, cal_y, cal_ret = [], [], []
    cost = lane.costs.buy + lane.costs.sell
    for row in range(cal_start, decision - horizon + 1):
        p, support = _vote(patterns, lane, row)
        if not support:
            continue
        gross = float(lane.market.close[row + horizon] / lane.market.open[row + 1] - 1)
        if np.isfinite(gross):
            cal_p.append(p); cal_y.append(int(gross > 0)); cal_ret.append(gross - cost)
    pcfg = app.s.section("m58").get("prediction", {})
    calibrator = PlattCalibrator(int(pcfg.get("minimum_calibration_outcomes", 30))).fit(cal_p, cal_y)
    raw_p, support = _vote(patterns, lane, decision)
    calibrated = calibrator.status.startswith("PLATT") and bool(support)
    probability = float(calibrator.apply(np.array([raw_p]))[0]) if calibrated else None
    min_p = float(app.s.section("prediction").get("min_probability", 0.56))
    direction = "UNCERTAIN" if probability is None or max(probability, 1 - probability) < min_p else \
        ("UP" if probability >= 0.5 else "DOWN")
    comparable = np.asarray(cal_ret, dtype=float)
    # TRADE gate: long net return (after configured costs) of earlier calibration rows that would also
    # have been confident UP calls. A correct UP classification alone is not an executable trade.
    min_trade_n = int(pcfg.get("minimum_trade_gate_outcomes", 20))
    if calibrated and cal_p:
        cal_q = calibrator.apply(np.asarray(cal_p, dtype=float))
        similar = comparable[cal_q >= min_p]
    else:
        similar = np.zeros(0)
    trade_gate_net = float(np.mean(similar)) if len(similar) >= min_trade_n else None
    tradable = direction == "UP" and calibrated and trade_gate_net is not None and trade_gate_net > 0
    expected = float(np.mean(comparable)) if calibrated and len(comparable) >= 20 else None
    interval = ([float(np.quantile(comparable, 0.1)), float(np.quantile(comparable, 0.9))]
                if calibrated and len(comparable) >= 20 else None)
    decision_session = str(lane.market.sessions[decision])
    natural = json.dumps({"contract": CONTRACT, "symbol": symbol, "timeframe": timeframe, "as_of": as_of,
                          "decision": decision_session, "target": target_session, "horizon": horizon,
                          "data": lane.data_fingerprint, "dna": dna["feature_config_sha"]}, sort_keys=True)
    prediction_id = sha(natural)[:24]
    snapshot_cfg = app.s.section("feature_space").get("snapshot_indicators", {})
    if not snapshot_cfg:
        from .lanes import load_space
        snapshot_cfg = load_space().get("snapshot_indicators", {})
    snapshot = build_snapshot(lane, bars, data, decision, snapshot_cfg, patterns)
    snapshot, snapshot_sha, snapshot_origin = _resolve_snapshot(app, prediction_id, snapshot)
    payload = {"contract": CONTRACT, "requested_as_of": as_of, "decision_session": decision_session,
               "target_session": target_session, "cutoff_enforcement": "all data windowed before lane construction",
               "data_last_session": data.last_session, "dna_contract": DNA_CONTRACT,
               "dna_counts": dna["counts"], "dna_patterns_considered": len(patterns),
               "supporting_stock_dna": support[:20], "calibration_outcomes": len(cal_y),
               "direction_label": "gross price move (no costs)",
               "trade_gate": {"similar_confident_up_outcomes": int(len(similar)),
                              "minimum_outcomes": min_trade_n,
                              "mean_long_net_return_after_costs": trade_gate_net,
                              "round_trip_cost": cost},
               "feature_snapshot_sha256": snapshot_sha,
               "calibration": calibrator.status, "raw_probability_internal": raw_p,
               "probability_exposed": probability, "expected_net_return": expected,
               "forecast_return_range_p10_p90": interval, "forecast_price_range":
               ([float(lane.market.close[decision] * (1 + interval[0])), float(lane.market.close[decision] * (1 + interval[1]))]
                if interval else None), "abstained": direction == "UNCERTAIN",
               "evidence_quality": "CALIBRATED" if calibrated else "INSUFFICIENT_CALIBRATION",
               "risk": ["research forecast, not investment advice", "daily delivery shorting is not modelled",
                        "costs are configured assumptions"], "created_utc": utc_now()}
    inserted = app.db.add_prediction({
        "prediction_id": prediction_id, "natural_key": natural, "mode": "HISTORICAL_FROZEN",
        "replay_id": "m58-" + sha(natural)[:16], "symbol": symbol, "timeframe": timeframe,
        "lane_key": lane.key, "source_kind": "STOCK_DNA", "source_id": "DNA_ENSEMBLE",
        "source_version": CONTRACT, "horizon_bars": horizon, "decision_label": decision_session,
        "decision_ns": int(lane.market.end_ns[decision]), "target_session": target_session, "direction": direction,
        "prob_raw": raw_p, "prob_calibrated": probability, "calibration": calibrator.status,
        "actionable": int(tradable), "entry_ref": float(lane.market.close[decision]),
        "stop": None, "target": None, "cost_bps": cost * 1e4, "data_fingerprint": lane.data_fingerprint,
        "feature_config_sha": lane.feature_config_sha, "payload": payload})
    frozen = app.db.one("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,))
    assert frozen is not None
    frozen_payload = json.loads(frozen["payload_json"])
    stable = {"prediction_id": prediction_id,
              "symbol": symbol, "as_of": as_of, "decision_session": decision_session,
              "target_session": target_session, "direction": frozen["direction"],
              "probability": frozen["prob_calibrated"],
              "expected_net_return": frozen_payload.get("expected_net_return"),
              "forecast_price_range": frozen_payload.get("forecast_price_range"),
              "action": "TRADE" if frozen["actionable"] else "NO_TRADE",
              "evidence_quality": frozen_payload.get("evidence_quality", "INSUFFICIENT_CALIBRATION"),
              "supporting_patterns": frozen_payload.get("supporting_stock_dna", [])[:10],
              "data_fingerprint": frozen["data_fingerprint"], "feature_snapshot_sha256": snapshot_sha,
              "feature_snapshot_origin": snapshot_origin,
              "feature_snapshot_report": str(out / f"{prediction_id}.feature-snapshot.json")}
    _atomic_json(out / f"{prediction_id}.feature-snapshot.json", snapshot)
    if inserted or not (out / f"{prediction_id}.json").exists():
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
    idx = (np.flatnonzero(lane.market.end_ns == _as_of_cutoff_ns(target)) if "T" in target else
           np.flatnonzero(lane.market.sessions == target))
    if not len(idx):
        raise ValueError(f"actual target session {target} is not available; reveal remains blocked")
    t = int(idx[-1])
    decision_idx = np.flatnonzero(lane.market.end_ns == int(pred["decision_ns"]))
    if not len(decision_idx) or int(decision_idx[-1]) + 1 > t:
        raise ValueError("prediction decision/target sessions are not ordered in the accepted lane")
    entry = int(decision_idx[-1]) + 1
    gross = float(lane.market.close[t] / lane.market.open[entry] - 1)
    net = gross - lane.costs.buy - lane.costs.sell
    actual = "UP" if gross > 0 else "DOWN" if gross < 0 else "FLAT"  # direction is cost-free
    direction = pred["direction"]
    correct = None if direction == "UNCERTAIN" else int(direction == actual)
    prob = pred["prob_calibrated"]
    brier = None if prob is None else (float(prob) - int(gross > 0)) ** 2
    trade_net = net if pred["actionable"] and direction == "UP" else None
    payload = {"actual_open": float(lane.market.open[entry]), "actual_close": float(lane.market.close[t]),
               "gross_return": gross, "net_return_after_configured_costs": net, "actual_direction": actual,
               "frozen_prediction": {"direction": direction, "probability": prob},
               "revealed_from_data_fingerprint": lane.data_fingerprint}
    app.db.add_evaluation({"prediction_id": prediction_id, "outcome_label": target,
                           "outcome_ns": int(lane.market.end_ns[t]), "realized_return": net,
                           "up": int(gross > 0), "correct": correct, "brier": brier,
                           "trade_net_return": trade_net, "exit_reason": "TARGET_SESSION_CLOSE",
                           "bars_held": int(pred["horizon_bars"]), "payload_json": payload})
    return {"status": "REVEALED", "prediction_id": prediction_id, "target_session": target,
            "predicted_direction": direction, "actual_direction": actual, "correct": correct,
            "probability": prob, "brier": brier, "gross_return": gross, "net_return": net,
            "actual_open": payload["actual_open"], "actual_close": payload["actual_close"]}


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    revealed = [r for r in rows if r.get("realized_return") is not None]
    directional = [r for r in revealed if r["direction"] != "UNCERTAIN" and r.get("correct") is not None]
    probs = [r for r in revealed if r.get("prob_calibrated") is not None and r.get("brier") is not None]
    trades = [float(r["trade_net_return"]) for r in revealed if r.get("trade_net_return") is not None]
    bins = []
    for lo in np.arange(0, 1, 0.1):
        group = [r for r in probs if lo <= float(r["prob_calibrated"]) < lo + 0.1]
        if group:
            bins.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "n": len(group),
                         "mean_probability": float(np.mean([r["prob_calibrated"] for r in group])),
                         "observed_up_rate": float(np.mean([bool(r.get("up")) for r in group]))})
    return {"predictions": len(rows), "revealed": len(revealed),
            "correct_directional": sum(int(r["correct"] == 1) for r in directional),
            "incorrect_directional": sum(int(r["correct"] == 0) for r in directional),
            "directional_predictions": len(directional), "directional_accuracy_denominator": len(directional),
            "directional_accuracy": float(np.mean([r["correct"] for r in directional])) if directional else None,
            "abstentions": sum(r["direction"] == "UNCERTAIN" for r in rows),
            "abstention_rate": float(np.mean([r["direction"] == "UNCERTAIN" for r in rows])) if rows else None,
            "mean_brier": float(np.mean([r["brier"] for r in probs])) if probs else None,
            "brier_denominator": len(probs), "calibration_bins": bins, "trades": len(trades),
            "mean_trade_net_return": float(np.mean(trades)) if trades else None,
            "total_compounded_trade_return": float(np.prod(1 + np.asarray(trades)) - 1) if trades else None}


def evaluate(app: Any, symbol: str | None, out: Path, start: str | None = None, end: str | None = None,
             timeframe: str | None = None, horizon: int | None = None,
             run_name: str | None = None, prediction_ids: Sequence[str] | None = None) -> dict[str, Any]:
    where, params = "WHERE p.mode='HISTORICAL_FROZEN' AND p.source_version=?", [CONTRACT]
    if symbol:
        where += " AND p.symbol=?"; params.append(normalize_symbol(symbol))
    if start:
        where += " AND p.decision_label>=?"; params.append(start)
    if end:
        where += " AND p.decision_label<=?"; params.append(end)
    if timeframe:
        where += " AND p.timeframe=?"; params.append(timeframe)
    if horizon is not None:
        where += " AND p.horizon_bars=?"; params.append(horizon)
    if run_name and prediction_ids is None:
        run_report = app.s.reports_dir / "prediction-evaluation" / run_name / "walk-forward-summary.json"
        if not run_report.is_file():
            raise ValueError(f"named walk-forward report not found: {run_report}")
        run_doc = json.loads(run_report.read_text(encoding="utf-8"))
        prediction_ids = [str(step["prediction_id"]) for step in run_doc.get("steps", [])]
    if prediction_ids is not None:
        if not prediction_ids:
            where += " AND 0"
        else:
            where += f" AND p.prediction_id IN ({','.join('?' * len(prediction_ids))})"
            params.extend(prediction_ids)
    rows = app.db.q("SELECT p.*, e.correct, e.brier, e.trade_net_return, e.realized_return, e.up FROM predictions p "
                    "LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id " + where + " ORDER BY p.decision_ns",
                    params)
    mode = ("NAMED_RUN" if run_name or prediction_ids is not None else
            "FILTERED" if any((start, end, timeframe, horizon is not None)) else "CUMULATIVE")
    result = {"contract": CONTRACT, "mode": mode, "run_name": run_name,
              "filters": {"symbol": symbol, "start": start, "end": end,
                          "timeframe": timeframe, "horizon": horizon}, **_metrics(rows)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "prediction-evaluation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return {**result, "report": str(out / "prediction-evaluation.json")}


def _step(app: Any, prediction_id: str, creation_status: str, snapshot: Mapping[str, Any],
          snapshot_origin: str = SNAPSHOT_FROZEN) -> dict[str, Any]:
    row = app.db.one("SELECT p.*, e.correct, e.brier, e.trade_net_return, e.realized_return, e.up, "
                     "e.payload_json evaluation_payload FROM predictions p LEFT JOIN evaluations e "
                     "ON e.prediction_id=p.prediction_id WHERE p.prediction_id=?", (prediction_id,))
    assert row is not None
    pp = json.loads(row["payload_json"])
    ep = json.loads(row["evaluation_payload"]) if row.get("evaluation_payload") else {}
    prob = row["prob_calibrated"]
    abstained = row["direction"] == "UNCERTAIN"
    if not abstained:
        abstention_reason = None
    elif prob is None:
        abstention_reason = ("no supporting Stock-DNA pattern fired" if not pp.get("supporting_stock_dna") else
                             "insufficient untouched calibration evidence; no confidence exposed")
    else:
        abstention_reason = "calibrated confidence below the configured minimum probability"
    trade_reason = ("eligible under the configured research policy" if row["actionable"] else
                    f"abstained: {abstention_reason}" if abstained else
                    "short execution is not modelled" if row["direction"] == "DOWN" else
                    "directional classification is not an executable trade")
    decision_timestamp = pd.Timestamp(int(row["decision_ns"]), unit="ns", tz="UTC").tz_convert(
        "Asia/Kolkata").isoformat()
    return {"prediction_timestamp": decision_timestamp, "as_of_timestamp": pp.get("requested_as_of"),
            "target_timestamp": row["target_session"], "symbol": row["symbol"], "timeframe": row["timeframe"],
            "horizon": row["horizon_bars"], "prediction_id": prediction_id,
            "predicted_direction": row["direction"], "calibrated_probability": prob,
            "displayed_confidence": max(float(prob), 1 - float(prob)) if prob is not None else None,
            "abstained": abstained, "abstention_reason": abstention_reason,
            "trade_decision": "TRADE" if row["actionable"] else "NO_TRADE",
            "trade_reason": trade_reason,
            "actual_direction": ep.get("actual_direction"), "actual_gross_return": ep.get("gross_return"),
            "actual_net_return": row["realized_return"], "actual_trade_net_return": row["trade_net_return"],
            "correctness": ("NOT_APPLICABLE" if row["correct"] is None else
                            "CORRECT" if row["correct"] else "INCORRECT"),
            "prediction_status": "NEWLY_CREATED" if creation_status == "FROZEN" else "REUSED",
            "feature_snapshot_sha256": snapshot["snapshot_sha256"], "feature_snapshot_origin": snapshot_origin,
            "feature_snapshot": snapshot}


def _csv_row(step: Mapping[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in step.items() if k != "feature_snapshot"}
    snapshot = step["feature_snapshot"]
    for tf, frame in snapshot["timeframes"].items():
        row[f"{tf}_bar_end"] = frame.get("bar_end")
        for key, value in (frame.get("bar") or {}).items():
            row[f"{tf}_{key}"] = value
        for group in ("contextual_indicators", "prediction_features", "calculated_features"):
            for key, item in frame.get(group, {}).items():
                params = "_".join(str(v) for _, v in sorted(item.get("parameters", {}).items()))
                output = item.get("output")
                base = f"{tf}_{item.get('indicator', key)}" + (f"_{params}" if params else "")
                column = base if output == "value" else f"{base}_{output}"
                row[column.replace(".", "_").replace(":", "_")] = item.get("value")
                if item.get("reason"):
                    row[column.replace(".", "_").replace(":", "_") + "_reason"] = item["reason"]
    return row


def _write_steps(out: Path, steps: Sequence[Mapping[str, Any]]) -> None:
    (out / "walk-forward-steps.json").write_text(json.dumps(list(steps), indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    flat = [_csv_row(s) for s in steps]
    fields = list(dict.fromkeys(k for row in flat for k in row))
    with (out / "walk-forward-steps.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader(); writer.writerows(flat)


def format_step_table(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the intentionally concise console table; detailed evidence stays in snapshot artifacts."""
    fmt = "{:<18}   {:<10}   {:<11}   {:<6}   {:<8}   {:<7}   {}"
    lines = [fmt.format("Time", "Prediction", "Probability", "Actual", "Return", "Correct", "Trade")]
    for step in steps:
        stamp = pd.Timestamp(step["prediction_timestamp"])
        time = (stamp.strftime("%Y-%m-%d") if step.get("timeframe", "1d") in {"1d", "1w"}
                else stamp.strftime("%Y-%m-%d %H:%M"))
        conf = step["displayed_confidence"]
        prob = "—" if conf is None else f"{conf:.0%}"  # calibrated, for the predicted side only
        gross = step.get("actual_gross_return")
        ret = "—" if gross is None else f"{gross:+.2%}"
        correct = {"CORRECT": "YES", "INCORRECT": "NO"}.get(step["correctness"], "—")
        lines.append(fmt.format(time, step["predicted_direction"], prob, step["actual_direction"] or "—",
                                ret, correct, step["trade_decision"]))
    return lines


def _fmt(value: Any, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def format_run_totals(result: Mapping[str, Any]) -> list[str]:
    """Run-specific totals; every figure is computed from this invocation's steps only."""
    e = result["evaluation"]
    return [
        f"Run totals ({result['symbol']} {result['timeframe']} h={result['horizon']} "
        f"{result['start']}..{result['end']}; this invocation only, not cumulative):",
        f"  steps planned {result['planned']}, completed {result['steps_done']}, errors {len(result['errors'])}",
        f"  predictions newly created {result['newly_frozen']}, reused {result['reused_predictions']}; "
        f"revealed outcomes {e['revealed']}",
        f"  directional classifications: correct {e['correct_directional']}, incorrect "
        f"{e['incorrect_directional']}, accuracy {_fmt(e['directional_accuracy'], '.3f')} "
        f"(n={e['directional_accuracy_denominator']}); abstentions (UNCERTAIN) {e['abstentions']}",
        f"  Brier score {_fmt(e['mean_brier'], '.4f')} (n={e['brier_denominator']}; calibrated probabilities only)",
        f"  executable trades {e['trades']}; mean net trade return {_fmt(e['mean_trade_net_return'], '+.3%')}; "
        f"compounded net trade return {_fmt(e['total_compounded_trade_return'], '+.3%')}",
        "  Directional classifications are not trades; only TRADE rows are executable under the research policy.",
    ]


def format_step_details(step: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    """Per-step evidence from the frozen snapshot. Indicators are serialized from the snapshot itself
    (declarative configuration), never from a renderer-side list."""
    lines = [f"  Prediction: {step['predicted_direction']}  (snapshot {step['feature_snapshot_sha256'][:12]}, "
             f"{step.get('feature_snapshot_origin', SNAPSHOT_FROZEN)})", "  Evidence:"]
    for tf, frame in snapshot.get("timeframes", {}).items():
        if frame.get("calculation_status") != "COMPLETED":
            lines.append(f"    {tf}: unavailable ({frame.get('reason')})")
            continue
        lines.append(f"    {tf} bar end {frame.get('bar_end')}: close {_fmt((frame.get('bar') or {}).get('close'), ',.2f')}")
        for name, item in frame.get("contextual_indicators", {}).items():
            value = item.get("value")
            lines.append(f"    {tf} {name}: " + (f"{value:,.4f}" if value is not None else f"null ({item.get('reason')})"))
    fired = snapshot.get("patterns", {}).get("fired", [])
    lines.append(f"    Fired patterns: {', '.join(str(p['pattern_id']) for p in fired) or 'none'}")
    lines.append(f"    Trade decision: {step['trade_decision']}")
    lines.append(f"    Reason: {step['trade_reason']}")
    return lines


def walk_forward(app: Any, symbol: str, start: str, end: str, timeframe: str, horizon: int, out: Path,
                 max_steps: int | None = None, run_name: str | None = None) -> dict[str, Any]:
    """Chronological freeze-then-reveal loop. Natural keys make reruns a deterministic resume."""
    symbol = normalize_symbol(symbol)
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    data = Ingestor(app.s, app.db).ingest(symbol, use_minute=True)
    if timeframe == "1d":
        sessions = [str(x) for x in data.daily["date"].tolist()]
        plan = [(sessions[i], sessions[i + horizon]) for i in range(len(sessions) - horizon)
                if start <= sessions[i] <= end]
    else:
        frame = all_bars(data).get(timeframe)
        if frame is None or not len(frame):
            raise ValueError(f"no accepted {timeframe} candles for the requested walk-forward")
        f = frame.frame
        eligible = np.flatnonzero(~f["incomplete"].to_numpy(dtype=bool))
        plan = []
        for pos, row in enumerate(eligible[:-horizon] if horizon else eligible):
            target_row = eligible[pos + horizon]
            session = str(f["session"].iloc[row])
            if start <= session <= end:
                as_of = pd.Timestamp(int(f["end_ns"].iloc[row]), unit="ns", tz="UTC").tz_convert("Asia/Kolkata").isoformat()
                target = pd.Timestamp(int(f["end_ns"].iloc[target_row]), unit="ns", tz="UTC").tz_convert("Asia/Kolkata").isoformat()
                plan.append((as_of, target))
    if max_steps is not None:
        plan = plan[:max_steps]
    done = frozen = revealed = 0
    steps: list[dict[str, Any]] = []
    errors = []
    for as_of, target in plan:
        try:
            pred = predict(app, symbol, as_of, target, timeframe, horizon, out / "frozen")
            frozen += int(pred["status"] == "FROZEN")
            actual = reveal(app, pred["prediction_id"])
            revealed += int(actual["status"] == "REVEALED")
            snapshot = app.db.get_feature_snapshot(pred["feature_snapshot_sha256"])
            if snapshot is None:
                raise ValueError("frozen feature snapshot is missing from the content-addressed store")
            steps.append(_step(app, pred["prediction_id"], pred["status"], snapshot,
                               pred.get("feature_snapshot_origin", SNAPSHOT_FROZEN)))
            done += 1
        except Exception as exc:
            errors.append({"as_of": as_of, "target": target, "error": f"{type(exc).__name__}: {exc}"})
    summary = evaluate(app, symbol, out, start=start, end=end, timeframe=timeframe, horizon=horizon,
                       run_name=run_name, prediction_ids=[s["prediction_id"] for s in steps])
    _write_steps(out, steps)
    displayed = [{k: v for k, v in step.items() if k != "feature_snapshot"} for step in steps]
    result = {"status": "COMPLETED" if not errors else "PARTIAL", "run_name": run_name,
              "symbol": symbol, "timeframe": timeframe, "horizon": horizon, "start": start, "end": end,
              "planned": len(plan),
              "steps_done": done, "newly_frozen": frozen, "newly_revealed": revealed,
              "reused_predictions": done - frozen, "revealed_outcomes": summary["revealed"],
              "errors": errors, "steps": displayed, "evaluation": summary,
              "steps_json": str(out / "walk-forward-steps.json"),
              "steps_csv": str(out / "walk-forward-steps.csv")}
    (out / "walk-forward-summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                                     encoding="utf-8")
    return result


__all__ = ["CONTRACT", "evaluate", "format_run_totals", "format_step_details", "format_step_table", "predict",
           "reveal", "walk_forward"]
