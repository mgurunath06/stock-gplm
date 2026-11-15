"""Reports and audit traces built from the database (HTML, CSV and JSON; no external assets).

Metric vocabulary (kept separate everywhere):

* **training accuracy**  weighted accuracy of the last fit on its own training rows (in-sample);
* **sequential OOS accuracy**  accuracy of REPLAY + LIVE predictions evaluated after their outcome
  window completed; every prediction was stored before its outcome existed at the replay clock;
* **model-selection performance**  in-sample statistics used to choose rules or champions;
* **trading performance**  simulated trade returns after the configured cost assumptions, for
  predictions that were actionable when issued;
* **live forward performance**  the subset of evaluated predictions whose mode is ``LIVE``.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import CLASSIFICATION
from .db import AuditDB, utc_now


def accuracy_table(db: AuditDB, symbols: Sequence[str] | None = None) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []
    if symbols:
        where = f"WHERE p.symbol IN ({','.join('?' * len(symbols))})"
        params = list(symbols)
    rows = db.q(f"""
      SELECT p.lane_key, p.symbol, p.timeframe, p.source_kind, p.source_id, p.horizon_bars, p.replay_id,
             COUNT(p.prediction_id) AS predictions, COUNT(e.correct) AS evaluated, AVG(e.correct) AS oos_accuracy,
             AVG(e.brier) AS oos_brier, SUM(CASE WHEN p.mode='LIVE' THEN 1 ELSE 0 END) AS live_predictions,
             AVG(CASE WHEN p.mode='LIVE' THEN e.correct END) AS live_accuracy,
             SUM(p.actionable) AS actionable, COUNT(CASE WHEN p.actionable=1 THEN e.trade_net_return END) AS actionable_trades,
             AVG(CASE WHEN p.actionable=1 THEN e.trade_net_return END) AS actionable_mean_net,
             AVG(e.trade_net_return) AS all_signals_mean_net, AVG(e.up) AS base_rate_up
      FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id {where}
      GROUP BY p.lane_key, p.source_kind, p.source_id, p.horizon_bars, p.replay_id
      ORDER BY p.lane_key, p.source_kind, p.horizon_bars, p.source_id""", params)
    for r in rows:
        last = db.q("SELECT e.correct FROM predictions p JOIN evaluations e ON e.prediction_id=p.prediction_id "
                    "WHERE p.replay_id=? AND p.lane_key=? AND p.source_kind=? AND p.source_id=? AND p.horizon_bars=? "
                    "AND e.correct IS NOT NULL ORDER BY p.decision_ns DESC LIMIT 20",
                    (r["replay_id"], r["lane_key"], r["source_kind"], r["source_id"], r["horizon_bars"]))
        r["rolling20_accuracy"] = (sum(x["correct"] for x in last) / len(last)) if last else None
        if r["source_kind"] == "MODEL":
            th = db.one("SELECT train_acc, loss, rows, device FROM training_history WHERE lane_key=? AND model_kind=? "
                        "AND horizon=? ORDER BY id DESC LIMIT 1", (r["lane_key"], r["source_id"], r["horizon_bars"]))
            r["training_accuracy_last_fit"] = th["train_acc"] if th else None
            r["training_device"] = th["device"] if th else None
            ch = db.one("SELECT model_kind FROM champions WHERE lane_key=? AND horizon=?",
                        (f"{r['lane_key']}|{r['replay_id']}", r["horizon_bars"]))
            r["champion"] = bool(ch and ch["model_kind"] == r["source_id"])
        if r["base_rate_up"] is not None and r["oos_accuracy"] is not None:
            r["accuracy_minus_majority"] = r["oos_accuracy"] - max(r["base_rate_up"], 1 - r["base_rate_up"])
    return rows


def live_forecasts(db: AuditDB, symbols: Sequence[str], session: str | None = None) -> list[dict[str, Any]]:
    out = []
    for sym in symbols:
        last = db.one("SELECT MAX(decision_ns) d FROM predictions WHERE symbol=? AND mode='LIVE'", (sym,))
        if not last or last["d"] is None:
            out.append({"symbol": sym, "status": "NO_LIVE_FORECAST", "candidates": [], "actionable": [],
                        "decision_label": None, "no_trade_reason": "no live forecast yet (run gplm daily)"})
            continue
        rows = db.q("SELECT * FROM predictions WHERE symbol=? AND mode='LIVE' AND decision_ns=? ORDER BY actionable DESC, "
                    "COALESCE(prob_calibrated, prob_raw) DESC", (sym, last["d"]))
        cands = []
        for r in rows:
            p = json.loads(r["payload_json"])
            cands.append({**{k: r[k] for k in ("prediction_id", "symbol", "timeframe", "source_kind", "source_id",
                                               "source_version", "horizon_bars", "decision_label", "target_session",
                                               "direction", "prob_raw", "prob_calibrated", "calibration", "actionable",
                                               "entry_ref", "stop", "target", "cost_bps", "created_utc")},
                          "horizon_label": p.get("horizon_label"), "expected_rr": p.get("expected_rr"),
                          "expected_r_multiple": p.get("expected_r_multiple"), "historical_oos": p.get("historical_oos"),
                          "supporting_indicators": p.get("supporting_indicators") or p.get("text"),
                          "market_conditions": p.get("market_conditions"), "reasons": p.get("reasons"),
                          "not_actionable_because": p.get("not_actionable_because"),
                          "risk_warnings": p.get("risk_warnings"), "entry_rule": p.get("entry_rule"),
                          "target_session_estimate": p.get("target_session_estimate"),
                          "train_acc_last_fit": p.get("train_acc_last_fit")})
        act = [c for c in cands if c["actionable"]]
        out.append({"symbol": sym, "decision_label": rows[0]["decision_label"] if rows else None,
                    "status": "CANDIDATES" if act else "NO_TRADE",
                    "no_trade_reason": None if act else "no prediction passed every gate (champion model, calibrated "
                    "probability, rolling outcomes, positive expected R, long-only delivery)",
                    "actionable": act, "candidates": cands})
    return out


def _table(rows: Sequence[Mapping[str, Any]], cols: Sequence[str]) -> str:
    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.4f}"
        if isinstance(v, (list, dict)):
            return html.escape(json.dumps(v, default=str)[:300])
        return html.escape("" if v is None else str(v))

    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{cell(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


CSS = """:root{--bg:#fbfbfa;--fg:#1d1d1b;--mut:#6b6b66;--line:#e3e2dd;--acc:#2f5d8a;--ok:#2e7d4f;--warn:#a15c00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#a3a29c;--line:#34332f;--acc:#7fb0e0;--ok:#6cc08b;--warn:#f0a64a}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0 auto;max-width:1200px;padding:16px}
h1{font-size:20px;margin:4px 0}h2{font-size:16px;margin:22px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}
.mut{color:var(--mut)}table{border-collapse:collapse;width:100%;font-size:12.5px;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;white-space:nowrap}th{color:var(--mut);font-weight:600}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}.ok{color:var(--ok)}.warn{color:var(--warn)}"""


def write_daily(out_dir: Path, summary: Mapping[str, Any], accuracy: Sequence[Mapping[str, Any]],
                forecasts: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out_dir / "accuracy.json").write_text(json.dumps(list(accuracy), indent=1, default=str), encoding="utf-8")
    (out_dir / "forecasts.json").write_text(json.dumps(list(forecasts), indent=1, default=str), encoding="utf-8")
    flat = [c for f in forecasts for c in f["candidates"]]
    for name, rows in (("accuracy.csv", accuracy), ("forecasts.csv", flat)):
        buf = io.StringIO()
        if rows:
            cols = list(rows[0].keys())
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v for k, v in r.items()})
        (out_dir / name).write_text(buf.getvalue(), encoding="utf-8")
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>GPLM daily report</title><style>{CSS}</style></head><body>",
             f"<h1>GPLM daily report — latest completed session {html.escape(str(summary.get('latest_session')))}</h1>",
             f"<p class='mut'>{CLASSIFICATION}. Generated {utc_now()} by run {html.escape(str(summary.get('run_id')))} "
             f"on {html.escape(str(summary.get('device')))}. Predictions are research forecasts, not orders or advice.</p>"]
    parts.append("<h2>Next-session forecasts</h2>")
    for f in forecasts:
        status = f["status"]
        cls = "ok" if status == "CANDIDATES" else "warn"
        parts.append(f"<div class='card'><b>{html.escape(f['symbol'])}</b> <span class='{cls}'>{status}</span> "
                     f"<span class='mut'>decision bar {html.escape(str(f.get('decision_label')))}</span>")
        if f.get("no_trade_reason"):
            parts.append(f"<div class='mut'>{html.escape(f['no_trade_reason'])}</div>")
        show = f["actionable"] or [c for c in f["candidates"] if c["source_kind"] == "MODEL"][:6]
        parts.append(_table(show, ["source_kind", "source_id", "horizon_label", "direction", "prob_calibrated",
                                   "calibration", "entry_rule", "entry_ref", "stop", "target", "expected_rr",
                                   "cost_bps", "actionable", "not_actionable_because"]))
        parts.append("</div>")
    parts.append("<h2>Sequential out-of-sample accuracy (stored before outcomes; evaluated after)</h2>")
    parts.append(_table(accuracy, ["lane_key", "source_kind", "source_id", "horizon_bars", "champion", "predictions",
                                   "evaluated", "oos_accuracy", "rolling20_accuracy", "base_rate_up",
                                   "accuracy_minus_majority", "oos_brier", "training_accuracy_last_fit",
                                   "live_predictions", "live_accuracy", "actionable", "actionable_trades",
                                   "actionable_mean_net", "all_signals_mean_net"]))
    parts.append("<h2>Run summary</h2><pre>" + html.escape(json.dumps({k: v for k, v in summary.items()
                                                                        if k not in ("lanes",)}, indent=1, default=str)[:20000]) + "</pre>")
    parts.append("<h2>Lanes</h2>" + _table(summary.get("lanes", []), ["lane", "replay_id", "steps_done", "steps_total",
                                                                     "first_session", "last_session", "predictions_new",
                                                                     "evaluations_new", "rule_searches", "seconds",
                                                                     "skipped_horizons"]))
    parts.append("</body></html>")
    (out_dir / "report.html").write_text("".join(parts), encoding="utf-8")
    return {k: str(out_dir / k) for k in ("report.html", "summary.json", "accuracy.csv", "forecasts.csv",
                                          "forecasts.json", "accuracy.json")}


def trace_prediction(db: AuditDB, prediction_id: str) -> dict[str, Any]:
    """Reconstruct everything that produced one prediction and everything that evaluated it."""
    p = db.one("SELECT * FROM predictions WHERE prediction_id=? OR prediction_id LIKE ?", (prediction_id, f"{prediction_id}%"))
    if p is None:
        raise KeyError(f"no prediction {prediction_id}")
    payload = json.loads(p["payload_json"])
    run = db.one("SELECT * FROM runs WHERE run_id=?", (p["run_id"],))
    ev = db.one("SELECT * FROM evaluations WHERE prediction_id=?", (p["prediction_id"],))
    session = p["decision_label"][:10]
    files = db.q("SELECT path, timeframe, period, sha256, decision FROM data_files WHERE symbol=? AND "
                 "substr(period,1,10) <= ? ORDER BY timeframe, period", (p["symbol"], session))
    cache = db.one("SELECT * FROM candle_cache WHERE symbol=?", (p["symbol"],))
    model = None
    if p["source_kind"] == "MODEL":
        mid = payload.get("model_id")
        model = db.one("SELECT * FROM models WHERE model_id=?", (mid,)) if mid else None
        if model:  # verify the artifact still matches its recorded hash
            try:
                model["artifact_sha256_ok"] = hashlib.sha256(Path(model["path"]).read_bytes()).hexdigest() == model["sha256"]
            except OSError:
                model["artifact_sha256_ok"] = False
    training = db.q("SELECT step_label, epoch, loss, train_acc, rows, device, seconds FROM training_history WHERE "
                    "lane_key=? AND model_kind=? AND horizon=? AND step_label=? ORDER BY id",
                    (p["lane_key"], p["source_id"], p["horizon_bars"],
                     payload.get("step_session") or session)) if p["source_kind"] == "MODEL" else []
    strategy = db.one("SELECT * FROM strategies WHERE strategy_id=? AND version=?",
                      (p["source_id"], int(p["source_version"]))) if p["source_kind"] == "STRATEGY" else None
    rule_search = None
    if p["source_kind"] == "RULE":
        for e in db.q("SELECT * FROM events WHERE kind='RULE_SEARCH' AND subject=? ORDER BY seq DESC", (p["lane_key"],)):
            body = json.loads(e["payload_json"])
            if any(r["rule_id"] == p["source_id"] for r in body.get("selected", [])) and body.get("replay_id") == p["replay_id"]:
                rule_search = {"seq": e["seq"], "ts_utc": e["ts_utc"], **body}
                break
    cfg = db.one("SELECT sha256, path, first_seen_utc FROM config_snapshots WHERE sha256=?", (run["config_sha"],)) if run else None
    return {"prediction": {**{k: v for k, v in p.items() if k != "payload_json"}, "payload": payload},
            "evaluation": ev and {**{k: v for k, v in ev.items() if k != "payload_json"},
                                  "payload": json.loads(ev["payload_json"] or "{}")},
            "prediction_run": run, "config_snapshot": cfg,
            "input_data": {"canonical_candles": cache and {k: cache[k] for k in ("fingerprint", "path", "first", "last")},
                           "source_files_up_to_decision": files, "data_fingerprint": p["data_fingerprint"]},
            "feature_config_sha": p["feature_config_sha"], "model_artifact": model, "training_step": training,
            "strategy_version": strategy, "rule_search": rule_search,
            "integrity": {"prediction_row_append_only": True, "event_chain": db.verify_chain()}}


def trace_run(db: AuditDB, run_id: str) -> dict[str, Any]:
    run = db.one("SELECT * FROM runs WHERE run_id=? OR run_id LIKE ?", (run_id, f"{run_id}%"))
    if run is None:
        raise KeyError(f"no run {run_id}")
    rid = run["run_id"]
    return {"run": run,
            "events_by_kind": db.q("SELECT kind, level, COUNT(*) n FROM events WHERE run_id=? GROUP BY kind, level ORDER BY n DESC", (rid,)),
            "errors": db.q("SELECT seq, ts_utc, kind, subject, payload_json FROM events WHERE run_id=? AND level IN ('ERROR','WARN') ORDER BY seq LIMIT 200", (rid,)),
            "predictions": db.one("SELECT COUNT(*) n, SUM(mode='LIVE') live, SUM(actionable) actionable FROM predictions WHERE run_id=?", (rid,)),
            "evaluations": db.one("SELECT COUNT(*) n, AVG(correct) acc FROM evaluations WHERE run_id=?", (rid,)),
            "training_rows": db.one("SELECT COUNT(*) n FROM training_history WHERE run_id=?", (rid,)),
            "models_saved": db.one("SELECT COUNT(*) n FROM models WHERE run_id=?", (rid,)),
            "strategies_created": db.q("SELECT strategy_id, version, name, origin, active FROM strategies s WHERE EXISTS "
                                       "(SELECT 1 FROM events e WHERE e.run_id=? AND e.kind='STRATEGY_VERSION_CREATED' AND e.subject=s.strategy_id)", (rid,)),
            "data_files_touched": db.one("SELECT COUNT(*) n FROM data_files WHERE run_id=?", (rid,)),
            "api_calls": db.q("SELECT * FROM api_calls WHERE run_id=?", (rid,)),
            "checkpoints": db.q("SELECT key, updated_utc FROM checkpoints WHERE run_id=?", (rid,)),
            "chain": db.verify_chain()}
