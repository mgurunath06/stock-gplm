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
def cmd_feature_coverage(args: argparse.Namespace) -> int:
    """Precompute the M5.8 catalogue and write the calculation x timeframe coverage matrix."""
    def work(app: Any) -> dict[str, Any]:
        from .feature_engine import CATALOGUE, TIMEFRAMES, run
        from .research import resolve_universe

        syms, basis = resolve_universe(app, args.stocks or args.stock, args.universe, args.max_symbols)
        tfs = list(TIMEFRAMES) if args.timeframes == "all" else [x.strip() for x in args.timeframes.split(",")]
        bad = sorted(set(tfs) - set(TIMEFRAMES))
        if bad:
            raise ValueError(f"unsupported timeframes: {bad}")
        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "feature-coverage" / args.name
        _p(f"Feature coverage: {len(syms)} stock(s) [{basis}] x {len(tfs)} timeframe(s) -> {out}")
        result = run(app, syms, tfs, out, Path(args.catalogue or CATALOGUE).resolve(), args.start, args.end)
        if not args.json:
            _p(f"  rows: {result['matrix_rows']}   status: {result['status_counts']}")
            _p(f"  matrix: {result['coverage_csv']}")
            _p(f"  cache: {result['cache_root']}")
        return result

    return _run(args, "feature-coverage", work)


def cmd_stock_dna(args: argparse.Namespace) -> int:
    """Discover point-in-time stock-specific feature behaviours."""
    def work(app: Any) -> dict[str, Any]:
        from .stock_dna import DEFAULT_TIMEFRAMES, run

        syms = _symbols(args, app)
        if len(syms) != 1:
            raise ValueError("stock-dna accepts exactly one --stock")
        tfs = list(DEFAULT_TIMEFRAMES) if args.timeframes == "all" else [x.strip() for x in args.timeframes.split(",")]
        bad = sorted(set(tfs) - set(DEFAULT_TIMEFRAMES))
        if bad:
            raise ValueError(f"unsupported timeframes: {bad}")
        horizons = [int(x) for x in args.horizons.split(",")]
        name = args.name or f"{syms[0]}-{args.as_of or 'latest'}"
        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "stock-dna" / name
        result = run(app, syms[0], tfs, args.as_of, out, horizons)
        if not args.json:
            _p(f"Stock DNA {result['symbol']} as of {result['as_of']}: {result['patterns']} patterns; "
               f"{result['validated']} validated, {result['experimental']} experimental")
            _p(f"  report: {result['report']}")
        return result

    return _run(args, "stock-dna", work)


def cmd_predict(args: argparse.Namespace) -> int:
    def work(app: Any) -> dict[str, Any]:
        from .m58_prediction import predict

        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "predictions" / "frozen"
        result = predict(app, args.stock, args.as_of, args.target, args.timeframe, args.horizon, out)
        if not args.json:
            _p(f"Prediction {result['prediction_id']} {result['status']}: {result['direction']} "
               f"P={result['probability']} action={result['action']} evidence={result['evidence_quality']}")
            _p(f"  report: {result['report']}")
        return result
    return _run(args, "predict", work)


def cmd_prediction_reveal(args: argparse.Namespace) -> int:
    def work(app: Any) -> dict[str, Any]:
        from .m58_prediction import reveal

        result = reveal(app, args.prediction)
        if not args.json:
            _p(f"{result['status']}: predicted {result.get('predicted_direction')} / actual "
               f"{result.get('actual_direction')} / correct {result.get('correct')}")
        return result
    return _run(args, "prediction-reveal", work)


def cmd_prediction_evaluate(args: argparse.Namespace) -> int:
    def work(app: Any) -> dict[str, Any]:
        from .m58_prediction import evaluate

        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "prediction-evaluation" / args.name
        result = evaluate(app, args.stock, out, start=args.start, end=args.end,
                          timeframe=args.timeframe, horizon=args.horizon, run_name=args.run_name)
        if not args.json:
            _p(f"[{result['mode']}] Predictions {result['predictions']}; revealed {result['revealed']}; accuracy "
               f"{result['directional_accuracy']}; abstention {result['abstention_rate']}; "
               f"mean Brier {result['mean_brier']}")
            _p(f"  report: {result['report']}")
        return result
    return _run(args, "prediction-evaluate", work)


def cmd_prediction_walk_forward(args: argparse.Namespace) -> int:
    def work(app: Any) -> dict[str, Any]:
        from .m58_prediction import format_run_totals, format_step_details, format_step_table, walk_forward

        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "prediction-evaluation" / args.name
        result = walk_forward(app, args.stock, args.start, args.end, args.timeframe, args.horizon, out,
                              args.max_steps, args.name)
        if not args.json:
            _p(f"Walk-forward {result['status']}: {result['steps_done']}/{result['planned']} steps")
            table = format_step_table(result["steps"])
            _p(table[0])
            for line, step in zip(table[1:], result["steps"]):
                _p(line)
                if args.details:
                    for detail in format_step_details(step, app.db.get_feature_snapshot(
                            step["feature_snapshot_sha256"]) or {}):
                        _p(detail)
            for err in result["errors"]:
                _p(f"  ERROR {err['as_of']} -> {err['target']}: {err['error']}")
            for line in format_run_totals(result):
                _p(line)
            _p(f"  report: {out / 'walk-forward-summary.json'}")
            _p(f"  feature snapshots: {result['steps_json']} and {result['steps_csv']}")
        return result
    return _run(args, "prediction-walk-forward", work)


def cmd_verify(args: argparse.Namespace) -> int:
    """Correctness-verification suite on synthetic fixtures; exit 0 pass, 1 correctness failure, 2 tracked gaps."""
    from datetime import datetime, timezone

    from ..verify.runner import run
    from .settings import load

    if args.out:
        out = Path(args.out).resolve()
    else:
        out = load(args.config, args.home).reports_dir / "verify" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rep = run(out, quiet=args.quiet and not args.json)
    if args.json:
        _p(json.dumps({"verdict": rep["verdict"], "out": rep["out"],
                       "sections": {s["section"]: s["verdict"] for s in rep["sections"]}}, indent=2))
    return int(rep["exit_code"])  # 0 pass, 1 correctness failure, 2 tracked calculation gaps only


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


def cmd_research(args: argparse.Namespace) -> int:
    """Unified catalogue + ML research matrix (development window only); resumable per stock x timeframe."""
    be = _backend(args.device)

    def work(app: Any) -> dict[str, Any]:
        from .m56 import TIMEFRAMES
        from .research import resolve_universe, run

        syms, basis = resolve_universe(app, args.stocks or args.stock, args.universe, args.max_symbols)
        tfs = list(TIMEFRAMES) if args.timeframes == "all" else [x.strip() for x in args.timeframes.split(",")]
        bad = sorted(set(tfs) - set(TIMEFRAMES))
        if bad:
            raise ValueError(f"unsupported timeframes: {bad}")
        methods = [x.strip() for x in args.methods.split(",") if x.strip() and x.strip() != "none"]
        bad_m = sorted(set(methods) - {"evolve", "tree", "screen"})
        if bad_m:
            raise ValueError(f"unknown ML methods {bad_m}; use evolve,tree,screen")
        if args.mode == "catalogue":
            methods = []
        out = Path(args.out).resolve() if args.out else app.s.reports_dir / "research" / args.name
        _p(f"Research: {len(syms)} stock(s) [{basis}] x {len(tfs)} timeframe(s); ML {methods or 'off'} -> {out}")
        pats = [x.strip() for x in (args.strategies or "").split(",") if x.strip()] or None
        workers = args.workers if args.workers is not None else max(1, min(len(syms) * len(tfs),
                                                                            (os.cpu_count() or 2) - 2))
        res = run(app, syms, tfs, be, catalogue=Path(args.catalogue).resolve(), methods=methods, out=out,
                  budget=args.budget, start=args.start, end=args.end, universe_basis=basis,
                  catalogue_enabled=args.mode in ("both", "catalogue"), strategy_patterns=pats,
                  library_extra=args.library, workers=workers, include_disabled=args.include_disabled)
        if not args.json:
            _p(f"  lanes: {res['lanes']}   workers: {workers}")
            _p(f"  catalogue: {res['catalogue_registered']} references, {res['catalogue_executed']} executed; "
               f"coverage {res['catalogue_coverage']}")
            _p(f"  backtests executed (TESTED rows): {res['tested_rows']}; ML strategies saved to library: "
               f"{res['library_saved']}")
            for r in res["by_status"]:
                _p(f"  {r['source']:<9} {r['status']:<30} {r['n']}")
            _p(f"  summary: {res['summary']}")
            _p("  resume: rerun the identical command (completed stock x timeframe lanes are skipped)")
        return res

    return _run(args, "research-matrix", work, be)


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


def cmd_library(args: argparse.Namespace) -> int:
    """Manage the declarative strategy library. Read-only actions need no lock."""
    from .research import library_roots
    from .strategy_library import StrategyLibrary, normalize, validate

    app = _app(args, readonly=args.action in ("list", "show", "history", "functions", "validate", "compare"))
    try:
        roots, writable = library_roots(app)
        lib = StrategyLibrary(roots, writable)
        a, t = args.action, list(args.target or [])
        if a == "functions":
            from .strategy_dsl import FUNCTIONS
            for k, v in sorted(FUNCTIONS.items()):
                _p(f"{k:<18} {v[2]}")
            return 0
        if a == "validate":
            if t:
                raw = json.loads(Path(t[0]).read_text(encoding="utf-8-sig"))
                bundle = isinstance(raw, dict) and "strategies" in raw
                for s in (raw["strategies"] if bundle else [raw]):
                    d = normalize(s, raw.get("defaults") if bundle else None, raw.get("exit_profiles"))
                    validate(d)
                    _p(f"OK {d['id']}")
                return 0
            r = lib.load(record=False)
            _p(json.dumps({**r.summary(), "invalid_entries": r.invalid}, indent=2))
            return 1 if r.invalid else 0
        if a in ("list", "show", "history", "compare"):
            r = lib.load(record=False)
            if a == "list":
                rows = [s for s in r.strategies.values() if (not args.origin or s.origin == args.origin)
                        and (not args.timeframe or args.timeframe in s.timeframes)
                        and (not args.stock or s.applies(args.stock, args.timeframe or s.timeframes[0]))]
                for s in sorted(rows, key=lambda x: x.id):
                    st = (s.blocked or {}).get("status", "EXECUTABLE")
                    _p(f"{s.id:<34} v{s.version:<3} {s.origin:<18} {st:<22} {'on ' if s.enabled else 'off'} "
                       f"{','.join(s.timeframes):<22} {s.doc.get('name', '')[:60]}")
                _p(f"{len(rows)} strategies; library {r.summary()}")
            elif a == "show":
                for sid in t:
                    s = r.strategies.get(sid)
                    _p(json.dumps({"id": sid, "version": s.version, "path": s.path, "enabled": s.enabled,
                                   "doc": s.doc} if s else {"id": sid, "error": "not found"}, indent=2, default=str))
            elif a == "history":
                for sid in t:
                    _p(json.dumps(lib.history_of(sid), indent=2))
            else:
                import sqlite3
                if not args.run:
                    raise SystemExit("compare needs --run <research output directory>")
                db = sqlite3.connect(Path(args.run) / "matrix.sqlite")
                cols = ("strategy_id", "symbol", "timeframe", "status", "classification", "val_trades", "val_mean_net",
                        "val_win_rate", "val_sharpe", "val_max_drawdown", "val_q_value", "final_mean_net")
                _p("  ".join(cols))
                q = ",".join("?" * len(t))
                for row in db.execute(f"SELECT {','.join(cols)} FROM matrix WHERE strategy_id IN ({q}) ORDER BY "
                                      "strategy_id, symbol", t):
                    _p("  ".join(str(v)[:12] for v in row))
            return 0
        if a == "add":
            for f in t:
                _p(f"added {lib.add_file(Path(f))} -> {writable / 'user'}")
            return 0
        if a == "edit":
            changes: dict[str, Any] = {}
            for kv in args.set or []:
                k, v = kv.split("=", 1)
                try:
                    changes[k] = json.loads(v)
                except ValueError:
                    changes[k] = v
            for sid in t:
                _p(f"editable copy written: {lib.copy_for_edit(sid, changes)} (the default stays immutable; "
                   f"'gplm library restore {sid}' brings it back)")
            return 0
        if a in ("enable", "disable"):
            for sid in t:
                lib.set_enabled(sid, a == "enable")
                _p(f"{sid}: {a}d")
            return 0
        if a == "restore":
            for sid in t:
                _p(f"{sid}: default restored; archived copies {lib.restore(sid)}")
            return 0
        from .strategy_ai import propose, translate

        tfs = [x.strip() for x in args.timeframes.split(",") if x.strip()]
        cfg = app.s.section("ai")
        if a == "translate":
            text = " ".join(t)
            if Path(text).is_file():
                text = Path(text).read_text(encoding="utf-8")
            res = translate(app.db, lib, cfg, args.provider, text, timeframes=tfs,
                            symbols=[args.stock] if args.stock else None, reference_id=args.reference,
                            allow_remote=args.allow_remote, allow_unknown_cost=args.allow_unknown_cost)
        else:
            import sqlite3
            if not args.run:
                raise SystemExit("propose needs --run <research output directory> to learn from")
            db = sqlite3.connect(Path(args.run) / "matrix.sqlite")
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM matrix WHERE status='TESTED' ORDER BY CAST(val_p_value AS REAL) LIMIT 30")]
            rows += [dict(r) for r in db.execute(
                "SELECT * FROM matrix WHERE status='TESTED' ORDER BY CAST(val_mean_net AS REAL) LIMIT 15")]
            res = propose(app.db, lib, cfg, args.provider, rows, n=args.n, timeframes=tfs,
                          allow_remote=args.allow_remote, allow_unknown_cost=args.allow_unknown_cost)
        _p(json.dumps(res, indent=2, default=str))
        return 0
    finally:
        app.close()


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
    rs = add("research-matrix", cmd_research, "unified matrix: every catalogue strategy + local ML discovery (resumable)")
    stocks(rs)
    device(rs)
    rs.add_argument("--name", required=True, help="run name; output <home>/reports/research/<name> (reuse to resume)")
    rs.add_argument("--out", help="explicit output directory instead of --name location")
    rs.add_argument("--timeframes", default="1d", help="all, or comma list from 1m,5m,15m,30m,60m,1d,1w")
    rs.add_argument("--mode", choices=["both", "catalogue", "ml"], default="both",
                    help="catalogue evaluation, autonomous ML discovery, or both")
    rs.add_argument("--methods", default="evolve", help="ML methods for --mode ml/both: evolve,tree,screen (or none)")
    rs.add_argument("--strategies", help="only these library ids / reference ids / origins (comma list, globs ok, "
                                         "e.g. 'C3-*,ML_DISCOVERED,MY-*')")
    rs.add_argument("--library", action="append", help="extra strategy-library root directory (repeatable)")
    rs.add_argument("--workers", type=int, help="parallel lane processes (default: CPU count - 2)")
    rs.add_argument("--include-disabled", action="store_true", help="also run disabled library strategies")
    rs.add_argument("--budget", choices=["quick", "standard", "deep"], default="quick")
    rs.add_argument("--catalogue", default=str(Path(__file__).resolve().parents[3] / "config" / "strategies" /
                                               "reference-300-strategies.txt"))
    rs.add_argument("--start")
    rs.add_argument("--end", help="defaults to m56.development_end; later dates are refused")
    fc = add("feature-coverage", cmd_feature_coverage,
             "precompute the 11-module M5.8 calculation catalogue and write its coverage matrix")
    stocks(fc)
    fc.add_argument("--name", required=True, help="output <home>/reports/feature-coverage/<name>")
    fc.add_argument("--out", help="explicit output directory")
    fc.add_argument("--timeframes", default="all", help="all, or comma list from 1m,5m,15m,30m,60m,4h,1d,1w")
    fc.add_argument("--catalogue", default=str(Path(__file__).resolve().parents[3] / "config" / "verify" /
                                                "primitives.json"))
    fc.add_argument("--start")
    fc.add_argument("--end")
    sd = add("stock-dna", cmd_stock_dna,
             "discover and quantify point-in-time stock-specific behaviours on cached lane features")
    sd.add_argument("--stock", required=True)
    sd.add_argument("--timeframes", default="all", help="all, or 1m,5m,15m,30m,60m,4h,1d,1w")
    sd.add_argument("--horizons", default="1", help="forward bars, comma-separated")
    sd.add_argument("--as-of", help="inclusive data cutoff YYYY-MM-DD (default latest accepted session)")
    sd.add_argument("--name")
    sd.add_argument("--out")
    pr = add("predict", cmd_predict, "freeze a point-in-time Stock-DNA historical prediction")
    pr.add_argument("--stock", required=True)
    pr.add_argument("--as-of", required=True, help="inclusive cutoff YYYY-MM-DD")
    pr.add_argument("--target", required=True, help="target session YYYY-MM-DD")
    pr.add_argument("--timeframe", default="1d")
    pr.add_argument("--horizon", type=int, default=1)
    pr.add_argument("--out")
    rv = add("prediction-reveal", cmd_prediction_reveal,
             "reveal actual outcome for an already frozen historical prediction")
    rv.add_argument("--prediction", required=True, help="frozen prediction id")
    pe = add("prediction-evaluate", cmd_prediction_evaluate,
             "walk-forward accuracy, calibration, abstention and net-performance report")
    pe.add_argument("--stock")
    pe.add_argument("--name", default="all")
    pe.add_argument("--start")
    pe.add_argument("--end")
    pe.add_argument("--timeframe")
    pe.add_argument("--horizon", type=int)
    pe.add_argument("--run-name")
    pe.add_argument("--out")
    pw = add("prediction-walk-forward", cmd_prediction_walk_forward,
             "chronological freeze-then-reveal evaluation over a session range (resumable)")
    pw.add_argument("--stock", required=True)
    pw.add_argument("--start", required=True)
    pw.add_argument("--end", required=True)
    pw.add_argument("--timeframe", default="1d")
    pw.add_argument("--horizon", type=int, default=1)
    pw.add_argument("--max-steps", type=int)
    pw.add_argument("--name", required=True)
    pw.add_argument("--details", action="store_true", help="print per-step feature and pattern evidence")
    pw.add_argument("--out")
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
    lb = add("library", cmd_library, "declarative strategy library (DSL): list/show/validate/add/edit/enable/"
                                     "disable/restore/history/functions/translate/propose/compare")
    lb.add_argument("action", choices=["list", "show", "validate", "add", "edit", "enable", "disable", "restore",
                                       "history", "functions", "translate", "propose", "compare"])
    lb.add_argument("target", nargs="*", help="strategy id(s), a JSON file (add/validate) or text (translate)")
    lb.add_argument("--origin")
    lb.add_argument("--stock")
    lb.add_argument("--timeframe")
    lb.add_argument("--set", action="append", help="edit: dotted.key=JSON value, repeatable (e.g. exit.max_hold_bars=7)")
    lb.add_argument("--provider", default="anthropic")
    lb.add_argument("--timeframes", default="1d")
    lb.add_argument("--reference", help="translate: catalogue reference id the text belongs to")
    lb.add_argument("--run", help="propose/compare: research run directory (matrix.sqlite) to learn from")
    lb.add_argument("--n", type=int, default=10)
    lb.add_argument("--allow-remote", action="store_true")
    lb.add_argument("--allow-unknown-cost", action="store_true")
    vf = add("verify", cmd_verify, "correctness-verification suite (synthetic fixtures): PASS/FAIL report with "
                                   "strategy-, indicator- and ML-level evidence")
    vf.add_argument("--out", help="report directory (default <home>/reports/verify/<UTC timestamp>)")
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
