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
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
sys.path.insert(0, str(HOME / "StockGPLMDownloads" / "scripts"))
sys.path.insert(0, str(HOME / "gplm-live"))
import gplm_jobs as J  # noqa: E402
from live_collector import session_api  # noqa: E402
from adaptive_workers import AdaptiveWorkers  # noqa: E402
from transfer_ledger import archived, download_event  # noqa: E402

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
    ends.extend(x.split("_")[-1] for x in archived("daily", sym))
    return max(ends) if ends else None


def minute_have(sym):
    p = MIN_ROOT / sym
    return {m.group(1) for f in (os.listdir(p) if p.is_dir() else []) if (m := MIN_RE.match(f))} | archived("minute", sym)


BACKOFF = [60, 300, 1800, 7200]  # seconds between retries of one (symbol, session)
MAX_ATTEMPTS = 5
MAX_WORKERS = 4
# Owner 2026-11-20: index history for relative-strength rules (Shoonya master: Exchange NSE, Instrument INDEX).
# Daily candles use the trading symbol ("Nifty 50"); minute candles use the token. Fetched first in every sweep.
INDICES = {"Nifty 50": "26000"}
STATE = LIVE / "fetch_state.json"
# 2026-11-21: first trading day per symbol (listing or rename), written on the PC by universe_build.py.
# Sessions before it are neither fetched nor expected (new listings/renames used to count as never complete).
LISTING = LIVE / "listing.json"


def listing_dates():
    try:
        return {k.upper(): v for k, v in json.loads(LISTING.read_text()).items()}
    except (OSError, ValueError):
        return {}


TOKEN_FILE = HOME / ".config" / "stock-gplm" / "shoonya_token.json"


class Retry:
    """Per (symbol, item) retry memory with backoff; per-symbol health. Global outages never count."""

    def __init__(self):
        try:
            self.s = json.loads(STATE.read_text())
        except (OSError, ValueError):
            self.s = {}
        # Older runs called terminal failures GAVE_UP.  Preserve every receipt
        # and error, but make the terminal state explicit and non-retryable.
        for key, value in self.s.items():
            if not key.startswith("_") and isinstance(value, dict) and value.get("status") == "GAVE_UP":
                value["status"] = "UNAVAILABLE"

    def due(self, key):
        r = self.s.get(key)
        return not r or (r["status"] == "RETRYING" and time.time() >= r["next_try"])

    def fail(self, key, err):
        r = self.s.setdefault(key, {"attempts": 0})
        r["attempts"] += 1
        r["last_error"] = err[:160]
        r["last_try"] = now().isoformat(timespec="seconds")
        if r["attempts"] >= MAX_ATTEMPTS:
            r["status"] = "UNAVAILABLE"
        else:
            r["status"] = "RETRYING"
            r["next_try"] = time.time() + BACKOFF[min(r["attempts"] - 1, len(BACKOFF) - 1)]

    def mark_unavailable(self, key, err, *, attempts=MAX_ATTEMPTS):
        """Record a provider item as terminal without manufacturing a file."""
        r = self.s.setdefault(key, {})
        r.update(status="UNAVAILABLE", attempts=max(int(r.get("attempts", 0)), attempts),
                 last_error=err[:160], last_try=now().isoformat(timespec="seconds"))
        r.pop("next_try", None)

    def ok(self, key):
        self.s.pop(key, None)

    def reset(self, prefix):
        for k in [k for k in self.s if k.startswith(prefix)]:
            del self.s[k]

    def save(self):
        write_atomic(STATE, self.s)

    def health(self, sym):
        rs = {k: v for k, v in self.s.items() if k.startswith(sym + "|")}
        pending = [k for k, v in rs.items() if v.get("status") == "RETRYING"]
        unavailable = [k for k, v in rs.items() if v.get("status") == "UNAVAILABLE"]
        if pending:
            return {"status": "RETRYING", "detail": f"{len(pending)} item(s) in backoff; "
                    f"{len(unavailable)} confirmed unavailable"}
        if unavailable:
            return {"status": "PARTIAL", "detail": f"{len(unavailable)} item(s) confirmed unavailable "
                    f"after {MAX_ATTEMPTS} attempts"}
        return {"status": "OK"}

    def unavailable_count(self, sym, days, have=()):
        present = {str(x) for x in have}
        return sum(
            1 for d in days if str(d) not in present
            and self.s.get(f"{sym}|minute|{d}", {}).get("status") == "UNAVAILABLE"
        )


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
    ap.add_argument("--pause", type=float, default=1.0, help="min seconds between API calls in market hours")
    ap.add_argument("--idle-pause", type=float, default=0.25, help="min seconds between API calls outside market hours")
    ap.add_argument("--every", type=int, default=600, help="seconds between passes")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--order", choices=["date", "symbol"], default="date",
                    help="minute gaps: date = newest day across all stocks; symbol = complete one stock, then the next")
    ap.add_argument("--universe", default=None,
                    help="file of extra symbols (bare, comma/space separated) filled AFTER the watchlist, in file order")
    ap.add_argument("--floor", default=None, help="seed: earliest session the provider still serves (YYYY-MM-DD)")
    a = ap.parse_args()
    syms = list(INDICES) + watchlist(a.watchlist)
    extra = []
    if a.universe and Path(a.universe).exists():
        raw = Path(a.universe).read_text().replace(",", " ").split()
        extra = [x.upper() + "-EQ" for x in raw if x.upper() + "-EQ" not in syms]
        extra = list(dict.fromkeys(extra))
    retry = Retry()
    # The configured provider floor is authoritative. Do not infer a global
    # history boundary from missing rows of recently listed/suspended stocks.
    provider_floor = a.floor or retry.s.get("_floor")
    if not provider_floor:
        raise SystemExit("--floor is required until a registered provider-window source exists")
    def save_retry():
        retry.s["_floor"] = provider_floor
        retry.save()
    save_retry()
    while True:
        sweep_no = int(retry.s.get("_sweep", 0)) + 1
        failed_symbols = set()
        notes = {}
        for rq in take_requests():  # PC: "reset SYM" clears its backoff so it is tried again now
            if rq.get("op") == "reset" and rq.get("symbol"):
                retry.reset(rq["symbol"] + "|")
        save_retry()
        try:
            api = session_api()
            tok = J.symbol_map()
            tok.update(INDICES)
        except SystemExit as exc:
            write_atomic(LIVE / "inventory.json", {"contract": CONTRACT, "generated": now().isoformat(timespec="seconds"),
                                                  "state": "AUTH_FAILED", "error": str(exc)})
            log(f"auth: {exc}")
            if a.once:
                return
            time.sleep(120)
            continue
        want = [d for d in sessions_back(a.minute_sessions, include_today=True)
                if str(d) >= provider_floor]
        listed = listing_dates()

        def want_for(s):
            first = listed.get(s.removesuffix("-EQ").upper())
            return [d for d in want if not first or str(d) >= first]
        # daily first (one call per symbol), then minute gaps newest-first so recent days close quickly
        yday = now().date() - timedelta(days=1)
        ok_n = fail_n = 0
        failed_now = []
        adaptive = AdaptiveWorkers(minimum=1, maximum=MAX_WORKERS, start=2)
        state_lock = threading.RLock()
        worker_local = threading.local()
        streak: list = []

        def worker_api():
            token_mtime = TOKEN_FILE.stat().st_mtime_ns
            if not hasattr(worker_local, "api") or worker_local.token_mtime != token_mtime:
                worker_local.api = session_api()
                worker_local.token_mtime = token_mtime
            return worker_local.api

        def base_pause():
            t = now()
            busy = t.weekday() < 5 and 9 * 60 <= t.hour * 60 + t.minute <= 15 * 60 + 45  # live collector shares API
            return a.pause if busy else a.idle_pause

        def attempt(key, fn):
            """Adaptive pace: speed up on success, double the pause on errors (throttling/network)."""
            nonlocal ok_n, fail_n
            with state_lock:
                if not retry.due(key):
                    return "skip"
            adaptive.wait(base_pause())
            err = None
            download_event(key, "CLOUD_DOWNLOAD_STARTED")
            try:
                fn()
                download_event(key, "CLOUD_DOWNLOAD_COMPLETED")
                with state_lock:
                    retry.ok(key)
                    ok_n += 1
                    streak.clear()
                adaptive.success()
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {str(exc)[:120]}"
                download_event(key, "CLOUD_DOWNLOAD_FAILED", {"error": err})
                if "MINUTE_UNAVAILABLE" not in err:
                    adaptive.failure()
                    with state_lock:
                        fail_n += 1
                        if a.order == "symbol":
                            retry.fail(key, err)
                            streak.append(key)
                        else:
                            failed_now.append((key, err))
            return err

        for s in syms:
            last = daily_last(s)
            if last and date.fromisoformat(last) < yday:
                start = date.fromisoformat(last) + timedelta(days=1)
                if any(J.session(start + timedelta(days=k))[0] for k in range((yday - start).days + 1)):
                    attempt(f"{s}|daily|{start}", lambda s=s, start=start: J.eod(api, s, start, yday))
        write_atomic(LIVE / "inventory.json", inventory(syms, want, notes, retry) | {"state": "FILLING"})
        def fill(s, days):
            have = minute_have(s)
            oldest = sorted(want)[:20]
            first = listed.get(s.removesuffix("-EQ").upper())
            for d in sorted(days):  # oldest first: those days are the next to leave the provider window
                if str(d) in have or str(d) < provider_floor or (first and str(d) < first):
                    continue
                if s not in tok:
                    key = f"{s}|master"
                    with state_lock:
                        r = retry.s.setdefault(key, {"attempts": 0})
                        err = "symbol not in current Shoonya master (renamed/delisted?)"
                        if r.get("status") != "UNAVAILABLE":
                            retry.fail(key, err)
                        if retry.s[key].get("status") == "UNAVAILABLE":
                            # No provider token means none of this symbol's session files can
                            # be requested. Resolve each missing item explicitly so the next
                            # sweep advances instead of stopping at this symbol forever.
                            for missing_day in days:
                                if str(missing_day) not in have:
                                    retry.mark_unavailable(f"{s}|minute|{missing_day}", err)
                        failed_symbols.add(s)
                        notes[s] = (f"unavailable after {MAX_ATTEMPTS} sweeps: symbol absent from current master"
                                    if retry.s[key].get("status") == "UNAVAILABLE"
                                    else f"deferred in sweep {sweep_no}: symbol absent from current master")
                    return False
                key = f"{s}|minute|{d}"
                err = attempt(key, lambda s=s, d=d: J.minute(worker_api(), d, s, tok[s], allow_today=True))
                if err == "skip":
                    with state_lock:
                        status = retry.s.get(key, {}).get("status")
                        if status == "RETRYING":
                            failed_symbols.add(s)
                            notes[s] = f"retry pending in sweep {sweep_no}: {d}"
                        elif status == "UNAVAILABLE":
                            notes[s] = f"continuing past unavailable session {d}"
                    continue
                if err and err != "skip" and "MINUTE_UNAVAILABLE" in err:
                    with state_lock:
                        retry.fail(key, err)
                if err:
                    with state_lock:
                        failed_symbols.add(s)
                        status = retry.s.get(key, {}).get("status")
                        notes[s] = (f"continuing past unavailable session {d}"
                                    if status == "UNAVAILABLE"
                                    else f"retry scheduled for {d}: {err[:100]}")
                    # A bad session must never block later sessions for the stock.
                    continue
            return True

        try:
            previous_rows = json.loads((LIVE / "universe_progress.json").read_text()).get("have", {})
        except (OSError, ValueError):
            previous_rows = {}
        # Resume the last snapshot and refresh each symbol when it is processed.
        # A full pre-scan delayed every restart without improving transfer safety.
        progress_rows = {x: int(previous_rows.get(x, 0)) for x in syms + extra}
        expected_rows = {x: len(want_for(x)) for x in syms + extra}
        unavailable_rows = {
            x: retry.unavailable_count(x, want_for(x), minute_have(x)) for x in syms + extra
        }
        def progress(current):
            # Recheck only the symbol just processed. Rewalking the full universe
            # after every symbol made progress O(symbols squared).
            progress_rows[current] = sum(1 for d in want_for(current) if str(d) in minute_have(current))
            expected_rows[current] = len(want_for(current))
            unavailable_rows[current] = retry.unavailable_count(
                current, want_for(current), minute_have(current))
            pending_rows = {k: max(0, expected_rows[k] - progress_rows[k] - unavailable_rows[k])
                            for k in progress_rows}
            write_atomic(LIVE / "universe_progress.json",
                         {"generated": now().isoformat(timespec="seconds"), "expected": len(want),
                          "window": [str(want[-1]), str(want[0])] if want else None, "current": current,
                          "sweep": sweep_no, "max_attempts": MAX_ATTEMPTS,
                          "failed_this_sweep": len(failed_symbols),
                          "adaptive_workers": adaptive.snapshot(),
                          "complete": sorted(k for k, v in progress_rows.items()
                                             if v >= expected_rows.get(k, len(want))),
                          "resolved": sorted(k for k, v in progress_rows.items()
                                             if v + unavailable_rows[k] >= expected_rows.get(k, len(want))),
                          "totals": {"expected": sum(expected_rows.values()),
                                     "downloaded": sum(progress_rows.values()),
                                     "unavailable": sum(unavailable_rows.values()),
                                     "pending": sum(pending_rows.values())},
                          "have": progress_rows, "expected_per_symbol": expected_rows,
                          "unavailable_per_symbol": unavailable_rows,
                          "pending_per_symbol": pending_rows})

        if a.order == "symbol":  # one stock fully, then the next: research can start per finished stock
            queue = iter(syms + extra)
            active = {}
            exhausted = False
            with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="candle") as pool:
                while active or not exhausted:
                    while not exhausted and len(active) < adaptive.snapshot()["target"]:
                        try:
                            s = next(queue)
                        except StopIteration:
                            exhausted = True
                            break
                        active[pool.submit(fill, s, want)] = s
                    if not active:
                        continue
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        s = active.pop(future)
                        future.result()
                        with state_lock:
                            save_retry()
                            if s in syms:
                                write_atomic(LIVE / "inventory.json", inventory(syms, want, notes, retry) |
                                             {"state": "FILLING"})
                        progress(s)
        else:
            for d in want:
                for s in syms:
                    fill(s, [d])
                write_atomic(LIVE / "inventory.json", inventory(syms, want, notes, retry) | {"state": "FILLING"})
        # A pass where everything failed is an outage (network/auth/provider), not a per-stock problem:
        # those failures do not consume the stocks' retry budget.
        outage = fail_n > 0 and ok_n == 0 and fail_n >= 5
        if not outage:
            for key, err in failed_now:
                retry.fail(key, err)
        retry.s["_sweep"] = sweep_no
        save_retry()
        inv = inventory(syms, want, notes, retry)
        gaps = inv["missing_total"]
        inv["state"] = "OUTAGE" if outage else "COMPLETE" if not gaps else "GAPS_REMAIN"
        inv["last_pass"] = {"ok": ok_n, "failed": fail_n, "outage": outage}
        inv["problems"] = {s: v["health"] for s, v in inv["symbols"].items() if v["health"]["status"] == "PROBLEM"}
        write_atomic(LIVE / "inventory.json", inv)
        log(f"pass done: ok={ok_n} failed={fail_n} outage={outage} missing={gaps} problems={list(inv['problems'])}")
        if a.once:
            return
        time.sleep(60 if (gaps or failed_symbols) and not outage else a.every)


if __name__ == "__main__":
    main()
