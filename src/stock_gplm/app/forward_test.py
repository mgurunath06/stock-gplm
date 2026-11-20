"""Plain trade-by-trade test of the library over a date range (owner 2026-11-20: "run for entire 2026").

    python -m stock_gplm.app.forward_test --stock RELIANCE --from 2026-01-01 [--to 2026-11-20]
        [--timeframes 5m,15m,60m,1d] [--config config/app/gplm.windows.json] [--name reliance-2026]

No optimisation, no gates, no date lock: every enabled library strategy with its default parameters; every
trigger in the range is traded with the strategy's exit (owner swing exit: stop 0.2% beyond the last swing
low/high, target 2x risk; intraday square-off 15:15, no entries after 14:45), same engine, fills and costs
as research (signal at bar close, entry next open, one trade at a time per strategy). Indicators use all
earlier history (warm-up), trades only start inside the range.
Output <home>/reports/forward/<name>/: trades.csv (every trade), summary.md (per strategy x TF, plus
before / after 2026-08-13 = development end vs test period, and per month).
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from . import strategy_dsl as dsl
from .context import App
from .ingest import ingest_many
from .lanes import all_bars, build_lane
from .research import _ctx_for
from .strategy_engine import EXITS_FILE, exit_for, simulate, swing_settings
import json
from .strategy_library import StrategyLibrary

REPO = Path(__file__).resolve().parents[3]


GRID_MAX = [0.01, 0.015, 0.025, 0.04, None]  # None: always the swing level
GRID_RR = [2.0, 2.5, 3.0]  # owner rule: target at least 2x risk


def run(stock: str, start: str, end: str | None, tfs: list[str], config: str, name: str, learn: bool = False,
        min_trades: int = 20) -> Path:
    app = App(config, None, quiet=True)
    out = app.s.reports_dir / "forward" / name
    out.mkdir(parents=True, exist_ok=True)
    lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], out / "_lib").load(record=False)
    strategies = [s for s in lib.strategies.values() if s.doc.get("enabled", True)]
    data = next(iter(ingest_many(app.s, app.db, [stock], use_minute=True).values()))
    bars = all_bars(data)
    costs = app.s.section("costs")
    trades: list[dict[str, Any]] = []
    learned: list[str] = []
    status: dict[tuple[str, str], str] = {}
    for tf in tfs:
        if tf not in bars or not len(bars[tf]):
            continue
        lane = build_lane(data, tf, costs, bars=bars)
        m = lane.market
        ctx = _ctx_for(bars[tf], tf, lane, bars, {}, None)
        ses = np.asarray(m.sessions).astype(str)
        a = int(np.searchsorted(ses, start))
        b = int(np.searchsorted(ses, end, side="right")) if end else len(m)
        sigs = []
        for s in strategies:
            if tf not in s.timeframes:
                continue
            if s.blocked:
                status[(s.id, tf)] = "BLOCKED: " + s.blocked["reason"][:60]
                continue
            try:
                mask = dsl.signal(s.entry_node, ctx, s.defaults())
                xm = dsl.signal(s.exit_node, ctx, s.defaults()) if s.exit_node is not None else None
            except (dsl.MissingData, dsl.DSLError) as exc:
                status[(s.id, tf)] = f"NOT RUN: {exc}"[:80]
                continue
            sigs.append((s, mask, xm))
        if learn and sigs:
            learned.append(_learn(stock, tf, m, sigs, (a, b), lane.costs.buy, lane.costs.sell, min_trades, start, end))
        for s, mask, xm in sigs:
            short = s.direction == "short"
            tr = simulate(m, mask, (a, b), exit_for(s.doc["exit"], stock, tf, s.id), lane.costs.buy, lane.costs.sell, short, xm)
            status[(s.id, tf)] = "OK"
            sws = swing_settings(stock, tf, strategy_id=s.id)
            for t in tr:
                trades.append({"strategy_id": s.id, "name": s.doc.get("name", s.id), "timeframe": tf,
                               "side": s.direction, "signal": m.label(t["t"]), "entry": m.label(t["t"] + 1),
                               "exit": m.label(t["exit_index"]), "entry_price": round(t["entry_price"], 2),
                               "exit_price": round(t["exit_price"], 2), "exit_reason": t["exit_reason"],
                               "net_pct": round(100 * t["net_return"], 3), "gross_pct": round(100 * t["gross_return"], 3),
                               "bars_held": t["bars_held"], "max_up_pct": round(100 * t["mfe"], 3),
                               "max_down_pct": round(100 * t["mae"], 3),
                               "exit_setting": _fmt(sws.get("max_swing_pct"), float(sws.get("rr", 2.0)))})
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]) if trades else ["strategy_id"])
        w.writeheader()
        w.writerows(trades)
    head = ["## Exit settings used", ""] + [f"- {tf}: {swing_settings(stock, tf)}" for tf in tfs] + [""]
    if learned:
        head += ["## Exit learning (all strategies together, this stock x timeframe)", ""] + learned + [""]
    (out / "summary.md").write_text(_summary(stock, start, end, tfs, trades, status, strategies, head), encoding="utf-8")
    return out


@contextmanager
def _exits_lock(timeout: float = 120.0):
    lock = EXITS_FILE.with_name(".exits.lock")
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - t0 > timeout:  # stale lock from a killed run
                lock.unlink(missing_ok=True)
                t0 = time.monotonic()
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def _fmt(mx, rr) -> str:
    return f"{'none' if mx is None else f'{mx:.1%}'} / {rr:g}x"


def _learn(stock, tf, m, sigs, seg, buy, sell, min_trades, start, end, min_strategy_trades: int = 8) -> str:
    """Grid over max_swing_pct x rr. Learned twice, most specific wins when used:
    * stock x timeframe (all its strategies together, >= ``min_trades``): the fallback;
    * stock x timeframe x strategy (>= ``min_strategy_trades`` of its own trades): owner 2026-11-20 "granular".
    Written to exits.json stocks.<SYM>.<tf> (+ .strategies.<id>) with the evidence of the chosen setting."""
    from .strategy_engine import Exit
    base = {k: v for k, v in swing_settings(stock, "*").items()}
    per: dict[tuple, dict[str, list[float]]] = {}
    for mx in GRID_MAX:
        for rr in GRID_RR:
            sw = {**base, "max_swing_pct": mx, "rr": rr}
            res: dict[str, list[float]] = {}
            for s, mask, xm in sigs:
                d = dict(s.doc["exit"])
                if d.get("swing_stop") is None:
                    continue
                d["swing_stop"] = sw
                res[s.id] = [x["net_return"] for x in simulate(m, mask, seg, Exit.of(d), buy, sell,
                                                               s.direction == "short", xm)]
            per[(mx, rr)] = res

    def pick(get, need):
        rows = []
        for (mx, rr), res in per.items():
            net = get(res)
            if len(net) >= need:
                rows.append((sum(net), len(net), mx, rr, float((np.array(net) > 0).mean())))
        return max(rows, key=lambda r: r[0]) if rows else None

    def ev(r):
        return {"from": start, "to": end or "latest", "trades": r[1], "win_rate": round(r[4], 3),
                "total_net_pct": round(100 * r[0], 2)}

    sym = stock.upper().removesuffix("-EQ")
    best = pick(lambda res: [x for v in res.values() for x in v], min_trades)
    entry: dict[str, Any] = {}
    lines = []
    if best:
        entry = {"max_swing_pct": best[2], "rr": best[3], "learned": ev(best)}
        lines.append(f"- {tf} (all strategies): {_fmt(best[2], best[3])} - {best[1]} trades, {best[4]:.0%} win, "
                     f"total {100 * best[0]:+.1f}%")
    else:
        lines.append(f"- {tf} (all strategies): not enough trades; default used")
    strat: dict[str, Any] = {}
    for s, _, _ in sigs:
        r = pick(lambda res, i=s.id: res.get(i, []), min_strategy_trades)
        if r:
            strat[s.id] = {"max_swing_pct": r[2], "rr": r[3], "learned": ev(r)}
    if strat:
        entry["strategies"] = strat
        lines.append(f"  - {len(strat)} strategies learned their own exit (>= {min_strategy_trades} trades); "
                     f"the rest use the line above")
    with _exits_lock():  # parallel stocks (forward_batch) update the same file
        doc = json.loads(EXITS_FILE.read_text(encoding="utf-8")) if EXITS_FILE.is_file() else {"default": {}, "stocks": {}}
        doc.setdefault("stocks", {}).setdefault(sym, {})[tf] = entry
        tmp = EXITS_FILE.with_name(f".exits.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, EXITS_FILE)
    import stock_gplm.app.strategy_engine as eng
    eng._EXITS = None  # reload
    return "\n".join(lines)


def _stats(ts: list[dict[str, Any]]) -> str:
    if not ts:
        return "- | - | - | -"
    net = np.array([t["net_pct"] for t in ts])
    win = net[net > 0].sum()
    loss = -net[net <= 0].sum()
    pf = f"{win / loss:.2f}" if loss > 0 else "-"
    return f"{len(ts)} | {(net > 0).mean():.0%} | {net.mean():+.2f}% | {net.sum():+.1f}% | {pf}"


def _summary(stock, start, end, tfs, trades, status, strategies, head=()) -> str:
    """Per TF: every strategy with Confidence / RR / Edge and ACTIVE status (config/strategies/confidence.json)."""
    import pandas as pd
    from .confidence import score
    names = {s.id: s.doc.get("name", s.id) for s in strategies}
    sides = {s.id: s.direction for s in strategies}
    sc = score(pd.DataFrame(trades).assign(stock=stock)) if trades else pd.DataFrame()
    lines = [f"# Forward test {stock}: {start} .. {end or 'latest'}", "",
             "Every trigger traded with the strategy's exit (owner swing exit: intraday until stop / target / 15:15, "
             "daily until stop / target), default parameters, costs included. Confidence = chance a trade ends in "
             "profit (recent trades weigh more; few trades pulled toward this stock's base rate); "
             "Edge = Confidence x RR - (1 - Confidence). ACTIVE per config/strategies/confidence.json.", "", *head]
    for tf in tfs:
        keys = [k for k in status if k[1] == tf]
        if not keys:
            continue
        x = sc[sc["timeframe"] == tf].set_index("strategy_id") if len(sc) else pd.DataFrame()
        keys.sort(key=lambda k: -(x.loc[k[0], "edge"] if k[0] in x.index else -9))
        lines += [f"## {tf}", "", "| Strategy | Side | Status | Confidence | RR | Edge | Trades | Win | Avg net | Total net | Why not active |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for k in keys:
            nm = f"{names[k[0]]} ({k[0]})"
            if status[k] != "OK":
                lines.append(f"| {nm} | {sides.get(k[0], '')} | {status[k]} |" + " |" * 8)
                continue
            if k[0] not in x.index:
                lines.append(f"| {nm} | {sides.get(k[0], '')} | NO SIGNALS | - | - | - | 0 | - | - | - | |")
                continue
            r = x.loc[k[0]]
            lines.append(f"| {nm} | {r.side} | {r.status} | {r.confidence:.0%} | {r.rr:.2f} | {r.edge:+.2f} | {r.trades} | "
                         f"{r.win_rate:.0%} | {r.avg_net_pct:+.2f}% | {r.total_net_pct:+.1f}% | {r.why} |")
        lines.append("")
    months = defaultdict(list)
    for t in trades:
        months[(t["timeframe"], t["signal"][:7], t["side"])].append(t)
    lines += ["## All strategies together, per month", "", "| TF | Month | Side | Trades | Win | Avg | Total | PF |",
              "|---|---|---|---|---|---|---|---|"]
    for k in sorted(months):
        lines.append(f"| {k[0]} | {k[1]} | {k[2]} | {_stats(months[k])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stock", required=True)
    ap.add_argument("--from", dest="start", default="2026-01-01")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--timeframes", default="5m,15m,60m,1d")
    ap.add_argument("--config", default=str(REPO / "config" / "app" / "gplm.windows.json"))
    ap.add_argument("--name")
    ap.add_argument("--learn-exits", action="store_true",
                    help="try the swing-exit grid per timeframe, save the best per stock in config/strategies/exits.json")
    ap.add_argument("--min-trades", type=int, default=20, help="learning: fewest trades a setting needs")
    a = ap.parse_args(argv)
    out = run(a.stock.upper(), a.start, a.end, a.timeframes.split(","), a.config,
              a.name or f"{a.stock.lower()}-{a.start[:4]}", a.learn_exits, a.min_trades)
    print(out / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
