"""Owner-run end-to-end check of stock_gplm.app.yahoo_history (live Yahoo, writes only to a temp folder).

    $env:PYTHONPATH="src"; python tests/owner/check_yahoo_history.py [--stocks RELIANCE,TCS,INFY]

Checks: offline unit test; fetch; build; every output file passes the project's Shoonya daily validator;
history reaches back 10+ years; no date overlaps Shoonya or the NSE MCP files; the join has no price jump;
ingest reads the extra root. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_gplm.app import nse_history as nh  # noqa: E402
from stock_gplm.app import yahoo_history as yh  # noqa: E402
from stock_gplm.ingestion.daily import validate_daily  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", default="RELIANCE,TCS,INFY")
    a = ap.parse_args()
    stocks = [s.strip().upper() for s in a.stocks.split(",") if s.strip()]
    shoonya, _, _, nse_out = yh.paths()

    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(ROOT / "tests" / "test_yahoo_history.py")], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    check("offline unit test", r.returncode == 0, (r.stdout.strip().splitlines() or [r.stderr[-200:]])[-1])

    nse_ok = [s for s in stocks if (nse_out / f"{s}-EQ").is_dir()]
    if not check("precondition: NSE history present (run nse_history first)", nse_ok == stocks,
                 f"missing {sorted(set(stocks) - set(nse_ok))}"):
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="yahoo-check-"))
    raw, out = tmp / "raw", tmp / "out"
    yh.fetch(stocks, raw, 1.0, False)
    check("fetch: one raw reply per stock", all(any((raw / s).glob("*.json")) for s in stocks))
    rep = yh.build(stocks, raw, out, shoonya, nse_out)
    check("build: every stock joined", all(v.startswith("ok") for v in rep.values()), str(rep)[:400])

    for s in stocks:
        files = sorted((out / f"{s}-EQ").glob("*.json.gz"))
        st = {(q.publication_integrity.status, q.content_quality.status) for q in map(validate_daily, files)}
        check(f"{s}: files pass the daily validator", bool(files) and st == {("PASS", "PASS")}, f"{len(files)} files")
        ydays = nh.shoonya_closes(out / f"{s}-EQ")
        if not ydays:
            continue
        check(f"{s}: reaches back 10+ years", min(ydays) <= "2015-12-31", f"from {min(ydays)}")
        other = nh.shoonya_closes(shoonya / f"{s}-EQ")
        if (nse_out / f"{s}-EQ").is_dir():
            other.update(nh.shoonya_closes(nse_out / f"{s}-EQ"))
        check(f"{s}: no overlap with Shoonya/NSE", not set(ydays) & set(other), f"next source starts {min(other)}")
        last_y, first_o = max(ydays), min(other)
        jump = other[first_o] / ydays[last_y] - 1
        check(f"{s}: join without a price jump", abs(jump) < 0.12, f"{last_y} -> {first_o}: {jump:+.2%}")

    try:
        from stock_gplm.app.context import App
        from stock_gplm.app.ingest import ingest_many
        app = App(nh.CFG, tmp / "home", overrides={"data.daily_extra_roots": [str(out)]}, quiet=True)
        d = next(iter(ingest_many(app.s, app.db, [stocks[0] + "-EQ"], use_minute=False).values()))
        first = str(d.daily["date"].min())
        check("ingest reads the Yahoo root", first <= "2015-12-31", f"{stocks[0]} daily from {first}, {len(d.daily)} days")
        app.close()
    except Exception as e:  # noqa: BLE001
        check("ingest reads the Yahoo root", False, f"{type(e).__name__}: {e}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
