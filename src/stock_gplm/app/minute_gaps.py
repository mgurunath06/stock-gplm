"""Minute-data gap check for the watchlist against the NSE trading calendar (read-only).

Expected sessions = weekdays in [--start, --end] minus calendar closed dates. For every stock it reports
MISSING sessions (no completed file), SHORT sessions (fewer than --min-records 1-minute bars in the metadata)
and EXTRA sessions present but not expected (e.g. weekend special sessions). Exit code 0 only when nothing is
missing or short, so scripts can wait on it.
Usage (PYTHONPATH=src): python -m stock_gplm.app.minute_gaps [--start 2026-01-01] [--end yesterday] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .live_lab import INDICES, REPO


def expected_sessions(start: str, end: str, cal: dict[str, Any]) -> list[str]:
    closed = set(cal.get("closed_dates") or [])
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5 and d.isoformat() not in closed:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def check(roots: list[Path], syms: list[str], start: str, end: str, min_records: int) -> dict[str, Any]:
    cal = json.loads((REPO / "config" / "calendar" / f"nse_{start[:4]}.json").read_text())
    exp = expected_sessions(start, end, cal)
    per: dict[str, Any] = {}
    for s in syms:
        have: dict[str, int | None] = {}
        for r in roots:
            d = Path(r) / f"{s}-EQ"
            if not d.is_dir():
                continue
            for f in os.listdir(d):
                if f.endswith(".json.gz.done") and f[10] == "." and start <= f[:10] <= end and f[:10] not in have:
                    try:
                        have[f[:10]] = json.loads((d / f"{f[:10]}.meta.json").read_text()).get("records")
                    except (OSError, ValueError):
                        have[f[:10]] = None
        missing = [x for x in exp if x not in have]
        short = {x: n for x, n in have.items() if x in exp and n is not None and n < min_records}
        extra = sorted(x for x in have if x not in exp)
        per[s] = {"have": len(have), "missing": missing, "short": short, "extra": extra}
    bad = {s: v for s, v in per.items() if v["missing"] or v["short"]}
    return {"window": [start, end], "expected_sessions": len(exp), "stocks": len(syms),
            "complete": len(syms) - len(bad), "with_gaps": len(bad), "per_stock": per}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--min-records", type=int, default=360, help="1-minute bars below which a session is SHORT")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    wl = (REPO / "deploy" / "oracle" / "live" / "watchlist.txt").read_text().splitlines()
    syms = [s for ln in wl for s in ln.split("#", 1)[0].split() if s not in INDICES]
    res = check([Path(r) for r in st.minute_roots], syms, a.start, a.end, a.min_records)
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"window {a.start}..{a.end}: {res['expected_sessions']} trading sessions expected; "
              f"{res['complete']}/{res['stocks']} stocks complete, {res['with_gaps']} with gaps")
        for s, v in res["per_stock"].items():
            if v["missing"] or v["short"] or v["extra"]:
                m = v["missing"]
                ms = f"{len(m)} missing ({m[0]}..{m[-1]})" if len(m) > 3 else f"{len(m)} missing {m}"
                print(f"  {s:12s} have {v['have']:3d}  {ms}  short {v['short'] or '-'}  extra {v['extra'] or '-'}")
    sys.exit(0 if res["with_gaps"] == 0 else 1)


if __name__ == "__main__":
    main()
