#!/usr/bin/env python3
"""GPLM cloud node: keep the Oracle store complete and publish an inventory the PC syncs from.

Runs independently of the PC. Each pass:
  1. for every watchlist symbol and every NSE session (registered calendar ~/StockGPLMDownloads/calendar)
     in the last --minute-sessions sessions, fetch the missing 1-minute session (newest first) with the
     existing gplm_jobs.minute() -> same raw format, .meta.json and .done marker (publish_done);
     today's session is fetched only after its collect_after time (16:30);
  2. extend daily candles from the last covered date to yesterday (gplm_jobs.eod);
  3. write ~/gplm-live/inventory.json (what exists, what is still missing, why).
Read-only market data only. Uses the cached token only (never prompts). Throttled to share the
API with the live collector.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
sys.path.insert(0, str(HOME / "StockGPLMDownloads" / "scripts"))
sys.path.insert(0, str(HOME / "gplm-live"))
import gplm_jobs as J  # noqa: E402
from live_collector import session_api  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
LIVE = HOME / "gplm-live"
DATA = HOME / "StockGPLMDownloads" / "data"
MIN_ROOT = DATA / "minute" / "shoonya" / "NSE"
DAY_ROOT = DATA / "daily" / "shoonya" / "NSE"
DAILY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz\.done$")
MIN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json\.gz\.done$")
CONTRACT = "stock-gplm.cloud-inventory.v1"


def now():
    return datetime.now(IST)


def log(m):
    print(f"{now():%H:%M:%S} {m}", flush=True)


def write_atomic(path, obj):
    fd, tmp = tempfile.mkstemp(prefix=".inv-", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def watchlist(path):
    out = []
    for ln in Path(path).read_text().splitlines():
        for s in ln.split("#", 1)[0].split():
            if s.upper() not in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
                out.append(s.upper() + "-EQ")
    return out


def sessions_back(n, include_today):
    out, d = [], now().date()
    t = now()
    while len(out) < n and d.year == 2026:
        sess, kind = J.session(d)
        if sess:
            ready = d < t.date() or (include_today and t >= J.dt(d, sess["collect_after"]))
            if ready:
                out.append(d)
        d -= timedelta(days=1)
    return out  # newest first


def daily_last(sym):
    p = DAY_ROOT / sym
    ends = [m.group(2) for f in (os.listdir(p) if p.is_dir() else []) if (m := DAILY_RE.match(f))]
    return max(ends) if ends else None


def minute_have(sym):
    p = MIN_ROOT / sym
    return {m.group(1) for f in (os.listdir(p) if p.is_dir() else []) if (m := MIN_RE.match(f))}


BACKOFF = [60, 300, 1800, 7200]  # seconds between retries of one (symbol, session)
MAX_ATTEMPTS = 5
STATE = LIVE / "fetch_state.json"


class Retry:
    """Per (symbol, item) retry memory with backoff; per-symbol health. Global outages never count."""

    def __init__(self):
        try:
            self.s = json.loads(STATE.read_text())
        except (OSError, ValueError):
            self.s = {}

    def due(self, key):
        r = self.s.get(key)
        return not r or (r["status"] == "RETRYING" and time.time() >= r["next_try"])

    def fail(self, key, err):
        r = self.s.setdefault(key, {"attempts": 0})
        r["attempts"] += 1
        r["last_error"] = err[:160]
        r["last_try"] = now().isoformat(timespec="seconds")
        if r["attempts"] >= MAX_ATTEMPTS:
            r["status"] = "GAVE_UP"
        else:
            r["status"] = "RETRYING"
            r["next_try"] = time.time() + BACKOFF[min(r["attempts"] - 1, len(BACKOFF) - 1)]

    def ok(self, key):
        self.s.pop(key, None)

    def reset(self, prefix):
        for k in [k for k in self.s if k.startswith(prefix)]:
            del self.s[k]

    def save(self):
        write_atomic(STATE, self.s)

    def health(self, sym):
        rs = {k: v for k, v in self.s.items() if k.startswith(sym + "|")}
        gave = [k for k, v in rs.items() if v["status"] == "GAVE_UP"]
        if gave:
            errs = sorted({v["last_error"] for k, v in rs.items() if v["status"] == "GAVE_UP"})
            return {"status": "PROBLEM", "detail": f"{len(gave)} item(s) failed {MAX_ATTEMPTS}x: {'; '.join(errs)[:200]}"}
        if rs:
            return {"status": "RETRYING", "detail": f"{len(rs)} item(s) in backoff"}
        return {"status": "OK"}


def inventory(syms, want, notes, retry=None):
    inv = {"contract": CONTRACT, "generated": now().isoformat(timespec="seconds"), "symbols": {},
           "window": [str(want[-1]), str(want[0])] if want else None}
    for s in syms:
        have = minute_have(s)
        miss = sorted(str(d) for d in want if str(d) not in have)
        inv["symbols"][s] = {"daily_last": daily_last(s), "minute_sessions": sorted(have),
                             "minute_missing": miss, "note": notes.get(s),
                             **({"health": retry.health(s)} if retry else {})}
    inv["missing_total"] = sum(len(v["minute_missing"]) for v in inv["symbols"].values())
    return inv


def take_requests():
    """PC asks for specific items (or a retry reset) via ~/gplm-live/requests.jsonl."""
    p = LIVE / "requests.jsonl"
    if not p.exists():
        return []
    tmp = p.with_suffix(".processing")
    os.replace(p, tmp)
    out = [json.loads(x) for x in tmp.read_text().splitlines() if x.strip()]
    tmp.unlink()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", default=str(LIVE / "watchlist.txt"))
    ap.add_argument("--minute-sessions", type=int, default=60, help="keep this many recent sessions complete")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between API calls (shared with live)")
    ap.add_argument("--every", type=int, default=600, help="seconds between passes")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    syms = watchlist(a.watchlist)
    retry = Retry()
    while True:
        notes = {}
        for rq in take_requests():  # PC: "reset SYM" clears its backoff so it is tried again now
            if rq.get("op") == "reset" and rq.get("symbol"):
                retry.reset(rq["symbol"] + "|")
        retry.save()
        try:
            api = session_api()
            tok = J.symbol_map()
        except SystemExit as exc:
            write_atomic(LIVE / "inventory.json", {"contract": CONTRACT, "generated": now().isoformat(timespec="seconds"),
                                                  "state": "AUTH_FAILED", "error": str(exc)})
            log(f"auth: {exc}")
            if a.once:
                return
            time.sleep(120)
            continue
        want = sessions_back(a.minute_sessions, include_today=True)
        # daily first (one call per symbol), then minute gaps newest-first so recent days close quickly
        yday = now().date() - timedelta(days=1)
        ok_n = fail_n = 0
        failed_now = []

        def attempt(key, fn):
            nonlocal ok_n, fail_n
            if not retry.due(key):
                return
            try:
                fn()
                retry.ok(key)
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                failed_now.append((key, f"{type(exc).__name__}: {str(exc)[:120]}"))
                fail_n += 1
            time.sleep(a.pause)

        for s in syms:
            last = daily_last(s)
            if last and date.fromisoformat(last) < yday:
                start = date.fromisoformat(last) + timedelta(days=1)
                if any(J.session(start + timedelta(days=k))[0] for k in range((yday - start).days + 1)):
                    attempt(f"{s}|daily|{start}", lambda s=s, start=start: J.eod(api, s, start, yday))
        write_atomic(LIVE / "inventory.json", inventory(syms, want, notes, retry) | {"state": "FILLING"})
        for d in want:
            for s in syms:
                if str(d) in minute_have(s):
                    continue
                if s not in tok:
                    retry.s[f"{s}|master"] = {"status": "GAVE_UP", "attempts": MAX_ATTEMPTS,
                                              "last_error": "symbol not in current Shoonya master (renamed/delisted?)"}
                    continue
                attempt(f"{s}|minute|{d}", lambda s=s, d=d: J.minute(api, d, s, tok[s], allow_today=True))
            write_atomic(LIVE / "inventory.json", inventory(syms, want, notes, retry) | {"state": "FILLING"})
        # A pass where everything failed is an outage (network/auth/provider), not a per-stock problem:
        # those failures do not consume the stocks' retry budget.
        outage = fail_n > 0 and ok_n == 0 and fail_n >= 5
        if not outage:
            for key, err in failed_now:
                retry.fail(key, err)
        retry.save()
        inv = inventory(syms, want, notes, retry)
        gaps = inv["missing_total"]
        inv["state"] = "OUTAGE" if outage else "COMPLETE" if not gaps else "GAPS_REMAIN"
        inv["last_pass"] = {"ok": ok_n, "failed": fail_n, "outage": outage}
        inv["problems"] = {s: v["health"] for s, v in inv["symbols"].items() if v["health"]["status"] == "PROBLEM"}
        write_atomic(LIVE / "inventory.json", inv)
        log(f"pass done: ok={ok_n} failed={fail_n} outage={outage} missing={gaps} problems={list(inv['problems'])}")
        if a.once:
            return
        time.sleep(60 if gaps and not outage else a.every)


if __name__ == "__main__":
    main()
