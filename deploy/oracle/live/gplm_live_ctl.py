#!/usr/bin/env python3
"""Oracle-side control for the live test collector (called over SSH by the PC live lab).

  login   read the Shoonya OAuth code (or the whole redirect URL) from STDIN, exchange it for a session
          token with the API secret that stays on Oracle, cache it via shoonya_session.save_token.
          The code is never put on a command line or in a log.
  start   start live_collector.py in the background if it is not already running (needs a fresh token).
  stop    stop the running collector.
  status  token freshness + collector pid.
Prints one JSON line. No order, position or account-changing call is made.
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

HOME = Path.home()
sys.path.insert(0, str(HOME))
import shoonya_session as S  # noqa: E402

ROOT = HOME / "gplm-live"
PY = HOME / "stock-gplm-venv" / "bin" / "python"
PIDFILE = ROOT / "collector.pid"


def out(**kw):
    print(json.dumps(kw), flush=True)


PROCS = {"collector": "live_collector.py", "sync": "cloud_sync.py"}


def running_pid(name="collector"):
    try:
        pid = int((ROOT / f"{name}.pid").read_text())
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        return pid if PROCS[name].encode() in cmd else None
    except (OSError, ValueError):
        return None


def spawn(name):
    pid = running_pid(name)
    if pid:
        return {"state": "ALREADY_RUNNING", "pid": pid}
    logf = open(ROOT / f"{name}-{datetime.now(ZoneInfo('Asia/Kolkata')):%Y%m%d}.log", "a")
    p = subprocess.Popen([str(PY), str(ROOT / PROCS[name])], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    (ROOT / f"{name}.pid").write_text(str(p.pid))
    return {"state": "STARTED", "pid": p.pid}


def token_status():
    try:
        t = S.load_token()
    except Exception as exc:  # unsafe perms etc.
        return {"token": "ERROR", "detail": type(exc).__name__}
    if not t:
        return {"token": "MISSING_OR_EXPIRED"}
    age_h = (datetime.now().timestamp() - t["created_at"]) / 3600
    return {"token": "CACHED", "age_hours": round(age_h, 1)}


def login():
    raw = sys.stdin.readline().strip()
    code = raw
    if "code=" in raw:
        q = parse_qs(urlparse(raw if "://" in raw else "http://x/?" + raw.split("?", 1)[-1]).query)
        code = (q.get("code") or [""])[0]
    if not code or len(code) > 512 or any(c.isspace() for c in code):
        return out(ok=False, error="no usable code found in input")
    secret_file = S.CONFIG_DIR / "shoonya_api_secret"
    if not secret_file.is_file() or secret_file.stat().st_mode & 0o077:
        return out(ok=False, error="API secret file missing or unsafe permissions on Oracle")
    secret = secret_file.read_text().strip()
    api = S.create_api()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = api.getAccessToken(code, secret, S.CLIENT_ID, S.USER_ID)
    except Exception as exc:
        return out(ok=False, error=f"token exchange failed: {type(exc).__name__}")
    if not (isinstance(result, (tuple, list)) and len(result) >= 4 and result[0]):
        return out(ok=False, error="Shoonya rejected the code (expired or already used?) - log in again")
    token, uid, _, account_id = result[:4]
    S.save_token(token, uid, account_id)
    api.injectOAuthHeader(token, uid, account_id)
    with contextlib.redirect_stdout(io.StringIO()):
        chk = api.get_limits()
    ok = isinstance(chk, dict) and chk.get("stat") == "Ok"
    out(ok=ok, token="CACHED", validated=ok)


def start(which=("collector", "sync")):
    if not S.load_token():
        return out(ok=False, error="no fresh token: log in first")
    res = {n: spawn(n) for n in which}
    out(ok=True, collector=(res.get("collector") or {}).get("state"), processes=res)


def stop(which=("collector",)):
    res = {}
    for n in which:
        pid = running_pid(n)
        if pid:
            os.kill(pid, signal.SIGTERM)
        res[n] = "STOPPED" if pid else "NOT_RUNNING"
    out(ok=True, collector=res.get("collector"), processes=res)


def status():
    inv = ROOT / "inventory.json"
    try:
        i = json.loads(inv.read_text())
        inv_s = {"state": i.get("state"), "generated": i.get("generated"), "missing_total": i.get("missing_total")}
    except (OSError, ValueError):
        inv_s = None
    out(ok=True, **token_status(), collector_pid=running_pid("collector"), sync_pid=running_pid("sync"),
        inventory=inv_s)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "reset":  # PC asks the cloud to retry a stock now (clears its backoff)
        with open(ROOT / "requests.jsonl", "a") as f:
            f.write(json.dumps({"op": "reset", "symbol": sys.argv[2]}) + "\n")
        out(ok=True, requested="reset", symbol=sys.argv[2])
        sys.exit(0)
    which = tuple(sys.argv[2:]) or None
    if cmd in ("start", "stop") and which:
        {"start": start, "stop": stop}[cmd](which)
    else:
        {"login": login, "start": start, "stop": stop, "status": status}[cmd]()
