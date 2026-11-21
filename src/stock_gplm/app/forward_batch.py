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
    from .confidence import write_reports
    return write_reports(root, t, f"from {start}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stocks", required=True, help="comma list or @file")
    ap.add_argument("--from", dest="start", default="2026-01-19")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--timeframes", default="5m,15m,30m,60m,1d,1w,1mo")
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
