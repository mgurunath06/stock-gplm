"""``gplm daily``: the one evening command.

1. Optionally refresh data (only when ``refresh.enabled`` and ``--refresh``; the configured local
   collector command is run and logged; GPLM never handles provider credentials).
2. Ingest new or changed local files (unchanged files are not parsed again).
3. Detect the latest completed session across the requested stocks.
4. Advance every lane's rolling replay to that session: evaluate earlier forecasts whose outcomes
   are now observable, update/retrain models, re-search rules, compare challengers with the
   accepted champion, and issue the next-session (LIVE) forecasts.
5. Write the report (HTML, CSV, JSON), back up the database and print a short summary.

Re-running with unchanged data inserts no predictions, evaluations, fits or API calls: every step
is checkpointed and every prediction has a natural key.
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .compute import Backend
from .context import App
from .db import utc_now
from .ingest import Ingestor, normalize_symbol
from .lanes import all_bars, build_lane
from .library import Library, seed_templates
from .report import accuracy_table, live_forecasts, write_daily
from .rolling import LaneReplay
from .settings import REPO

IST = timezone(timedelta(hours=5, minutes=30))


def refresh(app: App) -> dict[str, Any]:
    cfg = app.s.section("refresh")
    if not cfg.get("enabled") or not cfg.get("command"):
        return {"status": "SKIPPED", "reason": "refresh disabled in config (refresh.enabled=false or no command)"}
    cmd = [str(c) for c in cfg["command"]]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(cfg.get("timeout_s", 3600)), check=False)
        ok, code, err = proc.returncode == 0, proc.returncode, (proc.stderr or "")[-500:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, code, err = False, None, f"{type(exc).__name__}: {exc}"
    ms = (time.monotonic() - t0) * 1000
    with app.db.tx() as c:
        c.execute("INSERT INTO api_calls (ts_utc, provider, model, endpoint, purpose, request_id, input_tokens, "
                  "output_tokens, cost_usd, cost_basis, latency_ms, success, error, run_id, subject) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (utc_now(), cfg.get("provider", "local-collector"), None, cmd[0], "data refresh before daily run",
                   None, None, None, None, "NOT_APPLICABLE (local command)", ms, int(ok), err if not ok else None,
                   app.run_id, "data"))
    app.db.event("DATA_REFRESH", {"command": cmd, "exit_code": code, "seconds": round(ms / 1000, 1), "ok": ok},
                 level="INFO" if ok else "ERROR")
    return {"status": "OK" if ok else "FAILED", "exit_code": code, "seconds": round(ms / 1000, 1)}


def ensure_templates(app: App) -> None:
    tdir = REPO / "config" / "app" / "strategies"
    if tdir.is_dir():
        seed_templates(Library(app.s.strategies_dir, app.db), tdir)


def run_daily(app: App, symbols: Sequence[str], backend: Backend, *, do_refresh: bool = False,
              intraday: bool = True, start: str | None = None, max_steps: int | None = None,
              horizons: Sequence[int] | None = None) -> dict[str, Any]:
    s = app.s
    prog = app.progress
    cfg = dict(s.raw)
    if start:
        cfg["rolling"] = {**cfg["rolling"], "start": start}
    summary: dict[str, Any] = {"run_id": app.run_id, "device": backend.label, "device_reason": backend.reason,
                               "requested_symbols": list(symbols), "started_utc": utc_now()}
    summary["refresh"] = refresh(app) if do_refresh else {"status": "NOT_REQUESTED"}
    ensure_templates(app)
    lib = Library(s.strategies_dir, app.db)
    ing = Ingestor(s, app.db)
    datas = {}
    prog.update(force=True, task="ingest", device=backend.name)
    for sym in symbols:
        prog.update(stock=sym, note="incremental ingest")
        d = ing.ingest(sym)
        datas[d.symbol] = d
    latest = max((d.last_session for d in datas.values() if d.last_session), default=None)
    summary["latest_session"] = latest
    summary["data"] = {sym: {"last_session": d.last_session, "stale": d.last_session != latest,
                             "changed_files": d.changed_files, "first": d.summary.get("first"),
                             "minute_sessions": d.summary.get("minute_sessions_used"),
                             "minute_last": d.summary.get("minute_last"),
                             "minute_derived_daily_rows": d.summary.get("minute_derived_rows"),
                             "last_bar_source": str(d.daily["source"].iloc[-1]) if len(d.daily) else None}
                       for sym, d in datas.items()}
    for sym, info in summary["data"].items():
        if info["stale"]:
            prog.warn(f"{sym}: latest session {info['last_session']} is older than {latest}")
            app.db.event("DATA_STALE", {"symbol": sym, **info, "latest": latest}, subject=sym, level="WARN")
    tf_cfg = s.section("timeframes")
    plan = []
    for sym, d in datas.items():
        bars = all_bars(d)
        lanes = [tf_cfg.get("daily_lane", "1d")]
        if intraday:
            for tf in tf_cfg.get("intraday_lanes", []):
                if tf in bars and bars[tf].sessions() >= int(tf_cfg.get("intraday_min_sessions", 60)):
                    lanes.append(tf)
                else:
                    app.db.event("LANE_SKIPPED", {"symbol": sym, "timeframe": tf, "sessions": bars[tf].sessions()
                                                  if tf in bars else 0, "reason": "too few minute sessions"},
                                 subject=sym)
        for tf in lanes:
            plan.append((sym, d, tf, bars))
    prog.set_total(0)
    lane_objs = []
    for sym, d, tf, bars in plan:
        lane = build_lane(d, tf, s.section("costs"), bars=bars, cache_dir=s.cache_dir / "features")
        if lane is None:
            continue
        hz = list(horizons) if horizons else list(s.section("horizons").get(tf, [1, 5, 10, 20]))
        rule_h = [h for h in hz if h <= 20] if tf == "1d" else hz
        rp = LaneReplay(app.db, lane, hz, cfg, backend, prog, s.models_dir, lib, bars, rule_horizons=rule_h,
                        max_steps=max_steps)
        lane_objs.append(rp)
    total_steps = 0
    for rp in lane_objs:
        ck = app.db.get_checkpoint(f"replay:{rp.base_replay_id}") or {}
        done = ck.get("last_session")
        steps = rp._steps()
        total_steps += len([1 for st, _ in steps if done is None or st > done]) if max_steps is None else \
            min(max_steps, len([1 for st, _ in steps if done is None or st > done]))
    prog.set_total(total_steps)
    lane_results = []
    for rp in lane_objs:
        prog.update(force=True, task="replay", stock=rp.lane.symbol, timeframe=rp.lane.timeframe)
        try:
            lane_results.append(rp.run())
        except Exception as exc:
            prog.error(f"{rp.lane.key}: {type(exc).__name__}: {exc}")
            app.db.event("LANE_FAILED", {"lane": rp.lane.key, "error": f"{type(exc).__name__}: {exc}"},
                         subject=rp.lane.key, level="ERROR")
            lane_results.append({"lane": rp.lane.key, "error": f"{type(exc).__name__}: {exc}"})
    summary["lanes"] = lane_results
    failures = [r for r in lane_results if r.get("error")]
    succeeded = len(lane_results) - len(failures)
    summary["lane_counts"] = {"total": len(lane_results), "succeeded": succeeded, "failed": len(failures)}
    summary["lane_failures"] = [{"lane": r.get("lane"), "error": r.get("error")} for r in failures]
    summary["status"] = "FAILED" if failures and not succeeded else ("PARTIAL" if failures else "COMPLETED")
    summary["new_predictions"] = sum(r.get("predictions_new", 0) for r in lane_results)
    summary["new_evaluations"] = sum(r.get("evaluations_new", 0) for r in lane_results)
    summary["model_fits"] = sum(r.get("fits", 0) for r in lane_results)
    summary["device_stats"] = {k: round(v, 3) if isinstance(v, float) else v for k, v in backend.stats.items()}
    trained = app.db.q("SELECT device, COUNT(DISTINCT model_kind || '|' || lane_key || '|' || horizon || '|' || step_label) "
                       "fits FROM training_history WHERE run_id=? GROUP BY device ORDER BY device", (app.run_id,))
    summary["compute_attribution"] = {
        "selected_backend": backend.label,
        "training_devices": trained,
        "numerical_devices": (["cpu (NumPy: ingestion, features, inference and reporting)"] +
                              ([backend.label] if backend.stats.get("gpu_calls", 0) else [])),
        "gpu_calls": int(backend.stats.get("gpu_calls", 0)),
        "note": "No training device is reported when this run fitted no models.",
    }
    syms = list(datas)
    acc = accuracy_table(app.db, syms)
    fc = live_forecasts(app.db, syms)
    summary["forecast_status"] = {f["symbol"]: f["status"] for f in fc}
    summary["warnings"] = prog.state.get("warnings", 0)
    summary["errors"] = prog.state.get("errors", 0)
    out_dir = s.reports_dir / "daily" / str(latest or "no-data")
    files = write_daily(out_dir, summary, acc, fc)
    latest_dir = s.reports_dir / "latest"
    write_daily(latest_dir, summary, acc, fc)
    summary["report_files"] = files
    backup = app.db.backup(s.backups_dir, keep=int(s.section("retention").get("keep_backups", 10)))
    summary["backup"] = str(backup)
    app.db.event("DAILY_REPORT", {"latest_session": latest, "files": files, "backup": str(backup),
                                  "status": summary["status"], "lane_counts": summary["lane_counts"],
                                  "new_predictions": summary["new_predictions"],
                                  "new_evaluations": summary["new_evaluations"]},
                 level="ERROR" if summary["status"] == "FAILED" else
                       ("WARN" if summary["status"] == "PARTIAL" else "INFO"))
    return summary


def default_symbols(text: str | None) -> list[str]:
    if not text:
        raise SystemExit("give --stocks SYMBOL[,SYMBOL...] (for example --stocks RELIANCE,HDFCBANK,ICICIBANK)")
    return [normalize_symbol(x) for x in text.split(",") if x.strip()]


def now_ist() -> datetime:
    return datetime.now(IST)


__all__ = ["run_daily", "refresh", "default_symbols", "Path"]
