"""Owner-run end-to-end check of stock_gplm.app.nse_history (live NSE MCP, writes only to a temp folder).

    $env:PYTHONPATH="src"; python tests/owner/check_nse_history.py [--stocks RELIANCE,TCS,INFY]

Checks: offline unit test; MCP connect + tool schema; fetch in 3-month chunks until NSE's history ends;
build adjusted files; every output file passes the project's Shoonya daily validator; the adjusted
series joins Shoonya without a price jump; ingest reads the extra root. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_gplm.app import nse_history as nh  # noqa: E402
from stock_gplm.ingestion.daily import validate_daily  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


async def schema() -> dict:
    from stock_gplm.m39.client import NSEMCPClient
    async with NSEMCPClient("bhavcopy", connect_timeout=10, read_timeout=60, retries=2) as c:
        await c.connect()
        return {t["name"]: t for t in await c.list_tools()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", default="RELIANCE,TCS,INFY")
    a = ap.parse_args()
    stocks = [s.strip().upper() for s in a.stocks.split(",") if s.strip()]
    shoonya, _, _ = nh.paths()

    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(ROOT / "tests" / "test_nse_history.py")], capture_output=True, text=True,
                       env={"PYTHONPATH": str(ROOT / "src"), **__import__("os").environ})
    check("offline unit test", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:])

    try:
        tools = asyncio.run(schema())
        props = tools["get_stock_history"]["inputSchema"]["properties"]
        check("MCP connect + get_stock_history schema", {"symbol", "months", "endDate"} <= set(props),
              f"{len(tools)} tools")
    except Exception as e:  # noqa: BLE001
        check("MCP connect + get_stock_history schema", False, f"{type(e).__name__}: {e}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        raw, out = Path(tmp) / "raw", Path(tmp) / "out"
        asyncio.run(nh._fetch(stocks, range(2010, 2024), raw, 0.5))
        for s in stocks:
            chunks = sorted((raw / s).glob("*.json"))
            comp = raw / s / "_complete"
            check(f"{s} fetched", bool(chunks) and comp.exists(),
                  f"{len(chunks)} chunks; stop={json.loads(comp.read_text()).get('stopped_at') if comp.exists() else '-'}")
        nh.build(stocks, raw, out, shoonya)
        rep = json.loads((out / "build-report.json").read_text())
        for s in stocks:
            check(f"{s} built", rep.get(s, "").startswith("ok"), rep.get(s, "missing"))
            files = sorted((out / f"{s}-EQ").glob("*.json.gz"))
            bad = [f.name for f in files if validate_daily(f).disposition != "ACCEPT"]
            check(f"{s} files pass the daily validator", bool(files) and not bad, f"{len(files)} files {bad}")
            if files:
                last = max(files)
                rows = json.load(__import__("gzip").open(last))
                c_last = float(json.loads(rows[0])["intc"])  # newest NSE day before Shoonya starts
                sh = nh.shoonya_closes(shoonya / f"{s}-EQ")
                c_first = sh[min(sh)]
                jump = abs(c_first / c_last - 1)
                check(f"{s} joins Shoonya smoothly", jump < 0.15, f"last NSE {c_last:.2f} -> first Shoonya {c_first:.2f}")
        # ingest reads the extra root (no state written: only Settings + file listing)
        from stock_gplm.app.settings import load
        st = load(nh.CFG, overrides={"data.daily_extra_roots": [str(out)]})
        check("settings carries daily_extra_roots", st.daily_extra_roots == (out.resolve(),), str(st.daily_extra_roots))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
