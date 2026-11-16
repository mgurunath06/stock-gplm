"""``gplm daily``: the one evening command.

1. Optionally refresh data (only when ``refresh.enabled`` and ``--refresh``; the configured local
   collector command is run and logged; GPLM never handles provider credentials).
2. Ingest new or changed local files (unchanged files are not parsed again).
3. Establish the expected completed exchange session (``--as-of`` or the registered calendar) and
   check every symbol's freshness against it absolutely -- never against the newest symbol in the
   batch. A stale, incomplete, quarantined or calendar-unknown symbol can never produce LIVE output.
4. Advance every lane's rolling replay to that session: evaluate earlier forecasts whose outcomes
   are now observable, update/retrain models, re-search rules, compare challengers with the
   accepted champion, and issue the latest-bar forecasts (LIVE only when every M5.9A gate passes;
   otherwise SHADOW / NO_TRADE / UNAVAILABLE with reasons).
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
from .contracts import STALE_DATA, Freshness, evaluate_freshness
from .db import utc_now
from .ingest import Ingestor, normalize_symbol
from .lanes import all_bars, build_lane
from .library import Library, seed_templates
from .report import accuracy_table, live_forecasts, write_daily
from .rolling import LaneReplay
from .session_calendar import expected_completed_session, load_registered_calendar
from .governance import cost_policy_version, universe_membership
from .settings import REPO
from .targets import grid_position

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


def symbol_freshness(app: App, symbol: str, last_session: str | None, expected: str | None,
                     confirmed: bool) -> Freshness:
    """Absolute freshness of one symbol. An ERROR data issue on the last session quarantines it."""
    quarantined = bool(last_session) and app.db.one(
        "SELECT 1 FROM data_issues WHERE symbol=? AND severity='ERROR' AND instr(period, ?) > 0 LIMIT 1",
        (symbol, last_session)) is not None
    return evaluate_freshness(last_session, expected, calendar_confirmed=confirmed, quarantined=quarantined)


def intraday_freshness(base: Freshness, lane: Any, now: datetime | None) -> Freshness:
    """Within a session in progress the latest completed grid bar is the expected bar: if the lane's latest
    eligible bar ends earlier, the lane is STALE_DATA even though the session date matches."""
    if not base.fresh or now is None or not lane.intraday or not len(lane.market):
        return base
    m = lane.market
    inc = lane.bars.frame["incomplete"].to_numpy(dtype=bool)
    ok = [i for i in range(len(m)) if not inc[i]]
    if not ok:
        return Freshness("MISSING_EXPECTED_BAR", base.expected_session, base.last_session, ["MISSING_EXPECTED_BAR"])
    last = ok[-1]
    today = now.date().isoformat()
    if str(m.sessions[last]) != today:
        return base
    from ..m54.timeframes import timeframe_minutes
    now_ns = int(now.timestamp() * 1e9)
    _, left = grid_position(int(m.end_ns[last]), today, lane.timeframe)
    n_ns = timeframe_minutes(lane.timeframe) * 60_000_000_000
    if left >= 1 and now_ns >= int(m.end_ns[last]) + n_ns:
        return Freshness(STALE_DATA, base.expected_session, base.last_session, [STALE_DATA])
    return base


def run_daily(app: App, symbols: Sequence[str], backend: Backend, *, do_refresh: bool = False,
              intraday: bool = True, start: str | None = None, max_steps: int | None = None,
              horizons: Sequence[int] | None = None, as_of: str | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """``as_of``: the requested exchange session (historical replay when the data runs past it). Without it
    the expected session comes from the registered calendar and the current IST time."""
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
    calendar = load_registered_calendar(s, app.db)
    clock = None if as_of else (now or now_ist())
    expected, confirmed = expected_completed_session(calendar, as_of, clock)
    fresh = {sym: symbol_freshness(app, sym, d.last_session, expected, confirmed) for sym, d in datas.items()}
    latest = expected or max((d.last_session for d in datas.values() if d.last_session), default=None)
    summary["latest_session"] = latest
    summary["expected_session"] = {"session": expected, "calendar_confirmed": confirmed,
                                   "source": "--as-of" if as_of else ("registered calendar" if calendar else None),
                                   "calendar": calendar.source if calendar else None}
    summary["data"] = {sym: {"last_session": d.last_session, "stale": not fresh[sym].fresh,
                             "freshness": fresh[sym].to_dict(),
                             "changed_files": d.changed_files, "first": d.summary.get("first"),
                             "minute_sessions": d.summary.get("minute_sessions_used"),
                             "minute_last": d.summary.get("minute_last"),
                             "minute_derived_daily_rows": d.summary.get("minute_derived_rows"),
                             "last_bar_source": str(d.daily["source"].iloc[-1]) if len(d.daily) else None}
                       for sym, d in datas.items()}
    for sym, info in summary["data"].items():
        if info["stale"]:
            prog.warn(f"{sym}: {info['freshness']['status']} (last session {info['last_session']}, "
                      f"expected {expected}); no LIVE output")
            app.db.event("DATA_STALE", {"symbol": sym, **info, "expected_session": expected}, subject=sym,
                         level="WARN")
    tf_cfg = s.section("timeframes")
    plan = []
    for sym, d in datas.items():
        bars = all_bars(d, calendar)
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
        ctx = {"freshness": intraday_freshness(fresh[sym], lane, clock), "calendar": calendar,
               "universe": universe_membership(app.db, sym), "cost_policy_version": cost_policy_version(s.section("costs"))}
        rp = LaneReplay(app.db, lane, hz, cfg, backend, prog, s.models_dir, lib, bars, rule_horizons=rule_h,
                        max_steps=max_steps, end_session=as_of[:10] if as_of else None, live_context=ctx)
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
    fc = live_forecasts(app.db, syms, session=expected)
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
