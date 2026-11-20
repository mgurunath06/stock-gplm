"""Run forward_test for many stocks in parallel, then one report across all of them (owner 2026-11-20).

    python -m stock_gplm.app.forward_batch --stocks @deploy/oracle/live/watchlist.txt --from 2026-01-19 \
        --name watchlist-2026 --learn-exits --workers 6

Each stock: <home>/reports/forward/<name>/<SYM>/ (summary.md, trades.csv); per-strategy exits learned into
config/strategies/exits.json. Resumable: stocks with a finished summary.md are skipped (--force redoes them);
failures are retried once, one at a time. At the end: <name>/ALL_STOCKS.md + all_trades.csv and a .done marker.
CPU only (numpy); every process reads candles only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from .settings import load

REPO = Path(__file__).resolve().parents[3]
SKIP = {"NIFTY", "BANKNIFTY", "INDIAVIX", "FINNIFTY"}


def read_stocks(arg: str) -> list[str]:
    if arg.startswith("@"):
        text = Path(arg[1:]).read_text(encoding="utf-8")
        raw = [x for ln in text.splitlines() for x in ln.split("#", 1)[0].replace(",", " ").split()]
    else:
        raw = arg.replace(",", " ").split()
    return [s.upper() for s in dict.fromkeys(raw) if s.upper() not in SKIP]


def one(sym: str, a: argparse.Namespace, root: Path, log: Path) -> tuple[str, int]:
    cmd = [sys.executable, "-m", "stock_gplm.app.forward_test", "--stock", sym, "--from", a.start,
           "--timeframes", a.timeframes, "--config", a.config, "--name", f"{a.name}/{sym}"]
    if a.end:
        cmd += ["--to", a.end]
    if a.learn_exits:
        cmd += ["--learn-exits"]
    t0 = time.monotonic()
    with (root / sym).with_suffix(".log").open("w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO).returncode
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {sym} rc={rc} {time.monotonic() - t0:.0f}s\n")
    return sym, rc


def report(root: Path, stocks: list[str], start: str) -> Path:
    frames = []
    for s in stocks:
        f = root / s / "trades.csv"
        if f.is_file() and f.stat().st_size > 20:
            d = pd.read_csv(f)
            d.insert(0, "stock", s)
            frames.append(d)
    if not frames:
        (root / "ALL_STOCKS.md").write_text("no trades\n", encoding="utf-8")
        return root / "ALL_STOCKS.md"
    t = pd.concat(frames, ignore_index=True)
    t.to_csv(root / "all_trades.csv", index=False)
    win = lambda x: (x > 0).mean()  # noqa: E731
    g = t.groupby(["timeframe", "strategy_id", "name", "side"])
    per_stock = t.groupby(["timeframe", "strategy_id", "stock"]).net_pct.sum().reset_index()
    prof = per_stock.groupby(["timeframe", "strategy_id"]).net_pct.agg(stocks="size", profitable=lambda x: (x > 0).sum())
    s = g.net_pct.agg(trades="size", win=win, avg="mean", total="sum").reset_index()
    s = s.merge(prof.reset_index(), on=["timeframe", "strategy_id"])
    best = per_stock.sort_values("net_pct", ascending=False).groupby(["timeframe", "strategy_id"]).head(3) \
        .groupby(["timeframe", "strategy_id"]).apply(lambda x: ", ".join(f"{r.stock} {r.net_pct:+.1f}%" for r in x.itertuples()),
                                                      include_groups=False).rename("best_stocks").reset_index()
    s = s.merge(best, on=["timeframe", "strategy_id"], how="left")
    lines = [f"# All stocks: {len(frames)} stocks, {len(t)} trades, from {start}", "",
             "Per strategy x timeframe across all stocks (each stock with its own learned exits). "
             "Profitable = stocks where the strategy's total net > 0.", ""]
    for tf in ("1d", "60m", "15m", "5m"):
        x = s[s.timeframe == tf].sort_values("total", ascending=False)
        if x.empty:
            continue
        lines += [f"## {tf}", "", "| Strategy | Side | Stocks | Profitable stocks | Trades | Win | Avg net | Total net | Best stocks |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in x.itertuples():
            lines.append(f"| {r.name} ({r.strategy_id}) | {r.side} | {r.stocks} | {r.profitable} ({r.profitable / r.stocks:.0%}) | "
                         f"{r.trades} | {r.win:.0%} | {r.avg:+.2f}% | {r.total:+.1f}% | {r.best_stocks} |")
        lines.append("")
    st = t.groupby(["stock", "timeframe"]).net_pct.agg(trades="size", win=win, total="sum").reset_index()
    lines += ["## Per stock (all strategies together)", "", "| Stock | TF | Trades | Win | Total net |", "|---|---|---|---|---|"]
    for r in st.sort_values(["stock", "timeframe"]).itertuples():
        lines.append(f"| {r.stock} | {r.timeframe} | {r.trades} | {r.win:.0%} | {r.total:+.1f}% |")
    out = root / "ALL_STOCKS.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stocks", required=True, help="comma list or @file")
    ap.add_argument("--from", dest="start", default="2026-01-19")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--timeframes", default="5m,15m,60m,1d")
    ap.add_argument("--config", default=str(REPO / "config" / "app" / "gplm.windows.json"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--learn-exits", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    stocks = read_stocks(a.stocks)
    root = load(Path(a.config)).reports_dir / "forward" / a.name
    root.mkdir(parents=True, exist_ok=True)
    log = root / "batch.log"
    todo = [s for s in stocks if a.force or not (root / s / "summary.md").is_file()]
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} start: {len(stocks)} stocks, {len(todo)} to run, workers {a.workers}\n")
    with ThreadPoolExecutor(a.workers) as ex:
        failed = [s for s, rc in ex.map(lambda s: one(s, a, root, log), todo) if rc]
    for s in failed:  # one retry each, alone
        one(s, a, root, log)
    out = report(root, stocks, a.start)
    (root / "batch.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
