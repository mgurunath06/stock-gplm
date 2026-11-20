"""Recency research: find what works NOW for each stock x timeframe (owner design, 2026-11-20).

Idea: stock behaviour changes, so a strategy is judged on recent candles only, and we validate the
*method* ("a recent winner keeps winning on this stock x timeframe"), not one fixed strategy.

Per lane (stock x timeframe):
1. Every applicable library strategy (catalogue translations, user, AI and ML rules) x a few parameter
   combinations (``--max-combos``) is simulated once over the whole history with the normal engine
   (costs, next-open fills, no overlapping trades). Identical trade lists are kept once.
2. Walk forward in calendar steps. At each decision date D, using only trades that *closed before D*
   and were signalled in the lookback window, each variant gets a recency-weighted, shrunk average:
       score = sum(w * net) / (sum(w) + PRIOR),  w = 0.5 ** (age_days / (lookback * HALF_LIFE))
   HALF_LIFE defaults to 0.25 (owner, 2026-11-20: recent candles weigh more), so a trade's weight halves
   every quarter of the lookback; the oldest trade in the window counts 1/16 of the newest.
   The top ``top_k`` (one per strategy, score > 0, >= min_trades recent trades) are the picks.
   We then record the picks' trades signalled in [D, D + step) and, as a control, the trades of every
   eligible variant in the same window ("pick anything that was active").
3. Verdict per period: ``dev`` = steps whose forward window ends by ``m56.development_end``; ``test`` =
   steps decided after it (the final judge). The method HOLDS when picks made money (mean net > 0) and
   beat the control, over at least ``min_steps`` steps and ``min_fwd_trades`` trades.
4. Current picks: ranking at the latest candle. A lane is ASSIGNED when the method holds in dev and in
   test, WATCH when it holds in dev and test has too few trades, otherwise not used.

Re-run daily: the ranking rotates as new candles arrive (lanes whose data changed are recomputed;
unchanged lanes are reused from ``lanes/<SYMBOL>/<tf>.json``).

Usage (PYTHONPATH=src):
  python -m stock_gplm.app.recency --stocks RELIANCE,TCS --timeframes all --name now [--workers 8]
  python -m stock_gplm.app.recency --universe deploy/oracle/live/universe.txt --max-symbols 50 --name now
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

CONTRACT = "stock-gplm.recency.v1"
TF_ALL = ("5m", "15m", "30m", "60m", "1d", "1w")


@dataclass(frozen=True)
class Window:
    lookback_days: int
    step_days: int
    min_trades: int


WINDOWS: dict[str, Window] = {
    "1m": Window(30, 7, 5), "5m": Window(30, 7, 5), "15m": Window(30, 7, 5),
    "30m": Window(91, 14, 5), "60m": Window(91, 14, 5),
    "1d": Window(365, 30, 4), "1w": Window(1095, 91, 3),
}


@dataclass(frozen=True)
class Policy:
    top_k: int = 3
    prior: float = 3.0          # pseudo-trades at zero return: few lucky trades cannot top the list
    half_life: float = 0.25     # weight half-life as a fraction of the lookback (smaller = recent counts more)
    min_steps: int = 4          # steps with forward trades needed for a verdict
    min_fwd_trades: int = 10    # forward trades of the picks needed for a verdict
    current_list: int = 10      # how many current picks to report per lane


# --------------------------------------------------------------------------- pure walk-forward core


def _day_index(sessions: np.ndarray) -> np.ndarray:
    return np.asarray(sessions).astype("datetime64[D]").astype(np.int64)


def score(vid: np.ndarray, net: np.ndarray, age: np.ndarray, n_var: int, lookback: int, prior: float,
          half_life: float = 0.25) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-variant (score, count, plain mean) from the selected recent trades."""
    w = 0.5 ** (age / max(1.0, lookback * half_life))
    sw = np.bincount(vid, weights=w, minlength=n_var)
    swx = np.bincount(vid, weights=w * net, minlength=n_var)
    cnt = np.bincount(vid, minlength=n_var)
    sx = np.bincount(vid, weights=net, minlength=n_var)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(cnt > 0, sx / np.maximum(cnt, 1), np.nan)
    return swx / (sw + prior), cnt, mean


def pick(sc: np.ndarray, cnt: np.ndarray, group: np.ndarray, min_trades: int, k: int) -> list[int]:
    """Top ``k`` eligible variants (score > 0), at most one per strategy ``group``."""
    ok = np.nonzero((cnt >= min_trades) & (sc > 0))[0]
    out: list[int] = []
    seen: set[int] = set()
    for v in ok[np.argsort(-sc[ok], kind="stable")].tolist():
        if group[v] in seen:
            continue
        seen.add(int(group[v]))
        out.append(int(v))
        if len(out) >= k:
            break
    return out


def walk(vid: np.ndarray, t: np.ndarray, e: np.ndarray, net: np.ndarray, day: np.ndarray, group: np.ndarray,
         win: Window, pol: Policy, split_day: int) -> dict[str, Any]:
    """Walk-forward validation of "recent winner keeps winning" plus the current ranking.

    ``vid/t/e/net``: flat trades (variant, signal bar, exit bar, net return); ``day``: per-bar day number;
    ``group``: strategy index per variant. Decisions use only trades with exit bar < decision bar.
    """
    n_var = int(group.size)
    n_bars = int(day.size)
    steps: list[dict[str, Any]] = []
    if n_bars == 0:
        return {"steps": steps, "current": [], "current_scores": []}
    eday = day[e] if e.size else np.zeros(0, np.int64)
    d = int(day[0]) + win.lookback_days
    last = int(day[-1])
    while d + win.step_days <= last + 1:
        r = int(np.searchsorted(day, d, "left"))
        r2 = int(np.searchsorted(day, d + win.step_days, "left"))
        w0 = int(np.searchsorted(day, d - win.lookback_days, "left"))
        past = (e < r) & (t >= w0)
        sc, cnt, _ = score(vid[past], net[past], (d - eday[past]).astype(float), n_var, win.lookback_days, pol.prior,
                     pol.half_life)
        picks = pick(sc, cnt, group, win.min_trades, pol.top_k)
        fwd = (t >= r) & (t < r2)
        fv, fn = vid[fwd], net[fwd]
        elig = cnt >= win.min_trades
        pm = np.isin(fv, picks)
        cm = elig[fv]
        period = "dev" if d + win.step_days <= split_day + 1 else ("test" if d > split_day else "straddle")
        steps.append({"date": str(np.datetime64(d, "D")), "period": period, "eligible": int(elig.sum()),
                      "picks": picks, "pick_n": int(pm.sum()), "pick_sum": float(fn[pm].sum()),
                      "pick_sumsq": float((fn[pm] ** 2).sum()), "ctrl_n": int(cm.sum()),
                      "ctrl_sum": float(fn[cm].sum())})
        d += win.step_days
    dn = last + 1
    sel = e < n_bars
    sel &= t >= int(np.searchsorted(day, dn - win.lookback_days, "left"))
    sc, cnt, mean = score(vid[sel], net[sel], (dn - eday[sel]).astype(float), n_var, win.lookback_days, pol.prior,
                     pol.half_life)
    cur = pick(sc, cnt, group, win.min_trades, pol.current_list)
    return {"steps": steps, "current": cur,
            "current_scores": [{"variant": v, "score": float(sc[v]), "recent_trades": int(cnt[v]),
                                "recent_mean_net": float(mean[v])} for v in cur]}


def verdict(steps: Sequence[Mapping[str, Any]], pol: Policy) -> dict[str, Any]:
    used = [s for s in steps if s["pick_n"] > 0]
    n = sum(s["pick_n"] for s in used)
    tot = sum(s["pick_sum"] for s in used)
    sq = sum(s["pick_sumsq"] for s in used)
    cn = sum(s["ctrl_n"] for s in steps)
    cs = sum(s["ctrl_sum"] for s in steps)
    mean = tot / n if n else None
    cmean = cs / cn if cn else None
    sd = math.sqrt(max(0.0, (sq - n * mean * mean) / (n - 1))) if n > 1 and mean is not None else None
    tstat = mean / (sd / math.sqrt(n)) if sd else None
    hit = sum(1 for s in used if s["pick_sum"] > 0) / len(used) if used else None
    if len(used) < pol.min_steps or n < pol.min_fwd_trades:
        v = "TOO_FEW"
    elif mean is not None and mean > 0 and (cmean is None or mean > cmean):
        v = "HOLDS"
    else:
        v = "FAILS"
    return {"verdict": v, "steps": len(steps), "steps_with_trades": len(used), "pick_trades": n,
            "pick_mean_net": mean, "pick_t": tstat, "step_hit_rate": hit, "ctrl_trades": cn, "ctrl_mean_net": cmean}


def lane_status(dev: Mapping[str, Any], test: Mapping[str, Any]) -> str:
    if dev["verdict"] != "HOLDS":
        return "NOT_USED"
    return {"HOLDS": "ASSIGNED", "TOO_FEW": "WATCH"}.get(test["verdict"], "DROPPED_IN_TEST")


# --------------------------------------------------------------------------- lane computation (worker-safe)


def _thin(combos: list[dict[str, Any]], defaults: dict[str, Any], k: int) -> list[dict[str, Any]]:
    rest = [c for c in combos if c != defaults]
    if k <= 1 or not rest:
        return [defaults]
    step = max(1.0, len(rest) / (k - 1))
    return [defaults] + [rest[int(i * step)] for i in range(min(k - 1, len(rest)))]


def compute_lane(job: Mapping[str, Any]) -> dict[str, Any]:
    from . import strategy_dsl as dsl
    from .lanes import all_bars, build_lane
    from .research import _ctx_for, _rebuild
    from .strategy_engine import Exit, LaneInputs, simulate

    t0 = time.monotonic()
    data, tf = job["data"], job["timeframe"]
    win: Window = Window(**job["window"])
    pol: Policy = Policy(**job["policy"])
    out: dict[str, Any] = {"symbol": data.symbol, "timeframe": tf, "key": job["key"], "contract": CONTRACT,
                           "window": asdict(win), "policy": asdict(pol)}
    bars = all_bars(data)
    lane = build_lane(data, tf, job["costs"], bars=bars, cache_dir=job.get("feature_cache")) \
        if tf in bars and len(bars[tf]) else None
    if lane is None:
        return {**out, "status": "NO_DATA", "seconds": round(time.monotonic() - t0, 2)}
    m = lane.market
    ctx = _ctx_for(bars[tf], tf, lane, bars, job["refs"], job.get("benchmark"))
    li = LaneInputs(ctx, m, lane.costs.buy, lane.costs.sell, lane.intraday, tf, 1)
    n = len(m)
    variants: list[dict[str, Any]] = []
    vid_l, t_l, e_l, net_l, grp_l = [], [], [], [], []
    seen: set[bytes] = set()
    skipped: dict[str, int] = {}
    for gi, d in enumerate(job["strategies"]):
        try:
            s = _rebuild(d)
        except Exception as exc:  # noqa: BLE001 - one bad definition never stops the lane
            skipped[f"INVALID:{type(exc).__name__}"] = skipped.get(f"INVALID:{type(exc).__name__}", 0) + 1
            continue
        if s.blocked:
            skipped[s.blocked["status"]] = skipped.get(s.blocked["status"], 0) + 1
            continue
        ex = Exit.of(s.doc["exit"])
        short = s.direction == "short"
        for params in _thin(s.grid(), s.defaults(), job["max_combos"]):
            try:
                mask = dsl.signal(s.entry_node, ctx, params)
                xm = dsl.signal(s.exit_node, ctx, params) if s.exit_node is not None else None
            except dsl.MissingData:
                skipped["MISSING_DATA"] = skipped.get("MISSING_DATA", 0) + 1
                break
            except dsl.DSLError:
                skipped["DSL_SCOPE"] = skipped.get("DSL_SCOPE", 0) + 1
                break
            tr = simulate(m, mask, (0, n), ex, li.buy, li.sell, short, xm)
            if not tr:
                continue
            tt = np.fromiter((x["t"] for x in tr), np.int64, len(tr))
            ee = np.fromiter((x["exit_index"] for x in tr), np.int64, len(tr))
            key = tt.tobytes() + ee.tobytes() + (b"S" if short else b"L")
            if key in seen:
                continue
            seen.add(key)
            v = len(variants)
            variants.append({"strategy_id": s.id, "params": params, "direction": s.direction, "origin": s.origin,
                             "category": s.doc.get("category", ""), "name": s.doc.get("name", ""),
                             "entry": s.doc.get("entry", ""), "exit": ex.text(s.doc["exit"].get("when"), short),
                             "total_trades": len(tr)})
            vid_l.append(np.full(len(tr), v, np.int64))
            t_l.append(tt)
            e_l.append(ee)
            net_l.append(np.fromiter((x["net_return"] for x in tr), float, len(tr)))
            grp_l.append(gi)
    cat = (lambda xs, dt: np.concatenate(xs) if xs else np.zeros(0, dt))
    vid, t, e, net = cat(vid_l, np.int64), cat(t_l, np.int64), cat(e_l, np.int64), cat(net_l, float)
    group = np.asarray(grp_l, np.int64)
    day = _day_index(m.sessions)
    split = int(np.datetime64(job["split"], "D").astype(np.int64))
    w = walk(vid, t, e, net, day, group, win, pol, split)
    dev = verdict([s for s in w["steps"] if s["period"] == "dev"], pol)
    test = verdict([s for s in w["steps"] if s["period"] == "test"], pol)
    status = lane_status(dev, test)

    def vrow(v: int) -> dict[str, Any]:
        return {"variant": v, **variants[v]}

    steps = [{**s, "picks": [f"{variants[p]['strategy_id']}|{json.dumps(variants[p]['params'], sort_keys=True)}"
                             for p in s["picks"]]} for s in w["steps"]]
    current = [{**vrow(c["variant"]), **c} for c in w["current_scores"]]
    return {**out, "status": status, "bars": n, "data_start": m.label(0), "data_end": m.label(n - 1),
            "data_fingerprint": lane.data_fingerprint, "strategies": len(job["strategies"]),
            "variants": len(variants), "trades": int(net.size), "skipped": skipped, "dev": dev, "test": test,
            "steps": steps, "current": current, "seconds": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------- orchestration


def _load_pool(app: Any, patterns: Sequence[str] | None, extra: Sequence[str] | None) -> list[Any]:
    from .research import library_roots
    from .strategy_library import StrategyLibrary, select

    roots, writable = library_roots(app, extra)
    pool = select(StrategyLibrary(roots, writable).load().strategies, patterns)
    return sorted((s for s in pool.values() if s.enabled), key=lambda s: s.id)


def _lane_path(out: Path, symbol: str, tf: str) -> Path:
    return out / "lanes" / symbol / f"{tf}.json"


def run(app: Any, symbols: Sequence[str], timeframes: Sequence[str], out: Path, *, workers: int = 1,
        max_combos: int = 4, policy: Policy | None = None, patterns: Sequence[str] | None = None,
        library_extra: Sequence[str] | None = None, start: str | None = None, end: str | None = None,
        log: Any = print) -> dict[str, Any]:
    from .db import sha
    from .ingest import ingest_many, window_data
    from .research import _ref_symbols

    pol = policy or Policy()
    split = app.s.section("m56").get("development_end") or "2026-08-13"
    windows = {**WINDOWS, **{k: Window(**v) for k, v in (app.s.section("recency").get("windows") or {}).items()}}
    pool = _load_pool(app, patterns, library_extra)
    benchmark = app.s.section("research").get("benchmark_symbol")
    out.mkdir(parents=True, exist_ok=True)
    meta = {"contract": CONTRACT, "split_development_end": split, "symbols": len(symbols),
            "timeframes": list(timeframes), "windows": {k: asdict(windows[k]) for k in timeframes},
            "policy": asdict(pol), "max_combos": max_combos, "strategies": len(pool), "patterns": list(patterns or []),
            "window": {"start": start, "end": end}, "config_sha256": app.s.sha256, "code_sha": app.code_sha,
            "costs": app.s.section("costs"), "benchmark_symbol": benchmark}
    (out / "run-config.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
                                         encoding="utf-8")
    use_minute = any(tf.endswith("m") for tf in timeframes)
    refs: dict[str, Any] = {}
    for rs in sorted(_ref_symbols(pool, benchmark)):
        try:
            d = next(iter(ingest_many(app.s, app.db, [rs], use_minute=use_minute).values()), None)
            if d is not None:
                refs[rs] = window_data(d, start, end)
        except Exception as exc:  # noqa: BLE001 - strategies needing it are skipped as MISSING_DATA
            log(f"  reference {rs} not loaded: {exc}")
    sdocs = [{"doc": s.doc, "path": s.path, "root": s.root, "version": s.version, "sha256": s.sha256,
              "enabled": s.enabled} for s in pool]
    counts = {"computed": 0, "reused": 0, "failed": 0, "no_data": 0}
    ex = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    pending: dict[Any, tuple[str, str]] = {}

    def save(res: dict[str, Any]) -> None:
        p = _lane_path(out, res["symbol"], res["timeframe"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(res, default=str), encoding="utf-8")
        os.replace(tmp, p)
        counts["no_data" if res["status"] == "NO_DATA" else "computed"] += 1
        log(f"  {res['symbol']} {res['timeframe']}: {res['status']} ({res.get('variants', 0)} variants, "
            f"{res.get('seconds')} s)")

    def drain(limit: int) -> None:
        while len(pending) > limit:
            fut = next(as_completed(list(pending)))
            sym, tf = pending.pop(fut)
            try:
                save(fut.result())
            except Exception as exc:  # noqa: BLE001 - one bad lane never stops the run
                counts["failed"] += 1
                log(f"  {sym} {tf}: FAILED {type(exc).__name__}: {exc}")

    try:
        for sym in symbols:
            sel = [tf for tf in timeframes]
            try:
                data = next(iter(ingest_many(app.s, app.db, [sym], use_minute=use_minute).values()), None)
            except Exception as exc:  # noqa: BLE001
                log(f"  {sym}: not loaded ({exc})")
                counts["failed"] += len(sel)
                continue
            if data is None:
                counts["no_data"] += len(sel)
                continue
            data = window_data(data, start, end)
            for tf in sel:
                appl = [d for d, s in zip(sdocs, pool) if tf in s.timeframes and s.applies(sym, tf)]
                key = sha({"c": CONTRACT, "sym": sym, "tf": tf, "fp": data.fingerprint,
                           "lib": sha(sorted(d["sha256"] for d in appl)), "w": asdict(windows[tf]),
                           "pol": asdict(pol), "mc": max_combos, "cfg": app.s.sha256, "split": split,
                           "code": app.code_sha})
                p = _lane_path(out, data.symbol, tf)
                if p.is_file():
                    try:
                        if json.loads(p.read_text(encoding="utf-8")).get("key") == key:
                            counts["reused"] += 1
                            continue
                    except (OSError, ValueError):
                        pass
                job = {"data": data, "timeframe": tf, "window": asdict(windows[tf]), "policy": asdict(pol),
                       "costs": app.s.section("costs"), "feature_cache": app.s.cache_dir / "features",
                       "refs": refs, "benchmark": benchmark, "strategies": appl, "max_combos": max_combos,
                       "split": split, "key": key}
                if ex is None:
                    try:
                        save(compute_lane(job))
                    except Exception as exc:  # noqa: BLE001
                        counts["failed"] += 1
                        log(f"  {sym} {tf}: FAILED {type(exc).__name__}: {exc}")
                else:
                    pending[ex.submit(compute_lane, job)] = (sym, tf)
                    drain(workers * 2)
        drain(0)
    finally:
        if ex is not None:
            ex.shutdown(wait=True)
    summary = aggregate(out, symbols, timeframes)
    return {"out": str(out), "lanes": counts, **summary}


# --------------------------------------------------------------------------- reports


def _pct(x: Any) -> str:
    return "-" if x is None else f"{100 * float(x):+.2f}%"


def aggregate(out: Path, symbols: Sequence[str] | None = None, timeframes: Sequence[str] | None = None
              ) -> dict[str, Any]:
    """Read every lane file and write lanes.csv, current.csv, assigned.csv, steps.csv and summary.md."""
    import csv

    lanes: list[dict[str, Any]] = []
    for p in sorted((out / "lanes").glob("*/*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        if symbols and r["symbol"] not in set(symbols):
            continue
        if timeframes and r["timeframe"] not in set(timeframes):
            continue
        lanes.append(r)
    lane_rows, cur_rows, step_rows = [], [], []
    for r in lanes:
        base = {"symbol": r["symbol"], "timeframe": r["timeframe"], "status": r["status"]}
        row = {**base, "bars": r.get("bars"), "data_end": r.get("data_end"), "variants": r.get("variants"),
               "trades": r.get("trades")}
        for per in ("dev", "test"):
            for k, v in (r.get(per) or {}).items():
                row[f"{per}_{k}"] = v
        top = (r.get("current") or [{}])[0]
        row["current_top"] = top.get("strategy_id")
        lane_rows.append(row)
        for i, c in enumerate(r.get("current") or []):
            cur_rows.append({**base, "rank": i + 1, "strategy_id": c["strategy_id"],
                             "params": json.dumps(c["params"], sort_keys=True), "direction": c["direction"],
                             "score": c["score"], "recent_trades": c["recent_trades"],
                             "recent_mean_net": c["recent_mean_net"], "origin": c["origin"],
                             "category": c["category"], "entry": c["entry"], "exit": c["exit"]})
        for s in r.get("steps") or []:
            step_rows.append({**base, **{k: (";".join(v) if isinstance(v, list) else v) for k, v in s.items()}})

    def write(name: str, rows: list[dict[str, Any]]) -> None:
        cols: list[str] = []
        for x in rows:
            cols += [k for k in x if k not in cols]
        with (out / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols or ["symbol"])
            w.writeheader()
            w.writerows(rows)

    write("lanes.csv", lane_rows)
    write("current.csv", cur_rows)
    write("assigned.csv", [c for c in cur_rows if c["status"] in ("ASSIGNED", "WATCH")])
    write("steps.csv", step_rows)
    by_tf: dict[str, dict[str, int]] = {}
    for r in lane_rows:
        d = by_tf.setdefault(r["timeframe"], {})
        d[r["status"]] = d.get(r["status"], 0) + 1
    pooled = {}
    for per in ("dev", "test"):
        n = sum(int(r.get(f"{per}_pick_trades") or 0) for r in lane_rows)
        s = sum((r.get(f"{per}_pick_mean_net") or 0) * int(r.get(f"{per}_pick_trades") or 0) for r in lane_rows)
        cn = sum(int(r.get(f"{per}_ctrl_trades") or 0) for r in lane_rows)
        cs = sum((r.get(f"{per}_ctrl_mean_net") or 0) * int(r.get(f"{per}_ctrl_trades") or 0) for r in lane_rows)
        pooled[per] = {"pick_trades": n, "pick_mean_net": s / n if n else None, "ctrl_trades": cn,
                       "ctrl_mean_net": cs / cn if cn else None}
    lines = ["# Recency research: what works now", "",
             "Recent winners are picked using only past trades, then checked on the following week/month.",
             f"Lanes: {len(lane_rows)}.", "",
             "| Period | Picks' trades | Picks avg/trade | Everything-active avg/trade |", "|---|---|---|---|"]
    for per, name in (("dev", "Development (to split date)"), ("test", "Test (after split date)")):
        p = pooled[per]
        lines.append(f"| {name} | {p['pick_trades']} | {_pct(p['pick_mean_net'])} | {_pct(p['ctrl_mean_net'])} |")
    lines += ["", "| TF | " + " | ".join(("ASSIGNED", "WATCH", "DROPPED_IN_TEST", "NOT_USED", "NO_DATA")) + " |",
              "|---" * 6 + "|"]
    for tf in [x for x in TF_ALL + ("1m",) if x in by_tf]:
        d = by_tf[tf]
        lines.append(f"| {tf} | " + " | ".join(str(d.get(k, 0)) for k in
                                               ("ASSIGNED", "WATCH", "DROPPED_IN_TEST", "NOT_USED", "NO_DATA")) + " |")
    asg = [r for r in lane_rows if r["status"] == "ASSIGNED"]
    lines += ["", f"## Assigned now ({len(asg)})", ""]
    for r in sorted(asg, key=lambda x: -(x.get("test_pick_mean_net") or 0))[:40]:
        lines.append(f"- {r['symbol']} {r['timeframe']}: {r['current_top'] or '(no current pick)'} - test "
                     f"{_pct(r.get('test_pick_mean_net'))}/trade over {r.get('test_pick_trades')} trades")
    lines += ["", "ASSIGNED = method held before and after the split date. WATCH = held before, too few trades after.",
              "Files: lanes.csv, current.csv (today's ranking), assigned.csv, steps.csv (every walk-forward step)."]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"lanes_reported": len(lane_rows), "by_timeframe": by_tf, "pooled": pooled}


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    from .context import App
    from .research import resolve_universe
    from .settings import REPO

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(REPO / "config" / "app" / "gplm.windows.json"))
    ap.add_argument("--home", help="workspace (default from the config)")
    ap.add_argument("--stocks")
    ap.add_argument("--universe", help="all | nifty500 | file.csv | file.json | file.txt (one symbol per line)")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--timeframes", default="all", help="all (5m,15m,30m,60m,1d,1w) or a comma list")
    ap.add_argument("--name", required=True, help="output <home>/reports/recency/<name> (reuse to resume/refresh)")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--max-combos", type=int, default=4, help="parameter combinations per strategy (defaults first)")
    ap.add_argument("--top-k", type=int, default=Policy.top_k)
    ap.add_argument("--half-life", type=float, default=Policy.half_life,
                    help="recency weight half-life as a fraction of the lookback (default 0.25; smaller = more recent)")
    ap.add_argument("--strategies", help="library id patterns (comma list, globs ok)")
    ap.add_argument("--library", action="append", help="extra strategy-library root (repeatable), e.g. "
                                                       "state-intraday/strategy-library")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--report-only", action="store_true", help="only rebuild the CSV/summary from lane files")
    a = ap.parse_args(argv)
    app = App(a.config, a.home, quiet=True)
    try:
        tfs = list(TF_ALL) if a.timeframes == "all" else [x.strip() for x in a.timeframes.split(",") if x.strip()]
        bad = sorted(set(tfs) - set(WINDOWS))
        if bad:
            raise SystemExit(f"unsupported timeframes {bad}; use {sorted(WINDOWS)}")
        out = Path(a.out).resolve() if a.out else app.s.reports_dir / "recency" / a.name
        if a.report_only:
            print(json.dumps(aggregate(out, None, tfs), indent=2, default=str))
            return 0
        uni = a.universe
        if uni and uni.lower().endswith(".txt"):
            names = [x.strip() for x in Path(uni).read_text(encoding="utf-8-sig").splitlines()
                     if x.strip() and not x.startswith("#")]
            a.stocks, uni = ",".join(n if n.endswith("-EQ") else n + "-EQ" for n in names), None
        syms, basis = resolve_universe(app, a.stocks, uni, a.max_symbols)
        pats = [x.strip() for x in (a.strategies or "").split(",") if x.strip()] or None
        print(f"Recency research: {len(syms)} stock(s) [{basis}] x {tfs} -> {out}", flush=True)
        res = run(app, syms, tfs, out, workers=a.workers, max_combos=a.max_combos, policy=Policy(top_k=a.top_k, half_life=a.half_life),
                  patterns=pats, library_extra=a.library, start=a.start, end=a.end,
                  log=lambda s: print(s, flush=True))
        print(json.dumps({k: res[k] for k in ("lanes", "by_timeframe", "pooled")}, indent=2, default=str))
        print(f"summary: {out / 'summary.md'}")
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
