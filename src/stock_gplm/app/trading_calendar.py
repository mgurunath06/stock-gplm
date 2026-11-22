"""NSE equity trading calendar 2010..2026 from candles already on disk (owner 2026-11-22). No network.

    python -m stock_gplm.app.trading_calendar [--stocks 200] [--from 2010-01-01]

Ground truth = days the market actually traded: a date is a session when at least half of the sample stocks that
were listed at the time (first <= date <= last candle) have a daily candle on it. This catches ad-hoc closures
(e.g. 2024-01-22, 2024-11-20, 2026-01-15) and special weekend sessions (Budget Saturdays, DR drills, Muhurat)
that weekday rules miss. Days after the last observed session up to 2026-12-31 are scheduled weekdays minus the
official 2026 holidays below.
Sample: the first --stocks lines of the universe (Nifty 50 first). Sources: data/derived/yahoo-adjusted (to 2022)
and data/raw/shoonya (from 2022-10-31).
Writes data/reference/nse_trading_calendar.csv (date, weekday, weekend_session, source, coverage) and
state/reports/calendar/<today>/CALENDAR.md (sessions per year, checks against the official 2026 list, weekend
sessions, per-stock missing sessions).
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "data" / "reference" / "nse_trading_calendar.csv"
SOURCES = [ROOT / "data" / "derived" / "yahoo-adjusted" / "NSE", ROOT / "data" / "raw" / "shoonya" / "NSE"]
UNIVERSE = ROOT / "app" / "deploy" / "oracle" / "live" / "universe.txt"
# NSE circular NSE/CMTR/71775 + Maharashtra municipal election (2026-01-15). Weekday closures only.
HOLIDAYS_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
                 "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
                 "2026-11-24", "2026-12-25"]
KNOWN_ADHOC = ["2024-01-22", "2024-11-20", "2026-01-15"]          # must be closed
KNOWN_WEEKEND = ["2024-03-02", "2024-05-18", "2025-02-01"]        # must be open (Muhurat days vary by source)


def stock_days(sym: str) -> set[str]:
    from .universe_build import _rows
    out: set[str] = set()
    for root in SOURCES:
        d = root / f"{sym}-EQ"
        if d.is_dir():
            out |= set(_rows(d))
    return out


def build(n_stocks: int, start: str) -> tuple[Path, Path]:
    syms = [x.strip().upper().removesuffix("-EQ") for x in UNIVERSE.read_text(encoding="utf-8").split() if x.strip()]
    per = {s: d for s in syms[:n_stocks] if (d := {x for x in stock_days(s) if x >= start})}
    seen = Counter(x for d in per.values() for x in d)
    span = {s: (min(d), max(d)) for s, d in per.items()}
    sessions, cov = [], {}
    for day in sorted(seen):
        alive = sum(1 for a, b in span.values() if a <= day <= b)
        c = seen[day] / alive if alive else 0.0
        if c >= 0.5:
            sessions.append(day)
            cov[day] = c
    last = sessions[-1]
    hol = set(HOLIDAYS_2026)
    sched, d = [], date.fromisoformat(last) + timedelta(days=1)
    while d <= date(2026, 12, 31):
        if d.weekday() < 5 and d.isoformat() not in hol:
            sched.append(d.isoformat())
        d += timedelta(days=1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "weekend_session", "source", "coverage"])
        for x in sessions:
            wd = date.fromisoformat(x).weekday()
            w.writerow([x, date.fromisoformat(x).strftime("%a"), int(wd >= 5), "observed", round(cov[x], 3)])
        for x in sched:
            w.writerow([x, date.fromisoformat(x).strftime("%a"), 0, "scheduled", ""])
    # checks
    obs = set(sessions)
    years = Counter(x[:4] for x in sessions + sched)
    weekend = [x for x in sessions if date.fromisoformat(x).weekday() >= 5]
    bad_hol = [x for x in HOLIDAYS_2026 + KNOWN_ADHOC if x in obs]
    miss_we = [x for x in KNOWN_WEEKEND if x not in obs]
    weak = [x for x in sessions if cov[x] < 0.8]
    gaps = sorted(((s, sum(1 for x in sessions if span[s][0] <= x <= span[s][1] and x not in d))
                   for s, d in per.items()), key=lambda t: -t[1])
    rep = ROOT / "state" / "reports" / "calendar" / date.today().isoformat() / "CALENDAR.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    L = [f"# NSE trading calendar {start[:4]}..2026 ({date.today()})", "",
         f"Sample {len(per)} stocks. Observed sessions {len(sessions)} ({sessions[0]} .. {last}), scheduled after "
         f"that {len(sched)}. File: {OUT}", "",
         "## Checks", "",
         f"* Official 2026 holidays / known ad-hoc closures seen as sessions (should be none): {', '.join(bad_hol) or 'none'}",
         f"* Known special weekend sessions missing (should be none): {', '.join(miss_we) or 'none'}",
         f"* Sessions with coverage < 80% (inspect): {', '.join(weak) or 'none'}", "",
         "## Sessions per year", "", "| Year | Sessions |", "|---|---|",
         *[f"| {y} | {n} |" for y, n in sorted(years.items())], "",
         "## Weekend sessions observed", "", ", ".join(weekend) or "-", "",
         "## Stocks with most missing sessions inside their own history (data gaps)", "",
         "| Stock | Missing |", "|---|---|", *[f"| {s} | {n} |" for s, n in gaps[:30]], ""]
    rep.write_text("\n".join(L), encoding="utf-8")
    return OUT, rep


def load(path: Path = OUT) -> list[str]:
    """All sessions (observed + scheduled), sorted; [] when the file is missing."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return [r["date"] for r in csv.DictReader(f)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--from", dest="start", default="2010-01-01")
    a = ap.parse_args(argv)
    out, rep = build(a.stocks, a.start)
    print(f"{out}\n{rep}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
