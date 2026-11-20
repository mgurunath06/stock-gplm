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
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from . import strategy_dsl as dsl
from .context import App
from .ingest import ingest_many
from .lanes import all_bars, build_lane
from .research import _ctx_for
from .strategy_engine import Exit, simulate
from .strategy_library import StrategyLibrary

REPO = Path(__file__).resolve().parents[3]
SPLIT = "2026-08-14"  # first day of the out-of-sample test period (owner decision)


def run(stock: str, start: str, end: str | None, tfs: list[str], config: str, name: str) -> Path:
    app = App(config, None, quiet=True)
    out = app.s.reports_dir / "forward" / name
    out.mkdir(parents=True, exist_ok=True)
    lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], out / "_lib").load(record=False)
    strategies = [s for s in lib.strategies.values() if s.doc.get("enabled", True)]
    data = next(iter(ingest_many(app.s, app.db, [stock], use_minute=True).values()))
    bars = all_bars(data)
    costs = app.s.section("costs")
    trades: list[dict[str, Any]] = []
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
            short = s.direction == "short"
            tr = simulate(m, mask, (a, b), Exit.of(s.doc["exit"]), lane.costs.buy, lane.costs.sell, short, xm)
            status[(s.id, tf)] = "OK"
            for t in tr:
                trades.append({"strategy_id": s.id, "name": s.doc.get("name", s.id), "timeframe": tf,
                               "side": s.direction, "signal": m.label(t["t"]), "entry": m.label(t["t"] + 1),
                               "exit": m.label(t["exit_index"]), "entry_price": round(t["entry_price"], 2),
                               "exit_price": round(t["exit_price"], 2), "exit_reason": t["exit_reason"],
                               "net_pct": round(100 * t["net_return"], 3), "gross_pct": round(100 * t["gross_return"], 3),
                               "bars_held": t["bars_held"], "max_up_pct": round(100 * t["mfe"], 3),
                               "max_down_pct": round(100 * t["mae"], 3)})
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0]) if trades else ["strategy_id"])
        w.writeheader()
        w.writerows(trades)
    (out / "summary.md").write_text(_summary(stock, start, end, tfs, trades, status, strategies), encoding="utf-8")
    return out


def _stats(ts: list[dict[str, Any]]) -> str:
    if not ts:
        return "- | - | - | -"
    net = np.array([t["net_pct"] for t in ts])
    win = net[net > 0].sum()
    loss = -net[net <= 0].sum()
    pf = f"{win / loss:.2f}" if loss > 0 else "-"
    return f"{len(ts)} | {(net > 0).mean():.0%} | {net.mean():+.2f}% | {net.sum():+.1f}% | {pf}"


def _summary(stock, start, end, tfs, trades, status, strategies) -> str:
    names = {s.id: s.doc.get("name", s.id) for s in strategies}
    by = defaultdict(list)
    for t in trades:
        by[(t["strategy_id"], t["timeframe"])].append(t)
    lines = [f"# Forward test {stock}: {start} .. {end or 'latest'}", "",
             "Every trigger traded with the strategy's exit (owner swing exit), default parameters, costs included. "
             f"Columns: trades | win rate | average net | total net (sum of trades) | profit factor. "
             f"Split at {SPLIT} (before = development window, after = out-of-sample test period).", ""]
    for tf in tfs:
        rows = sorted({k for k in status if k[1] == tf}, key=lambda k: -sum(t["net_pct"] for t in by.get(k, [])))
        if not rows:
            continue
        lines += [f"## {tf}", "", "| Strategy | Side | All 2026: trades | win | avg | total | PF | "
                  f"Before {SPLIT}: trades | win | avg | total | PF | From {SPLIT}: trades | win | avg | total | PF |",
                  "|---|---|" + "---|" * 15]
        for k in rows:
            ts = by.get(k, [])
            side = next((s.direction for s in strategies if s.id == k[0]), "")
            if status[k] != "OK":
                lines.append(f"| {names[k[0]]} ({k[0]}) | {side} | {status[k]} |" + " |" * 14)
                continue
            pre = [t for t in ts if t["signal"][:10] < SPLIT]
            post = [t for t in ts if t["signal"][:10] >= SPLIT]
            lines.append(f"| {names[k[0]]} ({k[0]}) | {side} | {_stats(ts)} | {_stats(pre)} | {_stats(post)} |")
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
    a = ap.parse_args(argv)
    out = run(a.stock.upper(), a.start, a.end, a.timeframes.split(","), a.config,
              a.name or f"{a.stock.lower()}-{a.start[:4]}")
    print(out / "summary.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
