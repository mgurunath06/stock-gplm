"""Live-lab scorecard: how many of today's BUY setups reached target vs stop (PAPER/TEST).

Reads every events*.jsonl of one live-lab day (restarts rotate the file), dedups setups by key, and prints:
setups, closed, TARGET / STOP / time-limit / market-close counts, hit rate, avg and total % per setup,
split LIVE vs catch-up (catch-up = replayed after a late start, less honest) and by confidence and timeframe.
Writes scorecard-live.md next to the events. --watch N reprints every N seconds until Ctrl+C.

  python -m stock_gplm.app.live_scorecard [--date 2026-11-23] [--watch 300]
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
IST = timezone(timedelta(hours=5, minutes=30))
CONF = re.compile(r"confidence (\w+)")
RES = re.compile(r"(target reached|stop hit|time limit reached|market close)\s*\(?([+-]?\d+(?:\.\d+)?)%")
RES_KIND = {"target reached": "TARGET", "stop hit": "STOP", "time limit reached": "EXPIRED", "market close": "EOD"}


def load(day_dir: Path) -> tuple[dict, dict]:
    setups, outcomes = {}, {}
    files = sorted(day_dir.glob("events-*.jsonl")) + [day_dir / "events.jsonl"]
    for f in files:
        if not f.is_file():
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                k, key, title = e.get("kind"), e.get("key"), e.get("title") or ""
                if k == "SETUP" and key not in setups:
                    d = e.get("data") or {}
                    m = CONF.search(title)
                    setups[key] = {"symbol": e.get("symbol"), "tf": d.get("tf") or key.split("|")[1],
                                   "conf": (m.group(1) if m else "?").upper(),
                                   "catch_up": bool(d.get("catch_up", "catch-up" in title))}
                elif k == "OUTCOME":
                    d = e.get("data") or {}
                    if d.get("result"):
                        outcomes[key] = (d["result"], float(d.get("pnl_pct") or 0.0))
                    else:
                        m = RES.search(title)
                        if m:
                            outcomes[key] = (RES_KIND[m.group(1)], float(m.group(2)))
    return setups, outcomes


def stats(rows: list[tuple[str, float]], n_setups: int) -> str:
    c = defaultdict(int)
    for r, _ in rows:
        c[r] += 1
    n = len(rows)
    if not n:
        return f"| {n_setups} | 0 | - | - | - | - | - | - | - |"
    wins = sum(1 for _, p in rows if p > 0)
    tot = sum(p for _, p in rows)
    return (f"| {n_setups} | {n} | {c['TARGET']} | {c['STOP']} | {c['EXPIRED'] + c['EOD']} | "
            f"{100 * c['TARGET'] / n:.0f}% | {100 * wins / n:.0f}% | {tot / n:+.2f}% | {tot:+.1f}% |")


def report(date: str, day_dir: Path) -> str:
    setups, outcomes = load(day_dir)
    head = ("| setups | closed | target | stop | time/close | target hit | in profit | avg/setup | sum |\n"
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    out = [f"# Live scorecard {date}  (updated {datetime.now(IST):%H:%M:%S} IST, PAPER/TEST)", ""]

    def block(title: str, keys: list[str]) -> None:
        out.extend([f"## {title}", head, stats([outcomes[k] for k in keys if k in outcomes], len(keys)), ""])

    live = [k for k, s in setups.items() if not s["catch_up"]]
    block("All setups", list(setups))
    block("LIVE only (raised in real time - the honest number)", live)
    block("Catch-up (replayed after a late start)", [k for k in setups if setups[k]["catch_up"]])
    for label, field in (("confidence", "conf"), ("timeframe", "tf")):
        out.append(f"## LIVE by {label}")
        out.append(head.replace("| setups", f"| {label} | setups").replace("|---:", "|---|---:", 1))
        for v in sorted({setups[k][field] for k in live}):
            ks = [k for k in live if setups[k][field] == v]
            out.append(f"| {v} " + stats([outcomes[k] for k in ks if k in outcomes], len(ks)))
        out.append("")
    if not setups:
        out.append("No setups yet (feed not live, or market not open).")
    out.append("target hit = closed setups that reached target; in profit = closed with pnl > 0 "
               "(time-limit/close exits can still be green). Open setups are not counted until they close.")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=datetime.now(IST).date().isoformat())
    ap.add_argument("--out", default=str(REPO / "var" / "live-lab"))
    ap.add_argument("--watch", type=int, default=0, help="reprint every N seconds (0 = once)")
    a = ap.parse_args()
    day_dir = Path(a.out) / a.date
    while True:
        text = report(a.date, day_dir)
        print("\n" + text, flush=True)
        if day_dir.is_dir():
            (day_dir / "scorecard-live.md").write_text(text, encoding="utf-8")
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
