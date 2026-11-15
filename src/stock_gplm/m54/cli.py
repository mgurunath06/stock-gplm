"""``gplm``: local-first stock-behaviour discovery command line.

Every command runs locally. None needs Claude, Codex or any external API.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .store import read_json, utc_now, write_json_atomic

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO / "config" / "m54" / "discovery-default.json"
DEFAULT_PROVIDERS = REPO / "config" / "research" / "providers.json"
DEFAULT_UNIFIED = REPO / "config" / "m54" / "unified-default.json"
ETF_HEURISTIC = REPO / "config" / "m37" / "etf-symbol-heuristic.json"


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _symbols(args: argparse.Namespace) -> tuple[list[str], dict[str, Any] | None]:
    from .bulk import load_universe
    given = [x for x in (args.stock, args.stocks, args.universe) if x]
    if len(given) != 1:
        raise SystemExit("give exactly one of --stock, --stocks or --universe")
    if args.stock:
        return [args.stock], None
    if args.stocks:
        return [s for s in args.stocks.split(",") if s.strip()], None
    path = Path(args.universe)
    if not path.suffix:
        path = Path(args.config).resolve().parent / "universes" / f"{args.universe}.json"
    if not path.exists():
        raise SystemExit(f"universe file {path} not found. Create a stock-gplm.m54-universe.v1 file with "
                         "membership_basis (CURRENT_CONSTITUENT_DIAGNOSTIC for current index lists, which are "
                         "survivorship-biased).")
    doc = load_universe(path)
    return list(doc["symbols"]), doc


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {"raw_root": args.raw_root, "m32_run": args.m32_run}


def _detach(argv: Sequence[str], output_root: Path) -> int:
    jobs = output_root / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    job_id = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    log = jobs / f"{job_id}.log"
    child = [a for a in argv if a != "--detach"]
    env = dict(os.environ, PYTHONPATH=str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    with log.open("ab") as out:
        proc = subprocess.Popen([sys.executable, "-m", "stock_gplm.m54.cli", *child], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, cwd=os.getcwd(), env=env)
    write_json_atomic(jobs / f"{job_id}.json", {"job_id": job_id, "pid": proc.pid, "argv": child, "log": str(log),
                                                "started_utc": utc_now()})
    _print({"job_id": job_id, "pid": proc.pid, "log": str(log),
            "hint": "check progress with `gplm status`; stop with `gplm stop --run-id ...` or `--batch-id ...`"})
    return 0


def _unified(args: argparse.Namespace) -> bool:
    return args.data is not None or args.timeframes is not None


def cmd_discover_unified(args: argparse.Namespace, argv: Sequence[str]) -> int:
    """Daily + minute, multi-timeframe strategy discovery (one command, resumable)."""
    from .device import limit_process, resolve
    from .unified import UnifiedRun, load_settings
    output_root = Path(args.output_root).resolve()
    if args.detach:
        return _detach(argv, output_root)
    device, reason = resolve(args.device)
    overrides: dict[str, Any] = {"daily_root": args.daily_root, "minute_roots": args.minute_root or None,
                                 "m32_run": args.m32_run}
    settings = load_settings(Path(args.unified_config), args.budget, overrides)
    if args.recency == "uniform":
        settings["recency"].update({"half_lives_sessions": [], "regime_conditional": False})
    if args.universe == "all-eligible":
        from .unified_bulk import eligible_symbols
        universe: dict[str, Any] | None = eligible_symbols(Path(settings["data"]["daily_root"]), ETF_HEURISTIC)
        symbols = list(universe["symbols"])
    else:
        symbols, universe = _symbols(args)
    kw = {"timeframes": args.timeframes or "auto", "data": args.data or "all-available", "start": args.start,
          "end": args.end, "device": device}
    if args.dry_run:
        rows = []
        for sym in symbols[:50]:
            run = UnifiedRun(settings, sym, output_root, **kw)
            run.load(create=False)
            done = sorted(str(p.relative_to(run.dir / "stages")) for p in (run.dir / "stages").rglob("*.json")
                          if "-chunks" not in str(p)) if run.dir.exists() else []
            rows.append({"symbol": run.symbol, "run_id": run.run_id, "policy": run.policy.to_dict(),
                         "daily_status": run.daily.status, "lanes": run.lanes_plan, "completed_stages": done})
        _print({"dry_run": True, "budget": args.budget, "device": device, "device_reason": reason,
                "symbols_total": len(symbols), "symbols": rows})
        return 0
    if len(symbols) == 1:
        limit_process(args.max_memory_gb, threads=1)
        run = UnifiedRun(settings, symbols[0], output_root, workers=args.workers,
                         hypotheses_file=Path(args.hypotheses).resolve() if args.hypotheses else None, **kw)
        result = run.execute()
        result.update({"device": device, "device_reason": reason})
        _print(result)
        return 0 if result["state"] in {"COMPLETED", "STOPPED"} else 2
    from .unified_bulk import run_unified_batch
    result = run_unified_batch(settings, symbols, output_root, workers=args.workers, max_memory_gb=args.max_memory_gb,
                               universe=universe, **kw)
    _print(result)
    return 0


def cmd_discover(args: argparse.Namespace, argv: Sequence[str]) -> int:
    from .device import limit_process, resolve
    from .engine import Run, Settings
    if _unified(args):
        return cmd_discover_unified(args, argv)
    if args.universe == "all-eligible":
        raise SystemExit("--universe all-eligible needs the unified engine: add --data all-available")
    if args.timeframe != "1d":
        raise SystemExit("only --timeframe 1d is supported (minute data needs separate authorization)")
    output_root = Path(args.output_root).resolve()
    if args.detach:
        return _detach(argv, output_root)
    device, reason = resolve(args.device)
    symbols, universe = _symbols(args)
    settings = Settings.load(Path(args.config), args.budget, _overrides(args))
    hyp = Path(args.hypotheses).resolve() if args.hypotheses else None
    if args.dry_run:
        return _dry_run(settings, symbols, output_root, hyp, device, reason)
    if len(symbols) == 1:
        limit_process(args.max_memory_gb, threads=max(1, args.workers))
        run = Run(settings, symbols[0], output_root, hypotheses_file=hyp, device=device)
        result = run.execute()
        result["device"], result["device_reason"] = device, reason
        _print(result)
        return 0 if result["state"] in {"COMPLETED", "STOPPED"} else 2
    from .bulk import run_batch
    result = run_batch(Path(args.config), args.budget, symbols, output_root, workers=args.workers,
                       max_memory_gb=args.max_memory_gb, overrides=_overrides(args), hypotheses=hyp,
                       universe=universe, device=device)
    _print(result)
    return 0


def _dry_run(settings: Any, symbols: list[str], output_root: Path, hyp: Path | None, device: str, reason: str) -> int:
    from .engine import Run
    from .hypotheses import budget_sample, single_conditions
    rows = []
    for sym in symbols:
        run = Run(settings, sym, output_root, hypotheses_file=hyp, device=device)
        run._load(create=False)
        row: dict[str, Any] = {"symbol": run.symbol, "data_status": run.data.status, "bars": len(run.data.bars),
                               "run_id": run.run_id, "issues": run.data.issues}
        if run.data.status == "ACCEPTED":
            run._prepare()
            disc = slice(*run.segs["discovery"])
            enum = single_conditions(run.defs, run.features, disc, run.s["threshold_quantiles"])
            tested = len(budget_sample(enum, run.defs, int(run.s["budget"]["max_single_candidates"]), int(run.s["seed"])))
            b = run.s["budget"]
            row.update({"features": len(run.features), "enumerated": len(enum), "screen_hypotheses": tested + len(run.extra),
                        "horizons": run.horizons, "screen_tests": (tested + len(run.extra)) * len(run.horizons),
                        "max_refine_tests": None, "max_interaction_tests": b["max_interactions"] * len(run.horizons),
                        "segments": {k: [run.dates[a], run.dates[b2 - 1], b2 - a] for k, (a, b2) in run.segs.items() if b2 > a},
                        "holdout_cutoff": run.pin.cutoff})
        row["previous_progress"] = sorted(x.stem for x in (run.dir / "stages").glob("*.json")) if run.dir.exists() else []
        rows.append(row)
    _print({"dry_run": True, "budget": settings.config["budget_name"], "device": device, "device_reason": reason,
            "budget_limits": settings.config["budget"], "symbols": rows})
    return 0


def _find_run(output_root: Path, run_id: str) -> Path:
    matches = sorted((output_root / "runs").glob(f"*/{run_id}*")) + sorted((output_root / "unified").glob(f"*/{run_id}*"))
    if len(matches) != 1:
        raise SystemExit(f"run id prefix {run_id!r} matched {len(matches)} runs under {output_root}")
    return matches[0]


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.output_root).resolve()
    if args.run_id:
        d = _find_run(root, args.run_id)
        _print(read_json(d / "status.json") if (d / "status.json").exists() else {"run_dir": str(d), "state": "UNKNOWN"})
        return 0
    if args.batch_id:
        matches = sorted((root / "batches").glob(f"{args.batch_id}*"))
        if len(matches) != 1:
            raise SystemExit("batch id prefix must match exactly one batch")
        m = read_json(matches[0] / "batch-manifest.json")
        _print({k: m.get(k) for k in ("batch_id", "state", "progress", "updated_utc", "workers", "membership_basis")})
        return 0
    rows = []
    found = list((root / "runs").glob("*/*/status.json")) + list((root / "unified").glob("*/*/status.json"))
    for st in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]:
        s = read_json(st)
        rows.append({k: s.get(k) for k in ("symbol", "run_id", "kind", "state", "stage", "updated_utc", "progress")})
    _print(rows)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    root = Path(args.output_root).resolve()
    if args.run_id:
        d = _find_run(root, args.run_id)
    elif args.batch_id:
        matches = sorted((root / "batches").glob(f"{args.batch_id}*"))
        if len(matches) != 1:
            raise SystemExit("batch id prefix must match exactly one batch")
        d = matches[0]
    else:
        raise SystemExit("give --run-id or --batch-id")
    (d / "STOP").write_text(utc_now() + "\n", encoding="utf-8")
    out: dict[str, Any] = {"stop_file": str(d / "STOP"), "effect": "halts at the next checkpoint; delete the file to resume"}
    if args.terminate and (d / "status.json").exists():
        pid = int(read_json(d / "status.json").get("pid", 0))
        cmdline = Path(f"/proc/{pid}/cmdline")
        if pid > 1 and cmdline.exists() and b"stock_gplm.m54" in cmdline.read_bytes():
            os.kill(pid, signal.SIGTERM)
            out["sigterm_sent_to"] = pid
    _print(out)
    return 0


def cmd_resume_clear(args: argparse.Namespace) -> int:
    d = _find_run(Path(args.output_root).resolve(), args.run_id)
    (d / "STOP").unlink(missing_ok=True)
    _print({"cleared": str(d / "STOP"), "next": "re-run the same discover command to continue"})
    return 0


def _unified_report(d: Path, show: bool) -> int:
    ranked = read_json(d / "stages" / "rank.json")["payload"]
    _print({"run_dir": str(d), "report": str(d / "report.md"), "strategies_json": str(d / "strategies.json"),
            "verdict": ranked["verdict"], "tier_counts": ranked["tier_counts"],
            "total_discovery_tests": ranked["total_discovery_tests"],
            "top": [{"rank": x["rank"], "id": x["strategy_id"], "tf": x["timeframe"], "tier": x["tier"],
                     "entry": x["entry_text"], "exit": x["exit_text"]} for x in ranked["strategies"][:10]]})
    if show:
        print((d / "report.md").read_text(encoding="utf-8"))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .engine import normalize_symbol
    root = Path(args.output_root).resolve()
    if args.run_id:
        d = _find_run(root, args.run_id)
        if (d / "stages" / "rank.json").exists():
            return _unified_report(d, args.print)
    elif args.legacy is False and list((root / "unified" / normalize_symbol(args.stock)).glob("*/run-manifest.json")):
        ms = sorted((root / "unified" / normalize_symbol(args.stock)).glob("*/run-manifest.json"),
                    key=lambda p: read_json(p).get("completed_utc", ""), reverse=True)
        return _unified_report(ms[0].parent, args.print)
    else:
        manifests = sorted((root / "runs" / normalize_symbol(args.stock)).glob("*/run-manifest.json"),
                           key=lambda p: read_json(p).get("completed_utc", ""), reverse=True)
        if not manifests:
            raise SystemExit(f"no completed run for {args.stock} under {root}")
        d = manifests[0].parent
    f = read_json(d / "findings.json")
    _print({"run_dir": str(d), "report": str(d / "report.md"), "symbol": f["symbol"], "budget": f["budget_name"],
            "status_counts": f["status_counts"], "total_discovery_tests": f["total_discovery_tests"],
            "finalists": [{"id": x["hypothesis"]["hypothesis_id"], "h": x["horizon"], "status": x["status"],
                           "text": x["hypothesis"]["text"]} for x in f["findings"]]})
    if args.print:
        print((d / "report.md").read_text(encoding="utf-8"))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .report import verify_outputs
    d = _find_run(Path(args.output_root).resolve(), args.run_id)
    verify_outputs(d)
    src = d / "discovered" / f"{args.hypothesis}-h{args.horizon}.json"
    if not src.exists():
        raise SystemExit(f"{src.name} is not a discovered default of this run (only validation passers are exported)")
    doc = read_json(src)
    name = args.name or doc["name"]
    dest = Path(args.dest).resolve() / f"{name}.json"
    if dest.exists():
        raise SystemExit(f"{dest} exists; choose another --name (user copies are never overwritten)")
    doc.update({"name": name, "status": "USER_EDITABLE_COPY", "derived_from": {"run_dir": str(d), "file": src.name}})
    write_json_atomic(dest, doc)
    _print({"exported": str(dest), "original_kept_immutable": str(src),
            "evaluate_with": f"gplm discover --stock ... --hypotheses {dest}"})
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    from .device import detect, resolve
    info = detect()
    info["auto_resolves_to"] = resolve("auto")
    _print(info)
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    import numpy as np
    import pandas as pd

    from .evaluate import Market, evaluate
    from .hypotheses import signal_mask, single_conditions
    from .indicators import build_features
    from ..v1.portfolio import CostModel
    rng = np.random.default_rng(7)
    n = args.sessions
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    df = pd.DataFrame({"date": pd.bdate_range("2015-01-01", periods=n).strftime("%Y-%m-%d"), "open": close * (1 + rng.normal(0, 0.003, n)),
                       "high": close * 1.01, "low": close * 0.99, "close": close, "volume": rng.integers(1e5, 1e6, n).astype(float)})
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    space = read_json(Path(args.config).resolve().parent / "search-space.json")
    t0 = time.perf_counter()
    feats, defs, _ = build_features(df, space, max_period=120)
    t1 = time.perf_counter()
    cands = single_conditions(defs, feats, slice(0, n // 2), [0.1, 0.9])[: args.candidates]
    m = Market(tuple(df["date"]), *(df[c].to_numpy() for c in ("open", "high", "low", "close", "volume")),
               np.full(n, 0.02), np.zeros(n, bool))
    for h in cands:
        evaluate(m, signal_mask(h, feats), (0, n // 2), 10, CostModel(3, 5, 12.41, 10.91), target_atr=1.0,
                 bootstrap=200, random_draws=200, seed=1)
    t2 = time.perf_counter()
    from .device import detect
    _print({"device": "cpu", "sessions": n, "features": len(feats), "feature_seconds": round(t1 - t0, 3),
            "evaluations": len(cands), "evaluation_seconds": round(t2 - t1, 3),
            "per_evaluation_ms": round(1000 * (t2 - t1) / max(1, len(cands)), 2), "gpu": detect()})
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    from ..llm import envfile
    from ..llm.registry import check, load_registry, mock_health
    registry = load_registry(Path(args.providers))
    try:
        env_info = envfile.load(Path(args.env_file) if args.env_file else None)
    except envfile.InsecureEnvFile as exc:
        raise SystemExit(f"refusing insecure credentials file: {exc}")
    if args.action == "check":
        _print({"env_file": env_info["path"], "env_names_loaded": env_info["loaded"], "offline": registry.offline,
                "providers": check(registry, os.environ),
                "note": "values are never displayed; key_configured only reports presence"})
    else:
        audit = Path(args.output_root).resolve() / "providers"
        _print({"network": "none", "results": mock_health(registry, audit)})
    return 0


def cmd_hypothesize(args: argparse.Namespace) -> int:
    from ..llm import envfile
    from ..llm.client import LLMClient
    from ..llm.registry import RunBudget, authorize_live, load_registry, to_llm_config
    from .ai import build_prompt, parse_reply
    from .indicators import build_features
    import pandas as pd
    registry = load_registry(Path(args.providers))
    envfile.load(None)
    provider = authorize_live(registry, args.provider, os.environ, args.allow_live)
    space = read_json(Path(args.config).resolve().parent / "search-space.json")
    dummy = pd.DataFrame({"date": [f"d{i}" for i in range(300)], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    _, defs, _ = build_features(dummy, space, max_period=60)  # catalog only; no market data
    catalog = [d.to_dict() for d in defs.values()][: args.max_features]
    prompt = build_prompt(catalog, args.stock, args.max_hypotheses)
    budget = RunBudget(provider)
    cfg = to_llm_config(registry, [provider.name])
    budget.before(cfg.max_output_tokens)
    reply = LLMClient(cfg).complete(provider=provider.name, model=provider.model, purpose="m54-hypotheses", prompt=prompt)
    budget.after(reply.input_tokens + reply.output_tokens, reply.estimated_usd)
    doc = parse_reply(reply.text, {d["key"] for d in catalog}, f"{provider.name}:{provider.model}")
    out = Path(args.output).resolve()
    if out.exists():
        raise SystemExit(f"{out} exists")
    write_json_atomic(out, doc)
    _print({"written": str(out), "accepted": len(doc["hypotheses"]), "rejected": len(doc["rejected"]),
            "estimated_usd": reply.estimated_usd, "next": f"gplm discover --stock {args.stock} --hypotheses {out}"})
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    from .unified_bulk import library_export, library_list
    root = Path(args.output_root).resolve()
    if args.action == "list":
        _print(library_list(root, args.stock))
        return 0
    if not (args.strategy_id and args.name):
        raise SystemExit("library export needs --strategy-id and --name")
    _print(library_export(root, args.strategy_id, args.name, Path(args.dest)))
    return 0


def cmd_fixture(args: argparse.Namespace) -> int:
    from .fixture import make_fixture
    out = Path(args.out).resolve()
    if any(p.name == "raw" for p in [out, *out.parents]):
        raise SystemExit("fixtures must not be written into a raw-data tree")
    _print(make_fixture(out, [s for s in args.symbols.split(",") if s], start=args.start, end=args.end,
                        minute_from=args.minute_from, cutoff=args.cutoff, seed=args.seed))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gplm", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output-root", default="reports/m54", help="where runs, batches and jobs are stored")
    sub = p.add_subparsers(dest="command", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[common], **k)  # type: ignore[method-assign]

    d = sub.add_parser("discover", help="research one stock, a list or a universe")
    d.add_argument("--stock")
    d.add_argument("--stocks", help="comma-separated symbols")
    d.add_argument("--universe", help="universe name under config/m54/universes or a path")
    d.add_argument("--timeframe", default="1d")
    d.add_argument("--budget", default="standard", choices=["quick", "standard", "deep"])
    d.add_argument("--device", default="auto", choices=["cpu", "cuda", "auto"])
    d.add_argument("--workers", type=int, default=1)
    d.add_argument("--max-memory-gb", type=float, default=None)
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--resume", action="store_true", help="accepted for clarity; resume is always on")
    d.add_argument("--detach", action="store_true", help="run in the background, independent of this terminal")
    d.add_argument("--config", default=str(DEFAULT_CONFIG))
    d.add_argument("--hypotheses", help="stock-gplm.m54-hypotheses.v1 file with extra (user/AI) hypotheses")
    d.add_argument("--raw-root")
    d.add_argument("--m32-run")
    u = d.add_argument_group("unified multi-timeframe engine (selected by --data or --timeframes)")
    u.add_argument("--data", choices=["all-available", "daily", "minute"],
                   help="which permitted sources to use; selects the unified daily+minute engine")
    u.add_argument("--timeframes", help="auto, or a comma list of 1m,5m,15m,30m,60m,1d,1w")
    u.add_argument("--start", help="earliest session to use (YYYY-MM-DD); can only narrow the permitted range")
    u.add_argument("--end", help="latest session to use (YYYY-MM-DD); can only narrow the permitted range")
    u.add_argument("--recency", choices=["validated", "uniform"], default="validated",
                   help="validated: choose among uniform/exponential/regime weights on validation; uniform: no weighting")
    u.add_argument("--daily-root", help="override the daily raw root (read-only)")
    u.add_argument("--minute-root", action="append", help="override minute raw roots (repeatable; read-only)")
    u.add_argument("--unified-config", default=str(DEFAULT_UNIFIED))

    s = sub.add_parser("status")
    s.add_argument("--run-id")
    s.add_argument("--batch-id")
    s.add_argument("--limit", type=int, default=20)

    st = sub.add_parser("stop")
    st.add_argument("--run-id")
    st.add_argument("--batch-id")
    st.add_argument("--terminate", action="store_true", help="also send SIGTERM to the recorded worker pid")

    rc = sub.add_parser("clear-stop", help="remove a run's STOP file so it can resume")
    rc.add_argument("--run-id", required=True)

    r = sub.add_parser("report")
    r.add_argument("--stock")
    r.add_argument("--run-id")
    r.add_argument("--print", action="store_true")
    r.add_argument("--legacy", action="store_true", help="show the legacy daily-only report even if a unified run exists")

    e = sub.add_parser("export", help="copy a discovered default into an editable user strategy")
    e.add_argument("--run-id", required=True)
    e.add_argument("--hypothesis", required=True)
    e.add_argument("--horizon", type=int, required=True)
    e.add_argument("--name")
    e.add_argument("--dest", default=str(REPO / "config" / "m54" / "strategies" / "user"))

    lb = sub.add_parser("library", help="list discovered strategy defaults or export an editable copy")
    lb.add_argument("action", choices=["list", "export"])
    lb.add_argument("--stock")
    lb.add_argument("--strategy-id")
    lb.add_argument("--name")
    lb.add_argument("--dest", default=str(REPO / "config" / "m54" / "strategies" / "user"))

    fx = sub.add_parser("fixture", help="write a SYNTHETIC daily+minute fixture for integration tests")
    fx.add_argument("--out", required=True)
    fx.add_argument("--symbols", default="RELIANCE")
    fx.add_argument("--start", default="2019-01-01")
    fx.add_argument("--end", default="2021-12-31")
    fx.add_argument("--minute-from", default="2021-04-01")
    fx.add_argument("--cutoff", default="2021-11-01")
    fx.add_argument("--seed", type=int, default=7)

    sub.add_parser("devices")
    b = sub.add_parser("benchmark")
    b.add_argument("--sessions", type=int, default=2500)
    b.add_argument("--candidates", type=int, default=500)
    b.add_argument("--config", default=str(DEFAULT_CONFIG))

    pr = sub.add_parser("providers")
    pr.add_argument("action", choices=["check", "mock-health"])
    pr.add_argument("--providers", default=str(DEFAULT_PROVIDERS))
    pr.add_argument("--env-file")

    hy = sub.add_parser("hypothesize", help="ask one provider for hypotheses (live; needs explicit authorization)")
    hy.add_argument("--provider", required=True)
    hy.add_argument("--stock", required=True)
    hy.add_argument("--output", required=True)
    hy.add_argument("--allow-live", action="store_true")
    hy.add_argument("--max-hypotheses", type=int, default=10)
    hy.add_argument("--max-features", type=int, default=400)
    hy.add_argument("--providers", default=str(DEFAULT_PROVIDERS))
    hy.add_argument("--config", default=str(DEFAULT_CONFIG))
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    handlers = {"status": cmd_status, "stop": cmd_stop, "clear-stop": cmd_resume_clear, "report": cmd_report,
                "export": cmd_export, "devices": cmd_devices, "benchmark": cmd_benchmark, "providers": cmd_providers,
                "hypothesize": cmd_hypothesize, "library": cmd_library, "fixture": cmd_fixture}
    if args.command == "discover":
        return cmd_discover(args, argv)
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
