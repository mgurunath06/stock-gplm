"""Long daily history from the official NSE MCP (bhavcopy endpoint). Owner runs it; resumable.

    python -m stock_gplm.app.nse_history all --stocks RELIANCE,TCS [--start-year 2010] [--research]
    python -m stock_gplm.app.nse_history all --stocks @deploy/oracle/live/universe.txt

Steps (each skips work already done):
  fetch    get_stock_history per stock in 3-month chunks (NSE's maximum), newest first, following
           next_end_date -> data/raw/nse-mcp/<SYM>/<END>.json (raw tool payload); <SYM>/_complete marks where
           NSE's history stops (as of 2026-11-20 it starts 2021-11-22).
  build    per stock: adjust splits/bonuses (prev-close field when NSE gives one; otherwise history older
           than the last >40% overnight jump is dropped), scale onto Shoonya's adjusted prices using the
           overlap, write Shoonya-format yearly files ending the day before Shoonya's first date into
           data/derived/nse-mcp-adjusted/NSE/<SYM>-EQ/. Report: build-report.json there.
  config   writes config/app/gplm.windows.longdaily.json (daily_extra_roots, home state-long).
  --research runs research-matrix 1d,1w on that config for the same stocks.
Shoonya files and the normal config are untouched. Personal, non-commercial research.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

APP = Path(__file__).resolve().parents[3]
CFG = APP / "config" / "app" / "gplm.windows.json"
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
ALIASES = {
    "symbol": ("symbol", "SYMBOL", "ticker", "CH_SYMBOL"),
    "date": ("trading_date", "date", "DATE", "timestamp", "TIMESTAMP", "CH_TIMESTAMP", "mTIMESTAMP"),
    "open": ("open", "OPEN", "open_price", "OPEN_PRICE", "CH_OPENING_PRICE"),
    "high": ("high", "HIGH", "high_price", "HIGH_PRICE", "CH_TRADE_HIGH_PRICE"),
    "low": ("low", "LOW", "low_price", "LOW_PRICE", "CH_TRADE_LOW_PRICE"),
    "close": ("close", "CLOSE", "close_price", "CLOSE_PRICE", "CH_CLOSING_PRICE"),
    "prev": ("prev_close", "prevClose", "PREVCLOSE", "PREV_CLOSE", "previous_close", "CH_PREVIOUS_CLS_PRICE"),
    "volume": ("volume", "VOLUME", "total_traded_quantity", "TOTTRDQTY", "CH_TOT_TRADED_QTY"),
    "series": ("series", "SERIES", "CH_SERIES"),
}
GAP_LIMIT = 0.40  # an unexplained overnight move this large is treated as an unadjusted split/bonus


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8-sig"))


def paths() -> tuple[Path, Path, Path]:
    d = cfg()["data"]
    raw_tree = Path(d["real_raw_tree"])
    return Path(d["daily_root"]), raw_tree / "nse-mcp", raw_tree.parent / "derived" / "nse-mcp-adjusted" / "NSE"


# ------------------------------------------------------------------ parsing (pure)
def _get(row: dict, name: str) -> Any:
    for k in ALIASES[name]:
        if k in row and row[k] not in (None, "", "-"):
            return row[k]
    return None


def _date(v: Any) -> str:
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.fromisoformat(s[:10]).isoformat()


def records(payload: Any) -> list[dict]:
    """The one list of row objects in a tool payload (top level or one level down)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):
                inner = records(v)
                if inner:
                    return inner
    return []


def parse_rows(payload: Any, symbol: str) -> list[tuple]:
    """-> sorted [(date, o, h, l, c, prev|None, vol)]; EQ series only when a series field exists."""
    out = {}
    for r in records(payload):
        sym = _get(r, "symbol")
        ser = _get(r, "series")
        if (sym and str(sym).upper() != symbol) or (ser and str(ser).upper() != "EQ"):
            continue
        try:
            o, h, l, c = (float(str(_get(r, k)).replace(",", "")) for k in ("open", "high", "low", "close"))
            v = float(str(_get(r, "volume") or 0).replace(",", ""))
            p = _get(r, "prev")
            out[_date(_get(r, "date"))] = (o, h, l, c, float(str(p).replace(",", "")) if p is not None else None, v)
        except (TypeError, ValueError):
            continue
    return [(d, *out[d]) for d in sorted(out)]


def adjust_and_link(rows: list[tuple], shoonya: dict[str, float]) -> tuple[list[tuple], str]:
    """Back-adjust rows, scale onto Shoonya, keep only dates before Shoonya starts.

    rows: sorted [(date, o, h, l, c, prev|None, vol)]. Returns ([(date, o, h, l, c, vol)], note)."""
    if not rows or not shoonya:
        return [], "no NSE rows" if not rows else "no Shoonya daily"
    first = min(shoonya)
    mult, adj, cut = 1.0, [], None
    for i in range(len(rows) - 1, -1, -1):
        d, o, h, l, c, prev, v = rows[i]
        adj.append((d, o * mult, h * mult, l * mult, c * mult, v / mult))
        if i == 0:
            break
        pc = rows[i - 1][4]
        if prev and pc > 0:
            f = prev / pc
            if abs(f - 1) > 0.005:
                mult *= f
        elif pc > 0 and abs(o / pc - 1) > GAP_LIMIT:  # no prev-close from NSE: stop at an unexplained jump
            cut = d
            break
    adj.reverse()
    # join on the first 20 overlap days only (later NSE holes/actions don't matter for the older history)
    ratios = [shoonya[d] / c for d, o, h, l, c, v in adj if d in shoonya and c > 0][:20]
    if len(ratios) < 5:
        return [], f"overlap with Shoonya too short ({len(ratios)} days)"
    r = sorted(ratios)[len(ratios) // 2]
    if max(abs(x / r - 1) for x in ratios) > 0.01:
        return [], "NSE and Shoonya disagree >1% on the overlap"
    keep = [(d, o * r, h * r, l * r, c * r, v / r) for d, o, h, l, c, v in adj if d < first]
    return keep, f"ok{' (history starts after a large unexplained jump on ' + cut + ')' if cut else ''}"


# ------------------------------------------------------------------ fetch
async def _fetch(stocks: list[str], years: Iterable[int], raw: Path, pause: float) -> None:
    from ..m39.client import NSEMCPClient
    from ..m39.comparison import tool_payload
    async with NSEMCPClient("bhavcopy", connect_timeout=10, read_timeout=60, retries=3) as cl:
        await cl.connect()
        await cl.list_tools()
        ys = list(years)
        first, last = date(ys[0], 1, 1).isoformat(), min(date(ys[-1], 12, 31), date.today()).isoformat()
        for n, s in enumerate(stocks, 1):
            done = raw / s / "_complete"
            if done.exists():
                continue
            end, empty, failed = last, 0, False
            while end >= first:  # NSE serves max 3 months per call; its history has holes, so step past them
                f = raw / s / f"{end}.json"
                if f.exists():
                    payload = json.loads(f.read_text(encoding="utf-8"))
                else:
                    try:
                        payload = tool_payload(await cl.call_tool(
                            "get_stock_history", {"symbol": s, "months": 3, "endDate": end}))
                    except Exception as e:  # retried on the next run
                        log(f"fetch {s} {end}: {type(e).__name__}: {str(e)[:120]}")
                        await asyncio.sleep(pause * 5)
                        failed = True
                        break
                    await asyncio.sleep(pause)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    tmp = f.with_suffix(".part")
                    tmp.write_text(json.dumps(payload), encoding="utf-8")  # empty replies kept: no re-asking
                    os.replace(tmp, f)
                empty = 0 if records(payload) else empty + 1
                if empty >= 8:  # two years with nothing: NSE's history has ended
                    break
                nxt = payload.get("next_end_date") if isinstance(payload, dict) else None
                end = nxt if nxt and nxt < end else (date.fromisoformat(end) - timedelta(days=91)).isoformat()
            if failed:
                continue
            else:
                done.parent.mkdir(parents=True, exist_ok=True)
                done.write_text(json.dumps({"stopped_at": end, "reason": "reached --start-year"}))
            log(f"fetch: {n}/{len(stocks)} {s}")


# ------------------------------------------------------------------ build
def shoonya_closes(sdir: Path) -> dict[str, float]:
    out = {}
    for f in sdir.glob("*_*.json.gz"):
        for row in json.load(gzip.open(f)):
            r = json.loads(row)
            out[datetime.strptime(r["time"], "%d-%b-%Y").date().isoformat()] = float(r["intc"])
    return out


def write_part(folder: Path, start: str, end: str, rows: list[tuple], source: str = "nse-mcp-adjusted") -> None:
    name = f"{start}_{end}.json.gz"
    recs = []
    for d, o, h, l, c, v in sorted(rows, reverse=True):  # Shoonya order: newest first
        dd = date.fromisoformat(d)
        ss = int(datetime(dd.year, dd.month, dd.day, tzinfo=timezone.utc).timestamp())
        lo, hi = min(o, h, l, c), max(o, h, l, c)
        recs.append(json.dumps({"time": f"{dd:%d}-{MON[dd.month - 1]}-{dd.year}", "into": f"{o:.4f}",
                                "inth": f"{hi:.4f}", "intl": f"{lo:.4f}", "intc": f"{c:.4f}", "ssboe": str(ss),
                                "intv": f"{round(v)}.00"}, separators=(", ", ":")))
    blob = gzip.compress(json.dumps(recs).encode(), mtime=0)
    digest = hashlib.sha256(blob).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (folder / name).write_bytes(blob)
    meta = {"source": source, "symbol": folder.name, "from": start, "to": end,
            "quality_status": "DERIVED_ADJUSTED", "sha256_compressed": digest, "records": len(recs), "built_at_utc": now}
    (folder / name.replace(".json.gz", ".meta.json")).write_text(json.dumps(meta, indent=2))
    (folder / f"{name}.done").write_text(json.dumps({"status": "COMPLETE", "file": name, "bytes": len(blob),
                                                     "records": len(recs), "sha256": digest,
                                                     "quality_status": "DERIVED_ADJUSTED", "published_at_utc": now}))


def build(stocks: list[str], raw: Path, out: Path, shoonya_root: Path) -> None:
    report: dict[str, str] = {}
    for s in stocks:
        rows: dict[str, tuple] = {}
        for f in sorted((raw / s).glob("*.json")):
            for r in parse_rows(json.loads(f.read_text(encoding="utf-8")), s):
                rows[r[0]] = r
        keep, note = adjust_and_link([rows[d] for d in sorted(rows)], shoonya_closes(shoonya_root / f"{s}-EQ"))
        report[s] = f"{note}; {len(keep)} older days" if keep else note
        if not keep:
            continue
        folder = out / f"{s}-EQ"
        for old in folder.glob("*"):  # rebuilt from raw each time (derived output only)
            old.unlink()
        last = keep[-1][0]
        for y in sorted({d[:4] for d, *_ in keep}):
            yr = [x for x in keep if x[0][:4] == y]
            write_part(folder, f"{y}-01-01", min(f"{y}-12-31", last), yr)
    out.mkdir(parents=True, exist_ok=True)
    (out / "build-report.json").write_text(json.dumps(report, indent=1))
    ok = sum(1 for v in report.values() if v.startswith("ok"))
    log(f"build: {ok}/{len(report)} stocks extended; details {out / 'build-report.json'}")
    from . import yahoo_history as yh  # Yahoo fills only dates older than NSE: rebuild it so they never overlap
    _, yraw, yout, _ = yh.paths()
    ys = [s for s in stocks if (yraw / s).is_dir()]
    if ys:
        yh.build(ys, yraw, yout, shoonya_root, out)


def write_config(out: Path) -> Path:
    """Long-daily config: NSE MCP and (if built) Yahoo derived roots; each covers different dates."""
    c = cfg()
    yahoo = out.parent.parent / "yahoo-adjusted" / "NSE"
    # owner 2026-11-21: nse-mcp-adjusted is not read (mis-scaled, holes); Yahoo (checked vs raw NSE) fills before Shoonya
    c["data"]["daily_extra_roots"] = [str(yahoo)] if yahoo.is_dir() else []
    c["home"] = str(Path(c["home"]).parent / "state-long")
    p = CFG.with_name("gplm.windows.longdaily.json")
    p.write_text(json.dumps(c, indent=2), encoding="utf-8")
    log(f"config: {p}")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["fetch", "build", "config", "all"])
    ap.add_argument("--stocks", required=True, help="comma list, or @file with one symbol per line")
    ap.add_argument("--start-year", type=int, default=2010)
    ap.add_argument("--end-year", type=int, default=2023, help="must overlap Shoonya (starts 2022-10-31)")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between NSE calls")
    ap.add_argument("--research", action="store_true", help="run 1d,1w research-matrix afterwards")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()
    src = a.stocks[1:] if a.stocks.startswith("@") else None
    stocks = [x.strip().split(",")[0].upper().removesuffix("-EQ")
              for x in (Path(src).read_text(encoding="utf-8").splitlines() if src else a.stocks.split(","))
              if x.strip() and not x.startswith("#")]
    shoonya, raw, out = paths()
    if a.step in ("fetch", "all"):
        asyncio.run(_fetch(stocks, range(a.start_year, a.end_year + 1), raw, a.pause))
    if a.step in ("build", "all"):
        build(stocks, raw, out, shoonya)
    if a.step in ("config", "all"):
        conf = write_config(out)
        if a.research:
            cmd = [sys.executable, "-m", "stock_gplm.app", "research-matrix", "--config", str(conf),
                   "--stocks", ",".join(stocks), "--timeframes", "1d,1w", "--mode", "both",
                   "--methods", "evolve,tree,screen", "--budget", "deep", "--name", "discover-long",
                   "--workers", str(a.workers), "--quiet"]
            log("research: " + " ".join(cmd))
            return subprocess.call(cmd, env={**os.environ, "OPENBLAS_NUM_THREADS": "1"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
