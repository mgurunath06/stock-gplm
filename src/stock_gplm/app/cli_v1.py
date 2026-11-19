"""CLI commands for the PRD v1 roadmap milestones (M5.10 - M7.3). Registered by :func:`register`."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

from .targets import label_ns


def _p(x: Any) -> None:
    print(json.dumps(x, indent=2, default=str) if not isinstance(x, str) else x)


def _syms(args: argparse.Namespace) -> list[str]:
    raw = (getattr(args, "stocks", None) or "") + ("," + args.stock if getattr(args, "stock", None) else "")
    out = [s.strip() for s in raw.split(",") if s.strip()]
    if not out:
        raise SystemExit("give --stock SYMBOL or --stocks A,B,C")
    return out


# ----------------------------------------------------------------------------- M5.10
def cmd_model_export(args: argparse.Namespace) -> int:
    from .cli import _actor, _run
    from .model_export import export_champions

    def work(app: Any) -> dict[str, Any]:
        res = export_champions(app, _actor(args, app), symbols=_syms(args) if (args.stock or args.stocks) else None,
                               horizons=[int(h) for h in args.horizons.split(",")] if args.horizons else None,
                               approved_only=args.approved_only)
        _p(res)
        return {"status": "COMPLETED", **res["counts"]}

    return _run(args, "model-export", work)


# ----------------------------------------------------------------------------- M6.0
def cmd_costs(args: argparse.Namespace) -> int:
    from .costs import compile_profile, contract_note, slippage_bps, validate_schedule
    from .settings import load

    s = load(args.config, args.home)
    cfg = s.section("costs")
    sch = cfg.get("schedule")
    if not sch:
        _p({"model": "flat_bps", "costs": cfg})
        return 0
    errs = validate_schedule(sch)
    if errs:
        _p({"valid": False, "errors": errs})
        return 1
    if args.action == "show":
        out = {}
        for prof in ("delivery", "intraday"):
            for b in sch["slippage_buckets"]:
                bps = compile_profile(sch, prof, float(b["bps"]))
                out[f"{prof}/{b['name']}"] = {k: round(v, 3) for k, v in bps.items()} | {
                    "round_trip_bps": round(sum(v for v in bps.values()) + bps["slippage_bps"], 3)}
        _p({"contract": sch.get("contract"), "basis": sch.get("basis"),
            "assumed_order_value_inr": sch["assumed_order_value_inr"], "per_side_bps": out})
        return 0
    slip = slippage_bps(sch, args.traded_value)[0] if args.traded_value is not None else 0.0
    _p(contract_note(sch, args.profile, args.qty, args.buy, args.sell, slip))
    return 0


# ----------------------------------------------------------------------------- M6.1
def cmd_indicators(args: argparse.Namespace) -> int:
    from .cli import _app, _run

    if args.action == "show":
        from .indicator_store import values_at

        app = _app(args, readonly=True)
        try:
            ns = label_ns(args.bar)
            _p(values_at(app.db, _syms(args)[0].upper(), args.timeframe, ns,
                         args.features.split(",") if args.features else None))
            return 0
        finally:
            app.close()

    def work(app: Any) -> dict[str, Any]:
        from .indicator_store import snapshot_lane
        from .ingest import Ingestor
        from .lanes import all_bars, build_lane
        from .session_calendar import load_registered_calendar

        cal = load_registered_calendar(app.s, app.db)
        out = []
        for sym in _syms(args):
            data = Ingestor(app.s, app.db).ingest(sym)
            bars = all_bars(data, cal)
            for tf in args.timeframes.split(","):
                lane = build_lane(data, tf, app.s.section("costs"), bars=bars, cache_dir=app.s.cache_dir / "features")
                if lane is None:
                    out.append({"lane": f"{sym}|{tf}", "status": "NO_BARS"})
                    continue
                out.append(snapshot_lane(app.db, lane, args.last, args.features.split(",") if args.features else None))
        _p(out)
        return {"status": "COMPLETED", "lanes": len(out)}

    return _run(args, "indicators", work)


# ----------------------------------------------------------------------------- M6.2
def cmd_coverage(args: argparse.Namespace) -> int:
    from .cli import _app
    from .research import library_roots
    from .strategy_library import StrategyLibrary

    app = _app(args, readonly=True)
    try:
        roots, writable = library_roots(app)
        lib = StrategyLibrary(roots, writable).load(record=False)
        by: dict[str, list[str]] = {}
        for sid, s in sorted(lib.strategies.items()):
            key = "RUNNABLE" if not s.blocked else s.blocked.get("status", "BLOCKED")
            by.setdefault(key, []).append(sid)
        _p({"total": len(lib.strategies), "counts": {k: len(v) for k, v in by.items()},
            "blocked": {k: v for k, v in by.items() if k != "RUNNABLE"}, "summary": lib.summary()})
        return 0
    finally:
        app.close()


# ----------------------------------------------------------------------------- M6.3
def cmd_horizons(args: argparse.Namespace) -> int:
    from .cli import _app
    from .horizons import LabelSpec, baselines, buckets, label_summary, labels

    app = _app(args, readonly=args.action == "list")
    try:
        bk = buckets(app.s.section("horizon_buckets"))
        if args.action == "list":
            _p(bk)
            return 0
        from .ingest import Ingestor
        from .lanes import all_bars, build_lane
        from .session_calendar import load_registered_calendar

        b = bk[args.bucket]
        tf = b["timeframe"]
        data = Ingestor(app.s, app.db).ingest(_syms(args)[0])
        lane = build_lane(data, tf, app.s.section("costs"), bars=all_bars(data, load_registered_calendar(app.s, app.db)))
        if lane is None:
            _p({"status": "NO_BARS", "timeframe": tf})
            return 1
        f = lane.bars.frame
        final = ~f["incomplete"].to_numpy(bool)
        out = {}
        n = len(lane.market)
        for h in b["bars"]:
            lab = labels(lane.market, LabelSpec(int(h), band=lane.costs.buy + lane.costs.sell,
                                                session_bound=bool(b.get("session_bound"))),
                         lane.costs.buy, lane.costs.sell, final)
            out[f"h={h}"] = {**label_summary(lab), "baselines": baselines(lab["net"], lab["status"], int(n * 0.6),
                                                                          (int(n * 0.8), n))}
        _p({"bucket": args.bucket, "timeframe": tf, "spec": b, "lane": lane.key, "results": out})
        return 0
    finally:
        app.close()


# ----------------------------------------------------------------------------- M6.4
def cmd_benchmark(args: argparse.Namespace) -> int:
    from .cli import _run

    def work(app: Any) -> dict[str, Any]:
        from .benchmark import BENCHMARK_DATE, BaseRatePredictor, frames_from_bars, record, run
        from .ingest import Ingestor
        from .lanes import all_bars
        from .session_calendar import load_registered_calendar

        date = args.date or BENCHMARK_DATE
        cal = load_registered_calendar(app.s, app.db)
        tfs = args.timeframes.split(",")
        out = {}
        for sym in _syms(args):
            data = Ingestor(app.s, app.db).ingest(sym)
            frames = frames_from_bars(all_bars(data, cal), tfs)
            band = float(args.band)
            cfg = {"timeframes": tfs, "band": band, "predictor": "base-rate.v1", "data": data.fingerprint,
                   "calendar": getattr(cal, "source", None)}
            for variant in ("frozen", "adaptive"):
                res = run(frames, date, BaseRatePredictor(band=band), adaptive=variant == "adaptive", band=band)
                rec = record(app.db, data.symbol, date, variant, cfg, res)
                out[f"{data.symbol}/{variant}"] = {**rec, "result_sha256": res["result_sha256"], **res["summary"]}
        _p(out)
        bad = [k for k, v in out.items() if v.get("reproduced") is False]
        return {"status": "FAILED" if bad else "COMPLETED", "not_reproduced": bad}

    return _run(args, "benchmark", work)


# ----------------------------------------------------------------------------- M6.5
def cmd_controls(args: argparse.Namespace) -> int:
    from .cli import _actor, _app, _run
    from .hybrid import control_state, set_control

    if args.action == "show":
        app = _app(args, readonly=True)
        try:
            _p(control_state(app.db, args.stock))
            return 0
        finally:
            app.close()

    def work(app: Any) -> dict[str, Any]:
        st = set_control(app.db, args.action.replace("-", "_").upper(), _actor(args, app), args.reason or "",
                         scope=args.stock or "global", target=args.target)
        _p(st)
        return {"status": "COMPLETED"}

    return _run(args, "controls", work)


def cmd_hybrid(args: argparse.Namespace) -> int:
    from .cli import _run

    def work(app: Any) -> dict[str, Any]:
        import numpy as np

        from .horizons import LabelSpec, MATURE, labels
        from .hybrid import fit_hybrid
        from .ingest import Ingestor
        from .lanes import all_bars, build_lane
        from .session_calendar import load_registered_calendar

        cal = load_registered_calendar(app.s, app.db)
        sectors = json.loads(Path(args.sectors).read_text(encoding="utf-8")) if args.sectors else {}
        Xs, ys, ss, ords, tfc, sec, reg = [], [], [], [], [], [], []
        names: list[str] | None = None
        for sym in _syms(args):
            data = Ingestor(app.s, app.db).ingest(sym)
            lane = build_lane(data, args.timeframe, app.s.section("costs"), bars=all_bars(data, cal),
                              cache_dir=app.s.cache_dir / "features")
            if lane is None:
                continue
            names = names or [n for n in lane.names if ":" not in n][:60]
            cols = [lane.names.index(n) if n in lane.names else -1 for n in names]
            X = np.column_stack([lane.X[:, j] if j >= 0 else np.full(len(lane.market), np.nan) for j in cols])
            lab = labels(lane.market, LabelSpec(args.horizon, None, None), lane.costs.buy, lane.costs.sell)
            ok = lab["status"] == MATURE
            m = lane.market
            with np.errstate(invalid="ignore"):
                trend = np.where(m.close > np.convolve(m.close, np.ones(50) / 50, "full")[:len(m.close)], "UP", "DOWN")
            Xs.append(X[ok]); ys.append((lab["net"][ok] > 0).astype(float)); ss += [data.symbol] * int(ok.sum())
            ords.append(m.end_ns[ok]); tfc += [args.timeframe] * int(ok.sum())
            sec += [sectors.get(data.symbol, "UNKNOWN")] * int(ok.sum()); reg += list(trend[ok])
        model, report = fit_hybrid(np.vstack(Xs), np.concatenate(ys), ss, {"sector": sec, "regime": reg},
                                   np.concatenate(ords), names or [], min_stock_rows=args.min_stock_rows)
        out = app.s.home / "hybrid-models" / f"{args.name}.json"
        model.save(out)
        _p({**report, "saved": str(out)})
        return {"status": "COMPLETED", "rows": report["rows"]}

    return _run(args, "hybrid", work)


# ----------------------------------------------------------------------------- M6.6
def cmd_paper(args: argparse.Namespace) -> int:
    from .cli import _actor, _app, _run
    from . import paper as P

    if args.action == "summary":
        app = _app(args, readonly=True)
        try:
            _p(P.summary(app.db, args.source))
            return 0
        finally:
            app.close()

    def work(app: Any) -> dict[str, Any]:
        if args.action == "enrol":
            res = P.enrol(app.db, args.source, _actor(args, app), time.time_ns(), args.note or "")
            _p(res)
            return {"status": "COMPLETED"}
        # run: strategy sources only (strategy:<id>@<version>)
        from .ingest import Ingestor
        from .lanes import all_bars, build_lane
        from .research import _ctx_for, library_roots
        from .session_calendar import load_registered_calendar
        from . import strategy_dsl as dsl
        from .strategy_library import StrategyLibrary

        if not args.source.startswith("strategy:"):
            raise SystemExit("paper run supports strategy:<id>@<version> sources")
        sid = args.source.split(":", 1)[1].split("@")[0]
        lib = StrategyLibrary(*library_roots(app)).load(record=False)
        sdef = lib.strategies.get(sid)
        if sdef is None or sdef.blocked:
            raise SystemExit(f"strategy {sid} is unknown or blocked")
        cal = load_registered_calendar(app.s, app.db)
        tot: dict[str, int] = {}
        for sym in _syms(args):
            data = Ingestor(app.s, app.db).ingest(sym)
            bars = all_bars(data, cal)
            lane = build_lane(data, args.timeframe, app.s.section("costs"), bars=bars, cache_dir=app.s.cache_dir / "features")
            if lane is None:
                continue
            ctx = _ctx_for(bars[args.timeframe], args.timeframe, lane, bars, {}, None)
            params = sdef.defaults()
            if args.params:
                params.update(json.loads(args.params))
            mask = dsl.signal(sdef.entry_node, ctx, params)
            ex = sdef.doc["exit"]
            c = P.process(app.db, args.source, data.symbol, lane.market, mask, ex, lane.costs.buy, lane.costs.sell)
            for k, v in c.items():
                tot[k] = tot.get(k, 0) + v
        _p({"counts": tot, "summary": P.summary(app.db, args.source)})
        return {"status": "COMPLETED", **tot}

    return _run(args, "paper", work)


# ----------------------------------------------------------------------------- M7.0 - M7.3
def cmd_ops(args: argparse.Namespace) -> int:
    from .cli import _app, _run
    from . import ops

    if args.action == "pre-open":
        app = _app(args, readonly=True)
        try:
            _p(ops.pre_open(app))
            return 0
        finally:
            app.close()

    def work(app: Any) -> dict[str, Any]:
        if args.action in ("scan", "post-close", "intraday"):
            tf = args.timeframe or ("15m" if args.action == "intraday" else "1d")
            hz = args.horizon or ("intraday" if args.action == "intraday" else "d1_7")
            res = ops.scan(app, _syms(args), timeframe=tf, horizon=hz,
                           strategy_patterns=args.strategies.split(",") if args.strategies else None,
                           risk_file=Path(args.risk) if args.risk else None, as_of=args.as_of,
                           max_strategies=args.max_strategies)
            path = ops.write_report(app, res, f"{res.get('expected_session') or 'unknown'}-{tf}-{hz}")
            adv = ops.advance_alerts(app, None, tf)
            _p({**res, "alerts": adv, "report": str(path)})
            return {"status": "COMPLETED", "opportunities": res["opportunities"], "live": res["live"]}
        res = ops.advance_alerts(app, _syms(args) if (args.stock or args.stocks) else None)
        _p(res)
        return {"status": "COMPLETED"}

    return _run(args, "ops", work)


def cmd_alerts(args: argparse.Namespace) -> int:
    from .cli import _app
    from . import alerts as AL

    app = _app(args, readonly=True)
    try:
        if args.alert_id:
            _p(AL.status(app.db, args.alert_id))
        else:
            _p(AL.history(app.db, args.limit))
        return 0
    finally:
        app.close()


def cmd_opportunities(args: argparse.Namespace) -> int:
    from .cli import _app

    app = _app(args, readonly=True)
    try:
        where, params = "", []
        if args.session:
            where, params = "WHERE decision_label LIKE ?", [f"{args.session}%"]
        rows = app.db.q(f"SELECT payload_json FROM opportunities {where} ORDER BY decision_ns DESC, "
                        "COALESCE(rank, 1e9) LIMIT ?", (*params, args.limit))
        recs = [json.loads(r["payload_json"]) for r in rows]
        _p(recs if args.full else [{k: r.get(k) for k in ("rank", "symbol", "decision_bar", "action", "status",
                                                           "expected_net", "probability", "confidence", "trigger",
                                                           "stop", "targets", "no_trade_reasons", "status_reasons")}
                                   | {"strategy": r["source"]["id"]} for r in recs])
        return 0
    finally:
        app.close()


def register(add: Callable[..., argparse.ArgumentParser], stocks: Callable[[argparse.ArgumentParser], None]) -> None:
    me = add("model-export", cmd_model_export, "M5.10 freeze rolling champion fits into CANDIDATE artifacts")
    stocks(me)
    me.add_argument("--horizons", help="comma list")
    me.add_argument("--approved-only", action="store_true", help="only fits with an M5.9A owner approval")
    me.add_argument("--by")
    co = add("costs", cmd_costs, "M6.0 itemised NSE cost schedule: show per-side rates or a contract note")
    co.add_argument("action", choices=["show", "note"])
    co.add_argument("--profile", choices=["delivery", "intraday"], default="delivery")
    co.add_argument("--qty", type=float, default=100)
    co.add_argument("--buy", type=float, default=1000.0)
    co.add_argument("--sell", type=float, default=1010.0)
    co.add_argument("--traded-value", type=float, help="median daily traded value (INR) for the slippage bucket")
    ind = add("indicators", cmd_indicators, "M6.1 indicator store: snapshot lane values with provenance, or show a bar")
    ind.add_argument("action", choices=["snapshot", "show"])
    stocks(ind)
    ind.add_argument("--timeframes", default="1d")
    ind.add_argument("--timeframe", default="1d")
    ind.add_argument("--last", type=int, default=1)
    ind.add_argument("--features", help="comma list (default all)")
    ind.add_argument("--bar", help="show: bar label, e.g. 2026-08-13T15:30")
    add("coverage", cmd_coverage, "M6.2 strategy-library coverage: runnable vs blocked with reasons")
    hz = add("horizons", cmd_horizons, "M6.3 horizon buckets; label summary and baselines for one stock")
    hz.add_argument("action", choices=["list", "labels"])
    stocks(hz)
    hz.add_argument("--bucket", default="d1_7")
    bm = add("benchmark", cmd_benchmark, "M6.4 permanent 13 Aug 2026 next-candle benchmark (frozen + adaptive)")
    stocks(bm)
    bm.add_argument("--date", help="default 2026-08-13")
    bm.add_argument("--timeframes", default="5m,15m,60m,1d,1w,1mo")
    bm.add_argument("--band", default="0.001", help="neutral band on |return|")
    ct = add("controls", cmd_controls, "M6.5 pause updates, disable alerts, roll back to an approved artifact")
    ct.add_argument("action", choices=["show", "pause-updates", "resume-updates", "disable-alerts", "enable-alerts",
                                       "rollback", "clear-rollback"])
    ct.add_argument("--stock", help="scope (default global)")
    ct.add_argument("--target", help="rollback: approved artifact model id")
    ct.add_argument("--reason")
    ct.add_argument("--by")
    hy = add("hybrid", cmd_hybrid, "M6.5 fit a shared + stock-residual calibrated model (owner research run)")
    stocks(hy)
    hy.add_argument("--timeframe", default="1d")
    hy.add_argument("--horizon", type=int, default=5)
    hy.add_argument("--name", required=True)
    hy.add_argument("--sectors", help="JSON file {symbol: sector}")
    hy.add_argument("--min-stock-rows", type=int, default=150)
    pa = add("paper", cmd_paper, "M6.6 forward paper trading: enrol, run, summary")
    pa.add_argument("action", choices=["enrol", "run", "summary"])
    pa.add_argument("--source", required=True, help="strategy:<id>@<version>")
    stocks(pa)
    pa.add_argument("--timeframe", default="1d")
    pa.add_argument("--params", help="JSON parameter overrides")
    pa.add_argument("--note")
    pa.add_argument("--by")
    op = add("ops", cmd_ops, "M7 daily operation: pre-open, scan/post-close/intraday opportunities, alerts")
    op.add_argument("action", choices=["pre-open", "scan", "post-close", "intraday", "alerts"])
    stocks(op)
    op.add_argument("--timeframe")
    op.add_argument("--horizon", help="bucket (gplm horizons list)")
    op.add_argument("--strategies", help="library id patterns")
    op.add_argument("--max-strategies", type=int)
    op.add_argument("--risk", help="owner risk-inputs JSON")
    op.add_argument("--as-of")
    al = add("alerts", cmd_alerts, "M7.1 alert history or one alert's lifecycle")
    al.add_argument("--alert-id")
    al.add_argument("--limit", type=int, default=50)
    oo = add("opportunities", cmd_opportunities, "M7.0 stored ranked opportunities")
    oo.add_argument("--session")
    oo.add_argument("--limit", type=int, default=50)
    oo.add_argument("--full", action="store_true")


__all__ = ["register"]
