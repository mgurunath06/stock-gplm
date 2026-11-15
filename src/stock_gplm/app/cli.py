"""``gplm`` — Stock GPLM research application command line.

Everyday commands::

    gplm doctor
    gplm gpu-test [--require-gpu]
    gplm gpu-evidence --stocks RELIANCE --start 2026-08-01 --end 2026-11-14 --timeframes 15m --device gpu --fresh
    gplm daily --stocks RELIANCE,HDFCBANK,ICICIBANK --device auto [--refresh]
    gplm dashboard [--port 8765] [--open]
    gplm discover --stocks RELIANCE,HDFCBANK --data all-local --timeframes auto --budget deep --device auto --resume
    gplm m56 --stocks RELIANCE,HDFCBANK --timeframes 15m,1d --mode both --budget standard --device auto --resume
    gplm replay --stocks RELIANCE --start 2026-08-13 --device auto
    gplm strategies list | show ID | export --id ID | copy ID | edit ID --set exit.max_hold_bars=7 | activate ID | history ID
    gplm audit --run RUN_ID | --prediction PRED_ID | --verify | --export predictions --format csv | --backup
    gplm predictions --stocks RELIANCE --mode live
    gplm ai status | ai generate --provider ollama --stock RELIANCE --timeframe 1d

``gplm research ...`` passes through to the M5.4 engine CLI (fixture, report, library, stop, ...).
Exit codes: 0 success, 1 error, 2 ``--device gpu`` / ``--require-gpu`` unavailable, 3 another GPLM
process holds the writer lock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import CLASSIFICATION, __version__


def _p(*a: Any) -> None:
    print(*a, flush=True)


def _symbols(args: argparse.Namespace, app: Any) -> list[str]:
    from .ingest import list_symbols, normalize_symbol

    raw = getattr(args, "stocks", None) or getattr(args, "stock", None)
    if raw:
        return [normalize_symbol(x) for x in raw.split(",") if x.strip()]
    uni = getattr(args, "universe", None)
    if uni:
        if uni in ("all", "all-eligible", "all-local"):
            syms = list_symbols(app.s)["symbols"]
        else:
            doc = json.loads(Path(uni).read_text(encoding="utf-8-sig"))
            syms = [normalize_symbol(x) for x in (doc["symbols"] if isinstance(doc, dict) else doc)]
        if getattr(args, "max_symbols", None):
            syms = syms[: args.max_symbols]
        return syms
    raise SystemExit("give --stocks SYMBOL[,SYMBOL...] or --universe all")


def _app(args: argparse.Namespace, readonly: bool = False) -> Any:
    from .context import App

    return App(args.config, args.home, readonly=readonly, quiet=getattr(args, "quiet", False))


def _backend(name: str) -> Any:
    from .compute import DeviceUnavailable, resolve

    try:
        return resolve(name)
    except DeviceUnavailable as exc:
        _p(f"ERROR: {exc}")
        _p("Fix: run windows\\install.ps1 -Gpu (Windows) or install a CUDA build of PyTorch, then `gplm gpu-test`.")
        raise SystemExit(2)


def _run(args: argparse.Namespace, kind: str, fn: Any, device: Any = None) -> int:
    from .db import LockBusy

    app = _app(args)
    try:
        app.start(kind, " ".join(sys.argv[1:]) or kind, {k: v for k, v in vars(args).items() if k != "func"},
                  device=device.label if device is not None else None)
    except LockBusy as exc:
        _p(f"ERROR: {exc}")
        app.close()
        return 3
    try:
        if device is not None:
            app.db.event("DEVICE_SELECTED", {"label": device.label, "reason": device.reason, "requested": args.device})
        result = fn(app)
        app.progress.close()
        status = result.get("status", "COMPLETED") if isinstance(result, dict) else "COMPLETED"
        if status not in {"COMPLETED", "PARTIAL", "FAILED"}:
            status = "COMPLETED"
        app.finish(status, {"result": _brief(result)})
        if getattr(args, "json", False):
            _p(json.dumps(result, indent=2, default=str))
        return 0 if status == "COMPLETED" else 1
    except KeyboardInterrupt:
        app.db.event("INTERRUPTED", {"note": "stopped by the user; completed steps are checkpointed"}, level="WARN")
        app.finish("INTERRUPTED", {})
        _p("\nInterrupted. Completed steps are checkpointed; re-run the same command to resume.")
        return 130
    except SystemExit:
        app.finish("FAILED", {"error": "SystemExit"})
        raise
    except Exception as exc:
        app.fail(exc)
        _p(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        app.close()


def _brief(result: Any) -> Any:
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k in ("latest_session", "new_predictions", "new_evaluations",
                                                         "model_fits", "symbols", "forecast_status", "report_files",
                                                         "device", "status", "lane_counts", "lane_failures",
                                                         "compute_attribution", "device_stats", "budget", "lanes_done",
                                                         "accepted", "run_id", "evidence_file", "evidence_sha256",
                                                         "workload", "telemetry", "isolation")}
    return None


# ----------------------------------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import doctor
    from .settings import load

    rep = doctor(load(args.config, args.home))
    if args.json:
        _p(json.dumps(rep, indent=2, default=str))
    else:
        _p(f"GPLM {rep['app_version']} doctor — {'OK' if rep['ok'] else 'PROBLEMS FOUND'}")
        for c in rep["checks"]:
            d = c["detail"] if isinstance(c["detail"], str) else json.dumps(c["detail"], default=str)
            _p(f"  [{c['status']:>4}] {c['check']:<22} {d[:220]}")
    return 0 if rep["ok"] else 1


def cmd_gpu_test(args: argparse.Namespace) -> int:
    from .doctor import gpu_test
    from .settings import load

    s = load(args.config, args.home)
    s.ensure()
    rep = gpu_test(s, args.require_gpu)
    if args.json:
        _p(json.dumps(rep, indent=2, default=str))
    else:
        _p(f"GPU test: {rep['status']} — {rep['verdict']}")
        for k, v in rep["results"].items():
            _p(f"  {k:<18} " + ", ".join(f"{a}={b}" for a, b in v.items() if a not in ("memory",)))
        _p(f"  evidence: {rep['evidence_file']}")
    try:
        from .context import App

        app = App(args.config, args.home, quiet=True)
        app.db.event("GPU_TEST", {k: rep[k] for k in ("status", "verdict", "evidence_file", "wsl")} |
                     {"results": rep["results"]}, level="INFO" if rep["status"] == "GPU_PASS" else "WARN")
        app.close()
    except Exception as exc:  # the evidence file is the primary record
        _p(f"  (audit event not written: {exc})")
    return int(rep.get("exit_code", 0))


def cmd_gpu_evidence(args: argparse.Namespace) -> int:
    from .gpu_evidence import run

    return run(argparse.Namespace(**{k: v for k, v in vars(args).items() if k not in {"func", "command"}}))


def cmd_ingest(args: argparse.Namespace) -> int:
    def work(app: Any) -> dict[str, Any]:
        from .ingest import Ingestor

        syms = _symbols(args, app)
        ing = Ingestor(app.s, app.db)
        app.progress.set_total(len(syms))
        out = {}
        for s in syms:
            app.progress.update(task="ingest", stock=s)
            try:
                d = ing.ingest(s, use_minute=not args.daily_only)
                out[d.symbol] = {"last": d.last_session, "changed_files": d.changed_files,
                                 "daily_rows": len(d.daily), "minute_bars": len(d.minute),
                                 "minute_decisions": d.summary.get("minute_decisions")}
            except Exception as exc:
                app.progress.error(f"{s}: {exc}")
                app.db.event("INGEST_FAILED", {"symbol": s, "error": f"{type(exc).__name__}: {exc}"}, subject=s,
                             level="ERROR")
                out[s] = {"error": str(exc)}
            app.progress.advance()
        if not args.json:
            for k, v in list(out.items())[:50]:
                _p(f"  {k:<16} {v}")
            if len(out) > 50:
                _p(f"  ... {len(out) - 50} more")
        return {"symbols": list(out), "results": out}

    return _run(args, "ingest", work)


def cmd_discover(args: argparse.Namespace) -> int:
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .discover import discover

        syms = _symbols(args, app)
        res = discover(app, syms, data_mode=args.data, timeframes=args.timeframes, budget=args.budget, backend=be,
                       workers=args.workers, start=args.start, end=args.end, skip_engine=args.skip_engine)
        if not args.json:
            for r in res["results"]:
                _p(f"\n{r['symbol']}  candles {r['candles']}")
                eng = r.get("engine")
                if eng:
                    tiers: dict[str, int] = {}
                    for st in eng.get("strategies", []):
                        tiers[st["tier"]] = tiers.get(st["tier"], 0) + 1
                    _p(f"  M5.4 engine run {str(eng.get('run_id'))[:16]} {eng.get('state')} lanes {eng.get('lanes')}")
                    _p(f"    strategies by tier: {tiers or 'none'}   report: {eng.get('dir')}/report.md")
                for sc in r["screens"]:
                    if sc.get("status") != "DONE":
                        _p(f"  screen {sc['timeframe']}: {sc.get('status')}")
                        continue
                    tiers = {}
                    for rr in sc["rules"]:
                        tiers[rr["screen_tier"]] = tiers.get(rr["screen_tier"], 0) + 1
                    _p(f"  screen {sc['timeframe']:>3}: {sc['tests']} tests on {sc['device']} in {sc['screen_seconds']}s, "
                       f"min q {sc['min_q'] if sc['min_q'] is None else round(sc['min_q'], 4)}, rules {tiers or 'none'}")
        return res

    return _run(args, "discover", work, be)


def cmd_m56(args: argparse.Namespace) -> int:
    """Run the complete M5.6 reference coverage plus open-ended local discovery workflow."""
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .m56 import TIMEFRAMES, run_m56

        syms = _symbols(args, app)
        tfs = list(TIMEFRAMES) if args.timeframes == "all" else [x.strip() for x in args.timeframes.split(",")]
        bad = sorted(set(tfs) - set(TIMEFRAMES))
        if bad:
            raise ValueError(f"unsupported timeframes: {bad}")
        result = run_m56(app, syms, tfs, be, catalogue=Path(args.catalogue).resolve(), mode=args.mode,
                         budget=args.budget, manifest_only=args.manifest_only, start=args.start, end=args.end)
        if not args.json:
            _p(f"M5.6 catalogue: {result['reference_count']} references (minimum, not a ceiling)")
            _p(f"  manifest: {result['manifest']}")
            if result.get("reference"):
                rr = result["reference"]
                _p(f"  reference combinations: {rr['combinations']}  validation results: "
                   f"{rr['validation_results']}  holdout scores: {rr['holdout_scores']}")
                for key, value in sorted(rr["coverage"].items()):
                    _p(f"    {key:<30} {value}")
            for row in result.get("leaderboard", []):
                best = row.get("best")
                label = best["strategy_id"] if best else "NO VALIDATED STRATEGY"
                _p(f"  {row['symbol']:<16} {row['timeframe']:>3} {row['regime']:<10} {label}")
            if result.get("report"):
                _p(f"  report: {result['report']}")
        return result

    return _run(args, "m56", work, be)


def cmd_ml_discover(args: argparse.Namespace) -> int:
    """Local ML strategy discovery over stored candles (development window only); resumable."""
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .db import sha
        from .m56 import TIMEFRAMES
        from .ml_discovery import run

        syms = _symbols(args, app)
        tfs = list(TIMEFRAMES) if args.timeframes == "all" else [x.strip() for x in args.timeframes.split(",")]
        bad = sorted(set(tfs) - set(TIMEFRAMES))
        if bad:
            raise ValueError(f"unsupported timeframes: {bad}")
        methods = [x.strip() for x in args.methods.split(",") if x.strip()]
        name = args.name or "run-" + sha({"symbols": syms, "tfs": tfs, "methods": methods, "budget": args.budget,
                                          "start": args.start, "end": args.end})[:10]
        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "ml-discovery" / name
        _p(f"ML discovery: {len(syms)} stock(s) x {len(tfs)} timeframe(s) x {len(methods)} method(s) -> {out}")
        res = run(app, syms, tfs, be, methods=methods, out=out, budget=args.budget, start=args.start, end=args.end)
        if not args.json:
            _p(f"  lanes: {res['lane_status']}")
            for c in res["classifications"]:
                _p(f"  {c['method']:<7} {c['classification']:<13} {c['n']}")
            _p(f"  summary: {res['summary']}")
            _p(f"  resume/extend: rerun the same command (completed lanes are skipped)")
        return res

    return _run(args, "ml-discover", work, be)


def cmd_replay(args: argparse.Namespace) -> int:
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .daily import run_daily

        syms = _symbols(args, app)
        hz = [int(x) for x in args.horizons.split(",")] if args.horizons else None
        res = run_daily(app, syms, be, intraday=not args.no_intraday, start=args.start, max_steps=args.max_steps,
                        horizons=hz)
        _print_daily(res, args)
        return res

    return _run(args, "replay", work, be)


def _print_daily(res: dict[str, Any], args: argparse.Namespace) -> None:
    if args.json:
        return
    counts = res.get("lane_counts", {})
    _p(f"\nRUN STATUS: {res.get('status', 'COMPLETED')}   lanes: {counts.get('succeeded', 0)} succeeded / "
       f"{counts.get('failed', 0)} failed")
    _p(f"Latest completed session: {res['latest_session']}   selected backend: {res['device']}")
    attribution = res.get("compute_attribution", {})
    _p(f"  actual training: {attribution.get('training_devices') or 'none this run'}   "
       f"numerical: {', '.join(attribution.get('numerical_devices', [])) or 'not recorded'}")
    for sym, d in res["data"].items():
        _p(f"  {sym:<14} last {d['last_session']} ({d['last_bar_source']})  changed files {d['changed_files']}  "
           f"minute sessions {d['minute_sessions']} (to {d['minute_last']})" + ("  STALE" if d["stale"] else ""))
    for ln in res["lanes"]:
        if "error" in ln:
            _p(f"  lane {ln['lane']}: ERROR {ln['error']}")
            continue
        o = ln.get("oos", {})
        _p(f"  lane {ln['lane']:<18} steps {ln['steps_done']}/{ln['steps_total']} new preds {ln['predictions_new']} "
           f"new evals {ln['evaluations_new']} fits {ln['fits']} | OOS acc {o.get('acc')} (n={o.get('n')}) "
           f"in {ln['seconds']}s")
    _p(f"  totals: {res['new_predictions']} new predictions, {res['new_evaluations']} new evaluations, "
       f"{res['model_fits']} model fits")
    for sym, st in res["forecast_status"].items():
        _p(f"  next session {sym:<14} {st}")
    _p(f"  report: {res['report_files']['report.html']}")


def cmd_daily(args: argparse.Namespace) -> int:
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .daily import run_daily

        syms = _symbols(args, app)
        res = run_daily(app, syms, be, do_refresh=args.refresh, intraday=not args.no_intraday, max_steps=args.max_steps)
        _print_daily(res, args)
        return res

    return _run(args, "daily", work, be)


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve
    from .settings import load

    s = load(args.config, args.home)
    cfg = s.section("dashboard")
    serve(s, args.host or cfg.get("host", "127.0.0.1"), int(args.port or cfg.get("port", 8765)), args.open)
    return 0


def cmd_strategies(args: argparse.Namespace) -> int:
    from .library import Library, StrategyError, summarize, validate

    if args.action in ("list", "show", "export", "history", "validate"):
        if args.action == "validate":
            try:
                d = validate(json.loads(Path(args.target).read_text(encoding="utf-8-sig")))
                _p(json.dumps(d, indent=2))
                return 0
            except (StrategyError, OSError, ValueError) as exc:
                _p(f"INVALID: {exc}")
                return 1
        app = _app(args, readonly=True)
        lib = Library(app.s.strategies_dir, app.db)
        try:
            if args.action == "list":
                rows = summarize(lib.list(args.stock and _norm(args.stock), args.timeframe, args.active))
                if args.json:
                    _p(json.dumps(rows, indent=2))
                else:
                    _p(f"{len(rows)} strategies (latest versions)")
                    for r in rows:
                        sc = r["scope"]["symbols"]
                        _p(f"  {r['strategy_id']} v{r['version']:<2} {'ACTIVE  ' if r['active'] else 'inactive'} "
                           f"{'locked' if r['immutable'] else 'user  '} {r['timeframe']:>3} "
                           f"{(sc if sc == '*' else ','.join(sc))[:24]:<24} {r['origin'][:22]:<22} {r['name'][:70]}")
            elif args.action in ("show", "export"):
                sid = args.id or args.target
                d = lib.get(sid, args.version)
                body = json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, indent=2, sort_keys=True)
                if args.out:
                    Path(args.out).write_text(body + "\n", encoding="utf-8")
                    _p(f"wrote {args.out}")
                else:
                    _p(body)
            elif args.action == "history":
                for r in lib.history(args.id or args.target):
                    _p(json.dumps(r, default=str))
            return 0
        except StrategyError as exc:
            _p(f"ERROR: {exc}")
            return 1
        finally:
            app.close()

    def work(app: Any) -> dict[str, Any]:
        lib = Library(app.s.strategies_dir, app.db)
        sid = args.id or args.target
        if args.action in ("activate", "deactivate"):
            lib.set_active(sid, args.action == "activate")
            _p(f"{sid}: {args.action}d")
            return {"status": args.action}
        if args.action in ("copy", "edit"):
            changes: dict[str, Any] = {}
            base = lib.get(sid)
            for kv in args.set or []:
                key, _, val = kv.partition("=")
                try:
                    value = json.loads(val)
                except ValueError:
                    value = val
                parts = key.split(".")
                target = changes
                src = base
                for p in parts[:-1]:
                    target = target.setdefault(p, json.loads(json.dumps(src.get(p, {}))))
                    src = src.get(p, {})
                target[parts[-1]] = value
            if args.file:
                changes.update(json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
            res = lib.copy_for_edit(sid, changes, args.name)
            _p(f"created {res['strategy_id']} v{res['version']} (inactive; activate with `gplm strategies activate "
               f"{res['strategy_id']}`): {res['path']}")
            return {"status": "created", **res}
        if args.action == "import":
            doc = json.loads(Path(args.target).read_text(encoding="utf-8-sig"))
            docs = doc.get("strategies", [doc]) if isinstance(doc, dict) else doc
            out = []
            for d in docs:
                if d.get("contract") == "stock-gplm.m54-strategy.v2":
                    from .library import from_m54

                    d = from_m54(d, _norm(args.stock or d.get("symbol", "UNKNOWN")))
                out.append(lib.add(d, immutable=False, origin=f"import:{Path(args.target).name}", active=False))
            for r in out:
                _p(f"  {r['strategy_id']} v{r['version']} {'created' if r['created'] else 'already present'}")
            return {"status": "imported", "accepted": len(out)}
        if args.action == "seed":
            from .daily import ensure_templates

            ensure_templates(app)
            return {"status": "seeded"}
        raise SystemExit(f"unknown action {args.action}")

    return _run(args, "strategies", work)


def _norm(s: str) -> str:
    from .ingest import normalize_symbol

    return normalize_symbol(s)


def cmd_audit(args: argparse.Namespace) -> int:
    from .report import trace_prediction, trace_run

    if args.backup:
        app = _app(args)
        p = app.db.backup(app.s.backups_dir, keep=int(app.s.section("retention").get("keep_backups", 10)))
        app.db.event("BACKUP", {"path": str(p)})
        app.close()
        _p(f"backup written: {p}")
        return 0
    app = _app(args, readonly=True)
    try:
        if args.verify:
            chain = app.db.verify_chain()
            integ = app.db.one("PRAGMA quick_check")
            _p(json.dumps({"event_chain": chain, "sqlite_quick_check": integ}, indent=2))
            return 0 if chain["ok"] else 1
        if args.export:
            body = app.db.export_table(args.export, args.format)
            if args.out:
                Path(args.out).write_text(body, encoding="utf-8")
                _p(f"wrote {args.out}")
            else:
                sys.stdout.write(body)
            return 0
        if args.prediction or args.sample:
            pid = args.prediction
            if args.sample:
                row = app.db.one("SELECT p.prediction_id FROM predictions p JOIN evaluations e ON "
                                 "e.prediction_id=p.prediction_id WHERE p.source_kind='MODEL' ORDER BY (p.source_id='logit') DESC, "
                                 "p.decision_ns DESC LIMIT 1")
                if not row:
                    _p("no evaluated prediction yet")
                    return 1
                pid = row["prediction_id"]
            _p(json.dumps(trace_prediction(app.db, pid), indent=2, default=str))
            return 0
        rid = args.run
        if not rid:
            r = app.db.one("SELECT run_id FROM runs ORDER BY started_utc DESC LIMIT 1")
            rid = r and r["run_id"]
        if not rid:
            _p("no runs recorded yet")
            return 1
        _p(json.dumps(trace_run(app.db, rid), indent=2, default=str))
        return 0
    finally:
        app.close()


def cmd_predictions(args: argparse.Namespace) -> int:
    app = _app(args, readonly=True)
    try:
        syms = _symbols(args, app) if (args.stocks or args.universe) else [r["symbol"] for r in app.db.q(
            "SELECT DISTINCT symbol FROM predictions")]
        if args.mode == "live":
            from .report import live_forecasts

            fc = live_forecasts(app.db, syms)
            if args.json:
                _p(json.dumps(fc, indent=2, default=str))
            else:
                for f in fc:
                    _p(f"\n{f['symbol']}: {f['status']}  (decision bar {f.get('decision_label')})")
                    if f.get("no_trade_reason"):
                        _p(f"  {f['no_trade_reason']}")
                    for c in (f["actionable"] or [c for c in f["candidates"] if c["source_kind"] == "MODEL"][:6]):
                        _p(f"  {c['source_kind']:<8} {c['source_id'][:22]:<22} {str(c.get('horizon_label'))[:14]:<14} "
                           f"{c['direction']:<6} P_cal={c['prob_calibrated'] if c['prob_calibrated'] is None else round(c['prob_calibrated'], 3)} "
                           f"({str(c['calibration'])[:28]}) ref {c['entry_ref']} stop {c['stop'] and round(c['stop'], 2)} "
                           f"target {c['target'] and round(c['target'], 2)} actionable={bool(c['actionable'])}")
                        if c.get("not_actionable_because"):
                            _p(f"      not actionable: {'; '.join(c['not_actionable_because'])}")
            return 0
        rows = app.db.q("SELECT p.mode, p.symbol, p.timeframe, p.source_kind, p.source_id, p.horizon_bars, p.decision_label, "
                        "p.direction, p.prob_calibrated, p.actionable, e.realized_return, e.correct, e.trade_net_return "
                        f"FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id WHERE p.symbol IN "
                        f"({','.join('?' * len(syms))}) AND p.mode=? ORDER BY p.decision_ns DESC LIMIT ?",
                        (*syms, args.mode.upper(), args.limit))
        _p(json.dumps(rows, indent=1, default=str))
        return 0
    finally:
        app.close()


def cmd_ai(args: argparse.Namespace) -> int:
    if args.action == "status":
        from .settings import load

        s = load(args.config, args.home)
        cfg = s.section("ai")
        _p(json.dumps({"enabled": cfg.get("enabled"), "allow_remote": cfg.get("allow_remote"),
                       "monthly_usd_limit": cfg.get("monthly_usd_limit"), "per_run_usd_limit": cfg.get("per_run_usd_limit"),
                       "providers": {k: {"kind": v["kind"], "model": v.get("model"), "local": v.get("local"),
                                         "key_env": v.get("api_key_env"),
                                         "key_present": bool(v.get("api_key_env") and os.environ.get(v["api_key_env"])),
                                         "prices_configured": v.get("usd_per_1k_input") is not None}
                                     for k, v in cfg.get("providers", {}).items()}}, indent=2))
        return 0

    def work(app: Any) -> dict[str, Any]:
        from .ai import AIRefused, generate
        from .ingest import Ingestor
        from .lanes import build_lane
        from .library import Library

        sym = _norm(args.stock)
        d = Ingestor(app.s, app.db).ingest(sym)
        lane = build_lane(d, args.timeframe, app.s.section("costs"))
        if lane is None:
            raise SystemExit(f"no {args.timeframe} data for {sym}")
        catalog = [{"feature": n, "family": lane.defs[n].family if n in lane.defs else "?",
                    "kind": lane.defs[n].kind if n in lane.defs else "?"} for n in lane.names]
        try:
            res = generate(app.db, Library(app.s.strategies_dir, app.db), app.s.section("ai"), args.provider, sym,
                           args.timeframe, catalog, allow_remote=args.allow_remote,
                           allow_unknown_cost=args.allow_unknown_cost)
        except AIRefused as exc:
            _p(f"REFUSED: {exc}")
            app.db.event("AI_REFUSED", {"provider": args.provider, "reason": str(exc)}, level="WARN")
            return {"status": "refused", "reason": str(exc)}
        _p(json.dumps(res, indent=2, default=str))
        return {"status": "ok", **res}

    return _run(args, "ai", work)


def cmd_status(args: argparse.Namespace) -> int:
    from .settings import load

    s = load(args.config, args.home)
    prog = json.loads(s.progress_path.read_text(encoding="utf-8")) if s.progress_path.exists() else {}
    lock = s.home / "gplm.lock"
    out: dict[str, Any] = {"lock": json.loads(lock.read_text()) if lock.exists() else None, "progress": prog}
    if s.db_path.exists():
        from .db import AuditDB

        db = AuditDB(s.db_path, readonly=True)
        out["latest_runs"] = db.q("SELECT run_id, kind, status, started_utc, finished_utc, device FROM runs "
                                  "ORDER BY started_utc DESC LIMIT 5")
        db.close()
    _p(json.dumps(out, indent=2, default=str))
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    from .ingest import list_symbols
    from .settings import load

    u = list_symbols(load(args.config, args.home))
    _p(json.dumps({"count": len(u["symbols"]), "excluded_etf_like": len(u["excluded_etf_like"]),
                   "membership_basis": u["membership_basis"], "symbols": u["symbols"] if args.all else u["symbols"][:40]},
                  indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="application config (default config/app/gplm.json or $GPLM_CONFIG)")
    common.add_argument("--home", help="state directory (default from config, or $GPLM_HOME)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--quiet", action="store_true", help="no live progress display")
    p = argparse.ArgumentParser(prog="gplm", description=f"Stock GPLM {__version__} — {CLASSIFICATION}",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--version", action="version", version=f"gplm {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=fn)
        return sp

    def stocks(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--stocks", help="comma-separated symbols, e.g. RELIANCE,HDFCBANK")
        sp.add_argument("--stock", help="one symbol")
        sp.add_argument("--universe", help="'all' (every collected -EQ symbol minus ETF-like names) or a JSON file")
        sp.add_argument("--max-symbols", type=int)

    def device(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--device", choices=["auto", "cpu", "gpu"], default="auto")

    add("doctor", cmd_doctor, "check the environment, data, database and GPU")
    g = add("gpu-test", cmd_gpu_test, "run real GPU workloads and compare with the CPU")
    g.add_argument("--require-gpu", action="store_true", help="exit 2 unless CUDA executed and matched")
    ge = add("gpu-evidence", cmd_gpu_evidence, "isolated real-candle discovery, training and walk-forward evaluation")
    from .gpu_evidence import add_arguments
    add_arguments(ge)
    i = add("ingest", cmd_ingest, "incrementally catalogue and validate local daily+minute files")
    stocks(i)
    i.add_argument("--daily-only", action="store_true")
    d = add("discover", cmd_discover, "multi-timeframe strategy discovery (M5.4 engine + batched screen)")
    stocks(d)
    device(d)
    d.add_argument("--data", choices=["all-local", "daily", "minute"], default="all-local")
    d.add_argument("--timeframes", default="auto", help="auto, or a comma list of 1m,5m,15m,30m,60m,1d,1w")
    d.add_argument("--budget", choices=["quick", "standard", "deep"], default="standard")
    d.add_argument("--resume", action="store_true", help="accepted for clarity; completed stages are always reused")
    d.add_argument("--workers", type=int, default=1)
    d.add_argument("--start")
    d.add_argument("--end")
    d.add_argument("--skip-engine", action="store_true", help="run only the batched broad screen")
    mld = add("ml-discover", cmd_ml_discover, "local ML strategy discovery + cost-aware backtest (resumable)")
    stocks(mld)
    device(mld)
    mld.add_argument("--timeframes", default="1d,1w", help="all, or comma list from 1m,5m,15m,30m,60m,1d,1w")
    mld.add_argument("--methods", default="tree,screen", help="tree (sklearn, CPU), screen (GPU when available)")
    mld.add_argument("--budget", choices=["quick", "standard", "deep"], default="quick", help="screen search size")
    mld.add_argument("--start")
    mld.add_argument("--end", help="defaults to m56.development_end; later dates are refused")
    mld.add_argument("--name", help="output folder name under <home>/reports/ml-discovery (default: derived)")
    mld.add_argument("--out", help="explicit output directory (reuse it to resume)")
    m56 = add("m56", cmd_m56, "M5.6 full reference-library coverage plus autonomous local discovery")
    stocks(m56)
    device(m56)
    m56.add_argument("--catalogue", default=str(Path(__file__).resolve().parents[3] / "config" / "strategies" /
                                                "reference-300-strategies.txt"))
    m56.add_argument("--mode", choices=["reference", "discovery", "both"], default="both")
    m56.add_argument("--timeframes", default="all", help="all, or comma list from 1m,5m,15m,30m,60m,1d,1w")
    m56.add_argument("--budget", choices=["quick", "standard", "deep"], default="standard")
    m56.add_argument("--start")
    m56.add_argument("--end")
    m56.add_argument("--resume", action="store_true", help="reuse immutable caches/checkpoints; this is the default")
    m56.add_argument("--fresh", action="store_true", help="new run record; immutable evidence and holdout locks remain")
    m56.add_argument("--manifest-only", action="store_true", help="register/verify all 300 without candle evaluation")
    r = add("replay", cmd_replay, "historical rolling walk-forward replay (also issues the live forecast)")
    stocks(r)
    device(r)
    r.add_argument("--start", help="first rolling session (default rolling.start, 2026-08-13)")
    r.add_argument("--max-steps", type=int, help="process at most this many sessions per lane (then stop; resumable)")
    r.add_argument("--horizons", help="override horizons, e.g. 1,5,10")
    r.add_argument("--no-intraday", action="store_true")
    dl = add("daily", cmd_daily, "evening workflow: ingest, evaluate, retrain, forecast, report")
    stocks(dl)
    device(dl)
    dl.add_argument("--refresh", action="store_true", help="run the configured local data refresh first")
    dl.add_argument("--no-intraday", action="store_true")
    dl.add_argument("--max-steps", type=int)
    db_ = add("dashboard", cmd_dashboard, "serve the local dashboard")
    db_.add_argument("--host")
    db_.add_argument("--port", type=int)
    db_.add_argument("--open", action="store_true", help="open a browser")
    st = add("strategies", cmd_strategies, "strategy library")
    st.add_argument("action", choices=["list", "show", "export", "copy", "edit", "activate", "deactivate", "history",
                                       "import", "validate", "seed"])
    st.add_argument("target", nargs="?", help="strategy id (prefix ok) or a file for import/validate")
    st.add_argument("--id")
    st.add_argument("--version", type=int)
    st.add_argument("--out")
    st.add_argument("--stock")
    st.add_argument("--timeframe")
    st.add_argument("--active", action="store_true")
    st.add_argument("--set", action="append", help="for copy/edit: dotted.key=JSON value, repeatable")
    st.add_argument("--file", help="for copy/edit: JSON object of top-level replacements")
    st.add_argument("--name")
    a = add("audit", cmd_audit, "reconstruct runs and predictions, verify, export, back up")
    a.add_argument("--run")
    a.add_argument("--prediction")
    a.add_argument("--sample", action="store_true", help="trace the most recent evaluated model prediction")
    a.add_argument("--verify", action="store_true")
    a.add_argument("--export", help="table name: predictions, evaluations, events, runs, models, strategies, ...")
    a.add_argument("--format", choices=["csv", "json"], default="csv")
    a.add_argument("--out")
    a.add_argument("--backup", action="store_true")
    pr = add("predictions", cmd_predictions, "show live forecasts or replay predictions")
    stocks(pr)
    pr.add_argument("--mode", choices=["live", "replay"], default="live")
    pr.add_argument("--limit", type=int, default=50)
    ai = add("ai", cmd_ai, "optional AI strategy hypotheses (disabled by default)")
    ai.add_argument("action", choices=["status", "generate"])
    ai.add_argument("--provider", default="ollama")
    ai.add_argument("--stock")
    ai.add_argument("--timeframe", default="1d")
    ai.add_argument("--allow-remote", action="store_true")
    ai.add_argument("--allow-unknown-cost", action="store_true")
    add("status", cmd_status, "lock, live progress and latest runs")
    sy = add("symbols", cmd_symbols, "list the local universe")
    sy.add_argument("--all", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "research":
        from ..m54.cli import main as m54_main

        return int(m54_main(argv[1:]) or 0)
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
