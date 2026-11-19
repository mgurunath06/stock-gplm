"""M7.3 — daily operation: opportunity scan, alert processing and the pre-open / post-close / intraday cycle.

``scan`` is the end-to-end research-to-alert path for a set of symbols on one timeframe:

1. ingest (incremental) and absolute freshness against the registered calendar;
2. build the lane; evaluate each applicable library strategy (optimise on discovery, read validation once:
   the validation metrics are the historical evidence) and read its entry signal on the latest bar;
3. build the PRD opportunity record for every strategy that fires, with supporting higher-timeframe
   forecasts, stored indicator values (M6.1 store), regime, risk checks (M7.2) and operator controls (M6.5);
4. status from the canonical LIVE decision; rank; store opportunities append-only;
5. create/advance alerts and deliver notifications for current LIVE events (M7.1).

Heavy work (many symbols x the full library) is the owner's to run; agents only smoke-test it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import alerts as AL
from . import opportunity as OP
from . import risk as RK
from .benchmark import BaseRatePredictor
from .contracts import NEUTRAL_BAND_VERSION
from .db import jdump
from .horizons import buckets
from .hybrid import control_state
from .indicator_store import lane_rows, store as store_indicators

OPS_CONTRACT = "stock-gplm.ops.m73.v1"
CORE_INDICATORS = ("rsi", "atr", "ema", "sma", "macd", "adx", "bb", "obv", "mfi", "vwap", "supertrend", "donchian")


def _core_features(names: Sequence[str], limit: int = 24) -> list[str]:
    out = [n for n in names if ":" not in n and any(k in n.lower() for k in CORE_INDICATORS)]
    return out[:limit]


def _htf_forecasts(bars: Mapping[str, Any], decision_ns: int, band: float) -> list[dict[str, Any]]:
    out = []
    pred = BaseRatePredictor(band=band)
    for tf in ("1w", "1mo"):
        b = bars.get(tf)
        if b is None or not len(b):
            continue
        f = b.frame
        ok = f[(f["avail_ns"] <= decision_ns) & (~f["incomplete"].astype(bool))]
        if len(ok) < 20:
            out.append({"timeframe": tf, "direction": None, "status": "UNAVAILABLE",
                        "reason": "fewer than 20 completed bars (or no registered calendar)"})
            continue
        p = pred.predict(tf, ok)
        out.append({"timeframe": tf, "status": "COMPLETED_BARS_ONLY", "model": pred.version, **p})
    return out


def scan(app: Any, symbols: Sequence[str], *, timeframe: str = "1d", horizon: str = "d1_7",
         strategy_patterns: Sequence[str] | None = None, risk_file: Path | None = None, as_of: str | None = None,
         now: datetime | None = None, max_strategies: int | None = None) -> dict[str, Any]:
    from .daily import now_ist, symbol_freshness
    from .governance import active_calendar, cost_policy_version, strategy_state, universe_membership
    from .ingest import Ingestor, normalize_symbol
    from .lanes import all_bars, build_lane
    from .research import _ctx_for, library_roots
    from .session_calendar import expected_completed_session, load_registered_calendar
    from .strategy_engine import Exit, LaneInputs, evaluate_strategy, lane_regimes
    from .strategy_library import StrategyLibrary, select

    s = app.s
    cfg_opp = s.section("opportunities") or {}
    cfg_alerts = {**AL.DEFAULTS, **(s.section("alerts") or {})}
    bk = buckets(s.section("horizon_buckets"))
    if horizon not in bk:
        raise ValueError(f"unknown horizon bucket {horizon}; choose from {sorted(bk)}")
    hz_bars = int(bk[horizon]["bars"][0])
    risk = RK.load(risk_file or (Path(s.section("risk").get("file")) if (s.section("risk") or {}).get("file") else None))
    calendar = load_registered_calendar(s, app.db)
    cal_rec = active_calendar(app.db)
    cal_id = getattr(cal_rec, "calendar_id", None) or (cal_rec.get("calendar_id") if isinstance(cal_rec, dict) else None)
    expected, confirmed = expected_completed_session(calendar, as_of, None if as_of else (now or now_ist()))
    roots, writable = library_roots(app)
    lib = StrategyLibrary(roots, writable).load(record=False)
    pool = select(lib.strategies, strategy_patterns) if strategy_patterns else lib.strategies
    pool = {k: v for k, v in sorted(pool.items()) if v.enabled and not v.blocked and v.direction == "long"}
    if max_strategies:
        pool = dict(list(pool.items())[:max_strategies])
    cost_ver = cost_policy_version(s.section("costs"))
    now_ns = time.time_ns()
    records: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}
    ing = Ingestor(s, app.db)
    for raw in symbols:
        sym = normalize_symbol(raw)
        data = ing.ingest(sym)
        fresh = symbol_freshness(app, data.symbol, data.last_session, expected, confirmed)
        bars = all_bars(data, calendar)
        lane = build_lane(data, timeframe, s.section("costs"), bars=bars, cache_dir=s.cache_dir / "features")
        if lane is None or len(lane.market) < 100:
            per_symbol[sym] = {"status": "NO_LANE", "bars": 0 if lane is None else len(lane.market)}
            continue
        m = lane.market
        t = len(m) - 1
        dec_ns = int(m.end_ns[t])
        latest = fresh.fresh and str(m.sessions[t]) == str(expected)
        ctx = _ctx_for(bars[timeframe], timeframe, lane, bars, {}, None)
        li = LaneInputs(ctx, m, lane.costs.buy, lane.costs.sell, lane.intraday, timeframe, 3)
        regimes = lane_regimes(ctx, m, int(len(m) * 0.6))
        regime = {k: bool(v[t]) for k, v in regimes.items()}
        htf = _htf_forecasts(bars, dec_ns, (lane.costs.buy + lane.costs.sell))
        feats = _core_features(lane.names)
        ind_rows = lane_rows(lane, [t], feats)
        store_indicators(app.db, ind_rows)
        indicators = [{k: r[k] for k in ("feature", "indicator", "params", "value", "status", "calc_version")}
                      for r in ind_rows]
        ctl = control_state(app.db, sym)
        uni = universe_membership(app.db, sym)
        atr = float(m.natr[t]) * float(m.close[t]) if np.isfinite(m.natr[t]) else None
        cache: dict[Any, Any] = {}
        fired = evaluated = 0
        for sid, sdef in pool.items():
            if not sdef.applies(sym, timeframe):
                continue
            res = evaluate_strategy(sdef, li, cache, regimes)
            evaluated += 1
            if res.get("status") != "TESTED" or not bool(res["_mask"][t]) or m.no_signal[t]:
                continue
            fired += 1
            met = res["met"]
            ex: Exit = res["exit"]
            st = strategy_state(app.db, sdef.id, sdef.version)
            gov = {"latest_eligible_bar": latest, "calendar_id": cal_id,
                   "calendar_covers": bool(calendar is not None and calendar.covers(str(m.sessions[t]))),
                   "strategy": {"strategy_id": sdef.id, "version": sdef.version, "stage": st.get("stage"),
                                "seq": st.get("seq")},
                   "universe": {"eligible": uni.get("eligible"), "universe_id": uni.get("universe_id"),
                                "basis": uni.get("basis")},
                   "cost_policy_version": cost_ver, "neutral_band_version": NEUTRAL_BAND_VERSION}
            evidence = {"n": int(met.get("trades") or 0), "mean_net": met.get("mean_net"),
                        "mean_gross": met.get("mean_gross"), "win_rate": met.get("win_rate"),
                        "mean_bars": met.get("avg_bars_held"), "segment": met.get("segment"),
                        "p_value": met.get("p_value"), "max_drawdown": met.get("max_drawdown"),
                        "scope": "STOCK", "calibrated": False, "basis": "validation segment (read once)",
                        "params": res.get("params")}
            blockers = ["ALERTS_DISABLED"] if ctl["alerts_disabled"] else []
            inp = OP.OpportunityInput(
                symbol=sym, timeframe=timeframe, horizon=horizon, horizon_bars=hz_bars, decision_ns=dec_ns,
                close=float(m.close[t]), atr=atr, source_kind="STRATEGY", source_id=sdef.id,
                source_version=str(sdef.version), signal=True, evidence=evidence,
                exit={"stop_atr": ex.stop_atr, "target_atr": ex.target_atr, "max_hold_bars": ex.max_hold},
                regime=regime, timeframe_forecasts=htf, indicators=indicators, freshness=fresh,
                holding=RK.holding(risk, sym), governance=gov, extra_blockers=blockers,
                limitations=[f"cost model: {lane.cost_detail.get('model')} ({lane.cost_detail.get('liquidity_bucket')})"])
            rec = OP.build(inp, cfg_opp)
            if rec["action"] == "BUY":
                rc = RK.check(risk, rec, [r for r in records if r["action"] == "BUY" and r["status"] == "LIVE"])
                if rc["blockers"]:
                    inp.extra_blockers = list(blockers) + rc["blockers"]
                    rec = OP.build(inp, cfg_opp)
                rec["risk"] = rc
            records.append(rec)
        per_symbol[sym] = {"status": "SCANNED", "freshness": fresh.status, "latest_bar": m.label(t),
                           "strategies_evaluated": evaluated, "signals": fired, "controls": ctl}
    ranked = OP.rank(records)
    stored = OP.store(app.db, ranked)
    created = []
    for r in ranked:
        if r["action"] == "BUY":
            a = AL.create(app.db, r, now_ns, cfg_alerts, (r.get("risk") or {}).get("blockers", []))
            if a:
                created.append(a["alert_id"])
    out = {"contract": OPS_CONTRACT, "timeframe": timeframe, "horizon": horizon, "expected_session": expected,
           "calendar_id": cal_id, "symbols": per_symbol, "opportunities": len(ranked),
           "buy": sum(r["action"] == "BUY" for r in ranked), "live": sum(r["status"] == "LIVE" for r in ranked),
           "stored": stored, "alerts_created": len(created), "top": [
               {k: r.get(k) for k in ("rank", "symbol", "action", "status", "expected_net", "probability",
                                      "confidence", "trigger", "stop", "targets")} | {"strategy": r["source"]["id"]}
               for r in ranked[:20]]}
    return out


def advance_alerts(app: Any, symbols: Sequence[str] | None = None, timeframe: str = "1d") -> dict[str, Any]:
    """Advance every open alert on its lane's completed bars, then notify."""
    from .ingest import Ingestor
    from .lanes import all_bars
    from .session_calendar import load_registered_calendar

    s = app.s
    cfg = {**AL.DEFAULTS, **(s.section("alerts") or {})}
    calendar = load_registered_calendar(s, app.db)
    now_ns = time.time_ns()
    moved: dict[str, list[str]] = {}
    cache: dict[str, Any] = {}
    ing = Ingestor(s, app.db)
    for aid in AL.open_alerts(app.db):
        a = app.db.one("SELECT symbol, snapshot_json FROM alerts WHERE alert_id=?", (aid,))
        snap = json.loads(a["snapshot_json"])
        if symbols and a["symbol"] not in symbols:
            continue
        tf = snap.get("timeframe", timeframe)
        key = f"{a['symbol']}|{tf}"
        if key not in cache:
            cache[key] = all_bars(ing.ingest(a["symbol"]), calendar)[tf].frame
        f = cache[key]
        bars = f[["end_ns", "open", "high", "low", "close", "session"]].to_dict("records")
        moved[aid] = AL.advance(app.db, aid, bars, now_ns, cfg)
    sent = AL.notify(app.db, s.home / "alerts" / "outbox.jsonl", cfg)
    return {"advanced": {k: v for k, v in moved.items() if v}, "open": len(AL.open_alerts(app.db)), "notifications": sent}


def pre_open(app: Any) -> dict[str, Any]:
    """Pre-open readiness: calendar, live universe, supplements, risk inputs, controls, open alerts."""
    from .governance import active_calendar, active_supplements, list_universes

    s = app.s
    rf = (s.section("risk") or {}).get("file")
    try:
        risk = RK.load(Path(rf)) if rf else None
        risk_status = "LOADED" if risk else "NOT_CONFIGURED"
    except RK.RiskError as exc:
        risk_status = f"ERROR: {exc}"
    cal = active_calendar(app.db)
    return {"contract": OPS_CONTRACT, "calendar": getattr(cal, "source", None) if cal else None,
            "universe": list_universes(app.db).get("active"), "supplements": sorted(active_supplements(app.db)),
            "risk_inputs": risk_status, "controls": control_state(app.db),
            "open_alerts": len(AL.open_alerts(app.db)),
            "note": "market context feeds are not connected (provider decision pending, PRD §1 / roadmap M8)"}


def write_report(app: Any, result: Mapping[str, Any], name: str) -> Path:
    out = app.s.home / "reports" / "opportunities"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{name}.json"
    p.write_text(jdump(result), encoding="utf-8")
    lines = [f"# Opportunities {result.get('expected_session')} ({result.get('timeframe')}, {result.get('horizon')})", "",
             f"{result.get('opportunities')} records, {result.get('buy')} BUY, {result.get('live')} LIVE, "
             f"{result.get('alerts_created')} alerts created.", "",
             "| Rank | Symbol | Strategy | Action | Status | Exp. net | P | Conf | Trigger | Stop | Targets |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in result.get("top", []):
        lines.append(f"| {r.get('rank') or ''} | {r['symbol']} | {r['strategy']} | {r['action']} | {r['status']} | "
                     f"{r.get('expected_net')} | {r.get('probability')} | {r.get('confidence')} | {r.get('trigger')} | "
                     f"{r.get('stop')} | {r.get('targets')} |")
    lines += ["", "Research decision support only. No order is placed. Probabilities are historical win rates unless "
                  "marked calibrated."]
    p.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


__all__ = ["OPS_CONTRACT", "advance_alerts", "pre_open", "scan", "write_report"]
