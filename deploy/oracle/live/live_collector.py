#!/usr/bin/env python3
"""GPLM live test collector (M8 prototype): completed 1-minute NSE candles from Shoonya.

Read-only market data only: no order, position or account-changing call is made.
Uses ONLY the cached session token written by ~/shoonya_session.py (never prompts,
never reads the API secret). If the token is missing/expired it stops and says so.

Output (append-only, under ~/gplm-live/data/<YYYY-MM-DD>/):
  history.jsonl  warm-up bars from previous sessions (written once at start)
  bars.jsonl     completed bars of today, appended each minute (never rewritten)
  heartbeat.json atomic status snapshot (last cycle, lag, failures, token state)
"""
import argparse
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path.home()))
from shoonya_session import create_api, load_token  # noqa: E402  (cached token only)

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path.home() / "gplm-live"
MASTER = Path.home() / "StockGPLMDownloads" / "config" / "NSE_symbols.txt.zip"
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)
STOP_AFTER = dtime(15, 33)
CONTRACT = "stock-gplm.live-collector.v1"
INDEX_TS = {"NIFTY": "NIFTY INDEX", "BANKNIFTY": "NIFTY BANK", "INDIAVIX": "INDIA VIX"}  # watchlist name -> master


def now_ist():
    return datetime.now(IST)


def epoch(dt):
    return str(int(dt.timestamp()))


def write_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hb-", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log(msg):
    line = f"{now_ist():%H:%M:%S} {msg}"
    print(line, flush=True)


def load_tokens(symbols):
    with zipfile.ZipFile(MASTER) as z:
        rows = z.read(z.namelist()[0]).decode().splitlines()
    by_ts, by_sym = {}, {}
    for r in rows[1:]:
        p = r.split(",")
        if len(p) < 6 or p[0] != "NSE":
            continue
        by_ts[p[4]] = p[1]
        if p[5] == "EQ":
            by_sym.setdefault(p[3], p[1])
    out, missing = {}, []
    for s in symbols:
        s = s.strip().upper()
        if not s:
            continue
        if s in INDEX_TS:
            tok = by_ts.get(INDEX_TS[s])
        else:
            tok = by_ts.get(f"{s}-EQ") or by_sym.get(s)
        (out.__setitem__(s, tok) if tok else missing.append(s))
    return out, missing


def session_api():
    cached = load_token()
    if not cached:
        raise SystemExit("TOKEN_MISSING_OR_EXPIRED: run `python3 ~/shoonya_session.py` on Oracle first")
    api = create_api()
    api.injectOAuthHeader(cached["token"], cached["uid"], cached["account_id"])
    chk = api.get_limits()
    if not (isinstance(chk, dict) and chk.get("stat") == "Ok"):
        raise SystemExit("TOKEN_REJECTED: session not valid; log in again with ~/shoonya_session.py")
    return api


def parse(item):
    if isinstance(item, str):
        item = json.loads(item)
    t = datetime.strptime(item["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
    o, h, l, c = (float(item[k]) for k in ("into", "inth", "intl", "intc"))
    if not (l <= min(o, c) <= max(o, c) <= h):
        raise ValueError("OHLC inconsistency")
    return t, {"t": t.strftime("%Y-%m-%dT%H:%M"), "o": o, "h": h, "l": l, "c": c,
               "v": float(item.get("intv") or 0), "vwap": float(item.get("intvwap") or 0)}


def fetch(api, token, start, end):
    resp = api.get_time_price_series(exchange="NSE", token=token, starttime=epoch(start), endtime=epoch(end),
                                     interval=1)
    if not resp:
        return []
    if isinstance(resp, dict):
        if resp.get("stat") == "Not_Ok":
            raise RuntimeError(str(resp.get("emsg", "Not_Ok"))[:120])
        return []
    out = []
    for it in resp:
        t, bar = parse(it)
        if OPEN <= t.time() < CLOSE:
            out.append((t, bar))
    out.sort(key=lambda x: x[0])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.txt"))
    ap.add_argument("--warmup-days", type=int, default=10, help="calendar days of history to fetch at start")
    ap.add_argument("--pause", type=float, default=0.12, help="seconds between API calls")
    ap.add_argument("--lag", type=float, default=4.0, help="seconds after minute close before polling")
    ap.add_argument("--once", action="store_true", help="single cycle (test)")
    a = ap.parse_args()

    syms = [s for ln in Path(a.watchlist).read_text().splitlines() for s in ln.split("#", 1)[0].split()]
    tokens, missing = load_tokens(syms)
    today = now_ist().date()
    day_dir = ROOT / "data" / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    hb_path = day_dir / "heartbeat.json"
    hb = {"contract": CONTRACT, "date": today.isoformat(), "symbols": len(tokens), "missing": missing,
          "started": now_ist().isoformat(timespec="seconds"), "state": "STARTING", "pid": os.getpid()}
    write_atomic(hb_path, hb)
    try:
        api = session_api()
    except SystemExit as exc:
        hb.update(state="AUTH_FAILED", error=str(exc))
        write_atomic(hb_path, hb)
        raise
    log(f"session OK; {len(tokens)} symbols, missing={missing}")

    last = {}  # symbol -> last written minute (datetime)
    bars_path = day_dir / "bars.jsonl"
    if bars_path.exists():  # resume: never duplicate
        for line in bars_path.read_text().splitlines():
            r = json.loads(line)
            last[r["s"]] = max(last.get(r["s"], datetime.min.replace(tzinfo=IST)),
                               datetime.fromisoformat(r["t"]).replace(tzinfo=IST))

    hist_path = day_dir / "history.jsonl"
    extra_path = day_dir / "history-extra.jsonl"  # warm-up for symbols added after the first start of the day
    have_hist = set()
    for p in (hist_path, extra_path):
        if p.exists():
            have_hist |= {json.loads(x)["s"] for x in p.read_text().splitlines() if x.strip()}
    need = {s: t for s, t in tokens.items() if s not in have_hist} if hist_path.exists() else tokens
    if need:
        target = extra_path if hist_path.exists() else hist_path
        days = [today - timedelta(days=k) for k in range(a.warmup_days, 0, -1)]
        days = [d for d in days if d.weekday() < 5]
        tmp = target.with_suffix(".partial")
        n = 0
        hb.update(state="WARMING_UP")
        write_atomic(hb_path, hb)
        if target == extra_path and extra_path.exists():
            tmp.write_bytes(extra_path.read_bytes())
        with tmp.open("a") as f:
            for s, tok in need.items():
                for d in days:
                    try:
                        for _t, bar in fetch(api, tok, datetime.combine(d, OPEN, IST),
                                             datetime.combine(d, CLOSE, IST)):
                            f.write(json.dumps({"s": s, **bar}) + "\n")
                            n += 1
                    except Exception as exc:  # keep going; recorded
                        log(f"warmup {s} {d} failed: {exc}")
                    time.sleep(a.pause)
        os.replace(tmp, target)
        log(f"warm-up written: {n} bars for {len(need)} symbol(s) -> {target.name}")

    hb.update(state="RUNNING", cycles=0, failures_total=0)
    while True:
        now = now_ist()
        if now.time() >= STOP_AFTER:
            hb.update(state="STOPPED_AFTER_CLOSE")
            write_atomic(hb_path, hb)
            log("market closed; stopping")
            return
        if now.time() < OPEN:
            hb.update(state="WAITING_FOR_OPEN", updated=now.isoformat(timespec="seconds"))
            write_atomic(hb_path, hb)
            time.sleep(min(30, (datetime.combine(today, OPEN, IST) - now).total_seconds() + a.lag))
            continue
        cur_min = now.replace(second=0, microsecond=0)  # bars strictly before this are complete
        t0 = time.time()
        new, fails = 0, []
        with bars_path.open("a") as f:
            for s, tok in tokens.items():
                since = last.get(s)
                start = max(datetime.combine(today, OPEN, IST),
                            (since + timedelta(minutes=1)) if since else datetime.combine(today, OPEN, IST))
                if start >= cur_min:
                    continue
                try:
                    for t, bar in fetch(api, tok, start - timedelta(minutes=1), cur_min):
                        if t >= cur_min or (since and t <= since):
                            continue
                        f.write(json.dumps({"s": s, **bar, "rx": now_ist().strftime("%H:%M:%S")}) + "\n")
                        last[s] = since = t
                        new += 1
                except Exception as exc:
                    fails.append(f"{s}: {str(exc)[:80]}")
                time.sleep(a.pause)
            f.flush()
            os.fsync(f.fileno())
        dur = time.time() - t0
        hb.update(state="RUNNING", updated=now_ist().isoformat(timespec="seconds"),
                  last_complete_minute=(cur_min - timedelta(minutes=1)).strftime("%H:%M"),
                  cycle_seconds=round(dur, 1), new_bars=new, failures=fails[:10],
                  cycles=hb["cycles"] + 1, failures_total=hb["failures_total"] + len(fails),
                  symbols_current=sum(1 for v in last.values() if v >= cur_min - timedelta(minutes=2)))
        write_atomic(hb_path, hb)
        log(f"cycle {hb['cycles']}: +{new} bars in {dur:.1f}s, {len(fails)} failures")
        if fails and any("session" in x.lower() for x in fails):
            hb.update(state="AUTH_EXPIRED")
            write_atomic(hb_path, hb)
            raise SystemExit("session expired mid-run; log in again and restart")
        if a.once:
            return
        nxt = cur_min + timedelta(minutes=1, seconds=a.lag)
        time.sleep(max(1.0, (nxt - now_ist()).total_seconds()))


if __name__ == "__main__":
    main()
