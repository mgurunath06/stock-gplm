#!/usr/bin/env python3
"""GPLM cloud node: official NSE daily history for EVERY NSE index (broad, sector, thematic, strategy, VIX).

Owner 2026-11-20: all indices and sectors, fetched by the cloud (not the PC). Source: the official NSE MCP
(get_index_snapshot lists ~170 names; get_index_history serves 12 months per call, chained by next_end_date).

    ~/stock-gplm-venv/bin/python index_history.py            # backfill missing history + extend to latest
    ~/stock-gplm-venv/bin/python index_history.py --only "Nifty 50,Nifty Bank"

Each run (resumable, nothing fetched twice):
  1. names   get_index_snapshot -> ~/gplm-live/index/names.json
  2. fetch   per index: newest chunk first, then older chunks until NSE has no more; raw tool payloads are kept
             in ~/gplm-live/index/raw/<SYM>/<END>.json (a re-parse never needs a refetch). After the first full
             backfill only the newest chunk is refetched each run.
  3. publish Shoonya-format daily triplets (payload, .meta.json, .json.gz.done) under
             ~/StockGPLMDownloads/data/daily/nse-index/NSE/<SYM>/ : one file per complete year, then
             non-overlapping chunks for the running year (published files are never rewritten).
<SYM> = index_symbol(name), e.g. "Nifty 50" -> IDX_NIFTY_50-EQ (the -EQ suffix is the store's symbol key).
Read-only public market data, personal research. Paced (--pause) to stay polite to NSE.
"""
import argparse
import asyncio
import calendar
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
sys.path.insert(0, str(HOME / "gplm-live"))
from nse_mcp_client import NSEMCPClient  # noqa: E402  (copy of src/stock_gplm/m39/client.py, stdlib only)

WORK = HOME / "gplm-live" / "index"
OUT = HOME / "StockGPLMDownloads" / "data" / "daily" / "nse-index" / "NSE"
FLOOR = date(1990, 1, 1)
CONTRACT = "stock-gplm.nse-index-history.v1"


def index_symbol(name: str) -> str:
    """Official NSE index name -> store symbol. Must match strategy_dsl.index_symbol (tested on the PC)."""
    s = re.sub(r"[^A-Z0-9]+", "_", name.strip().upper()).strip("_")
    if len(s) > 32:  # store symbols are <= 40 chars; keep long names unique
        s = s[:23] + "_" + hashlib.sha1(name.strip().upper().encode()).hexdigest()[:8]
    return f"IDX_{s}-EQ"


def log(m):
    print(f"{datetime.now():%H:%M:%S} {m}", flush=True)


def payload(result):
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def records(p):
    """The record list in a tool payload (the only list of dicts in it)."""
    if isinstance(p, list):
        return p
    lists = [v for v in p.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
    return lists[0] if len(lists) == 1 else []


def _key(rec, *words):
    for k in rec:
        kl = k.lower().replace("_", "").replace(" ", "")
        if kl in words:
            return rec[k]
    return None


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _date(v):
    s = str(v).strip()[:11]
    for f in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d %b %Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s if f != "%Y-%m-%d" else s[:10], f).date()
        except ValueError:
            pass
    return None


def parse(rec):
    """One NSE record -> (date, open, high, low, close, volume) or None."""
    d = _date(_key(rec, "date", "historicaldate", "timestamp", "tradedate", "indexdate"))
    c = _num(_key(rec, "close", "closeprice", "closingindexvalue", "closeindexvalue"))
    if d is None or c is None:
        return None
    o = _num(_key(rec, "open", "openprice", "openindexvalue")) or c
    h = _num(_key(rec, "high", "highprice", "highindexvalue")) or max(o, c)
    lo = _num(_key(rec, "low", "lowprice", "lowindexvalue")) or min(o, c)
    v = _num(_key(rec, "volume", "sharestraded", "tradedvolume", "totaltradedquantity")) or 0.0
    return d, o, max(h, o, c), min(lo, o, c), c, v


def shoonya_row(d, o, h, lo, c, v):
    return json.dumps({"time": d.strftime("%d-%b-%Y").upper(), "into": f"{o:.2f}", "inth": f"{h:.2f}",
                       "intl": f"{lo:.2f}", "intc": f"{c:.2f}", "ssboe": str(calendar.timegm(d.timetuple())),
                       "intv": f"{v:.2f}"}, separators=(", ", ":"))


def write_atomic(path, data: bytes):
    fd, tmp = tempfile.mkstemp(prefix=".idx-", dir=path.parent)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def publish(sym, name, rows, a, b):
    """Write one triplet for rows in [a, b] (newest first, like Shoonya). Never rewrites a published file."""
    part = [r for r in rows if a <= r[0] <= b]
    if not part:
        return False
    d = OUT / sym
    d.mkdir(parents=True, exist_ok=True)
    base = f"{a.isoformat()}_{b.isoformat()}"
    if (d / f"{base}.json.gz.done").exists():
        return False
    blob = gzip.compress(json.dumps([shoonya_row(*r) for r in sorted(part, reverse=True)]).encode(), mtime=0)
    sha = hashlib.sha256(blob).hexdigest()
    ts = datetime.now(timezone.utc).isoformat()
    write_atomic(d / f"{base}.json.gz", blob)
    write_atomic(d / f"{base}.meta.json", json.dumps({
        "source": "nse-mcp-index", "symbol": sym, "index_name": name, "from": a.isoformat(), "to": b.isoformat(),
        "quality_status": "UNVALIDATED_RAW", "sha256_compressed": sha, "records": len(part),
        "fetched_at_utc": ts, "contract": CONTRACT}, indent=2).encode())
    write_atomic(d / f"{base}.json.gz.done", json.dumps({
        "status": "COMPLETE", "file": f"{base}.json.gz", "bytes": len(blob), "records": len(part), "sha256": sha,
        "quality_status": "UNVALIDATED_RAW", "published_at_utc": ts}, indent=2).encode())
    return True


def published_to(sym):
    d = OUT / sym
    ends = [f[11:21] for f in os.listdir(d) if f.endswith(".json.gz.done")] if d.is_dir() else []
    return date.fromisoformat(max(ends)) if ends else None


def publish_all(sym, name, rows, today):
    """Complete years as whole files; the running year in non-overlapping chunks after what is published."""
    if not rows:
        return 0
    last = max(r[0] for r in rows)
    done_to = published_to(sym)
    n = 0
    for y in range(min(r[0] for r in rows).year, last.year + 1):
        a, b = date(y, 1, 1), date(y, 12, 31)
        if done_to and done_to >= a:  # this year already (partly) published: only the tail after it
            a = done_to + timedelta(days=1)
        if a > last:
            continue
        if b <= last and (y < today.year):
            n += publish(sym, name, rows, a, b)
        else:  # running year: chunk up to the last completed session
            n += publish(sym, name, rows, a, last)
    return n


async def run(a):
    WORK.mkdir(parents=True, exist_ok=True)
    state_f = WORK / "state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()  # IST (the cloud clock may be UTC)
    async with NSEMCPClient("bhavcopy", connect_timeout=10, read_timeout=90, retries=3) as cl:
        await cl.list_tools()
        snap = payload(await cl.call_tool("get_index_snapshot", {"date": "today", "filter": ""}))
        names = []
        for r in records(snap):
            n = _key(r, "index", "indexname", "name", "indexsymbol")
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
        names = list(dict.fromkeys(names))
        if not names:
            raise SystemExit(f"get_index_snapshot gave no index names; keys: {list(snap)[:10]}")
        write_atomic(WORK / "names.json", json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                                                      "names": {n: index_symbol(n) for n in names}}, indent=1).encode())
        if a.only:
            want = {x.strip().upper() for x in a.only.split(",")}
            names = [n for n in names if n.upper() in want]
        log(f"{len(names)} indices")
        for i, name in enumerate(names, 1):
            sym = index_symbol(name)
            raw = WORK / "raw" / sym
            raw.mkdir(parents=True, exist_ok=True)
            st = state.setdefault(sym, {"name": name})
            fetched, end, error = 0, "today", None
            while True:  # newest chunk always; older chunks until NSE stops (only until backfilled once)
                f = raw / f"{end}.json"
                if f.exists() and end != "today":
                    p = json.loads(f.read_text())
                else:
                    try:
                        p = payload(await cl.call_tool("get_index_history",
                                                       {"indexName": name, "months": 12, "endDate": end}))
                    except Exception as exc:  # noqa: BLE001
                        error = f"{type(exc).__name__}: {exc}"[:200]
                        break
                    fetched += 1
                    if end == "today":  # keep the newest chunk under its real end date
                        ds = [x[0] for x in map(parse, records(p)) if x]
                        if ds:
                            f = raw / f"{max(ds).isoformat()}.json"
                    if f.name != "today.json":
                        write_atomic(f, json.dumps(p).encode())
                    time.sleep(a.pause)
                nxt = p.get("next_end_date") if isinstance(p, dict) else None
                if st.get("backfilled") or not records(p) or not nxt or str(nxt)[:10] < FLOOR.isoformat() \
                        or str(nxt)[:10] == end:
                    break
                end = str(nxt)[:10]
            if error:
                st["error"] = error
                log(f"[{i}/{len(names)}] {name}: {error}")
            else:
                st.pop("error", None)
                st["backfilled"] = True
            ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            rows = {}
            for f in sorted(raw.glob("*.json")):
                for x in map(parse, records(json.loads(f.read_text()))):
                    # today's row only once the session is final
                    if x and (x[0] < ist.date() or ist.strftime("%H:%M") >= "16:30"):
                        rows[x[0]] = x
            rows = sorted(rows.values())
            n = publish_all(sym, name, rows, today)
            st.update(rows=len(rows), first=rows[0][0].isoformat() if rows else None,
                      last=rows[-1][0].isoformat() if rows else None, calls=st.get("calls", 0) + fetched,
                      updated=datetime.now().isoformat(timespec="seconds"))
            log(f"[{i}/{len(names)}] {name} -> {sym}: {len(rows)} rows {st['first']}..{st['last']}, "
                f"{fetched} calls, {n} files published")
            write_atomic(state_f, json.dumps(state, indent=1).encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated official index names")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between NSE calls")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
