"""Owner-run end-to-end check of stock_gplm.app.recency (real local candles, writes only to a temp folder).

    $env:PYTHONPATH="src"; python tests/owner/check_recency.py [--stocks RELIANCE,TCS,INFY] [--library ..\\state-intraday\\strategy-library]

Checks: offline unit tests; full run on all timeframes (no lane fails); outputs written; current picks obey
the rules (enough recent trades, positive score); dev/test steps sit on the right side of the split date;
a second identical run reuses every lane; NO LOOK-AHEAD: a run on candles cut at --cut gives the same
picks for every step decided before the cut. Prints PASS/FAIL per check and the summary.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_gplm.app import recency as rc  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def run(args: list[str]) -> tuple[int, str, float]:
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "stock_gplm.app.recency", *args], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    return r.returncode, r.stdout + r.stderr, time.monotonic() - t0


def lanes(out: Path) -> dict[tuple[str, str], dict]:
    return {(p.parent.name, p.stem): json.loads(p.read_text(encoding="utf-8")) for p in (out / "lanes").glob("*/*.json")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", default="RELIANCE,TCS,INFY")
    ap.add_argument("--timeframes", default="all")
    ap.add_argument("--library", action="append", default=[])
    ap.add_argument("--cut", default="2026-10-15", help="cut date for the look-ahead check")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    a = ap.parse_args()

    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(ROOT / "tests" / "test_recency.py")], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    check("offline unit tests", r.returncode == 0, (r.stdout.strip().splitlines() or [r.stderr[-200:]])[-1])

    tmp = Path(tempfile.mkdtemp(prefix="recency-check-"))
    full, cut = tmp / "full", tmp / "cut"
    base = ["--stocks", a.stocks, "--timeframes", a.timeframes, "--name", "check", "--workers", str(a.workers)]
    for lib in a.library:
        base += ["--library", lib]
    try:
        code, text, secs = run(base + ["--out", str(full)])
        check("full run exits 0", code == 0, f"{secs:.0f} s" if code == 0 else text[-800:])
        if code != 0:
            return 1
        res = lanes(full)
        n_tf = len(rc.TF_ALL) if a.timeframes == "all" else len(a.timeframes.split(","))
        n_exp = len(a.stocks.split(",")) * n_tf
        check("one lane file per stock x timeframe", len(res) == n_exp, f"{len(res)} of {n_exp}")
        check("no lane FAILED", "FAILED" not in text, "")
        check("reports written", all((full / f).is_file() for f in
                                     ("lanes.csv", "current.csv", "assigned.csv", "steps.csv", "summary.md")))

        bad = []
        for (sym, tf), r_ in res.items():
            w = rc.WINDOWS[tf]
            for c in r_.get("current") or []:
                if c["recent_trades"] < w.min_trades or not c["score"] > 0:
                    bad.append(f"{sym} {tf} {c['strategy_id']}")
        check("current picks: enough recent trades and positive score", not bad, ", ".join(bad[:5]))

        import numpy as np
        split = None
        wrong = []
        for (sym, tf), r_ in res.items():
            split = split or json.loads((full / "run-config.json").read_text())["split_development_end"]
            sd = int(np.datetime64(split, "D").astype(np.int64))
            step = rc.WINDOWS[tf].step_days
            for s in r_.get("steps") or []:
                d = int(np.datetime64(s["date"], "D").astype(np.int64))
                if s["period"] == "dev" and d + step > sd + 1 or s["period"] == "test" and d <= sd:
                    wrong.append(f"{sym} {tf} {s['date']}")
        check(f"steps on the right side of the split ({split})", not wrong, ", ".join(wrong[:5]))

        code, text, secs = run(base + ["--out", str(full)])
        check("second identical run reuses every lane", code == 0 and '"computed": 0' in text, f"{secs:.0f} s")

        code, text, _ = run(base + ["--out", str(cut), "--end", a.cut])
        check(f"cut run (--end {a.cut}) exits 0", code == 0, "" if code == 0 else text[-800:])
        cres = lanes(cut)
        diff, compared = [], 0
        cd = int(np.datetime64(a.cut, "D").astype(np.int64))
        for key, r_ in cres.items():
            f_ = res.get(key)
            if not f_ or r_["status"] == "NO_DATA":
                continue
            step = rc.WINDOWS[key[1]].step_days
            fsteps = {s["date"]: s["picks"] for s in f_.get("steps") or []}
            for s in r_.get("steps") or []:
                if int(np.datetime64(s["date"], "D").astype(np.int64)) + step > cd:
                    continue
                compared += 1
                if fsteps.get(s["date"]) != s["picks"]:
                    diff.append(f"{key[0]} {key[1]} {s['date']}")
        check("no look-ahead: same picks before the cut date", compared > 0 and not diff,
              f"{compared} steps compared; differ: {', '.join(diff[:5])}")

        print("\n" + (full / "summary.md").read_text(encoding="utf-8")[:3000])
    finally:
        if a.keep:
            print(f"kept: {tmp}")
        else:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
