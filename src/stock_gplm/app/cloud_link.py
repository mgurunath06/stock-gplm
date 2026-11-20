"""PC <-> cloud link (M8.1 prototype): one app on two nodes.

* ``Stream``: a persistent SSH connection running ``tail -F`` on the cloud's append-only live file.
  Lines are pushed to the PC as soon as the cloud writes them; on any drop it reconnects from the
  last committed byte offset (no duplicates, no gaps).
* ``CloudSync``: completed-session sync. Lists the cloud's published ``.done`` markers for the watchlist,
  compares with every local raw root, pulls only the missing triplets (payload, meta, marker) in one
  tar stream, verifies bytes + SHA-256 against the marker, and publishes no-overwrite (hard link to the
  final name, payload -> meta -> marker so the marker is the commit point). A differing existing file is a
  conflict and is never replaced. Same rules as M2.4.
Either node keeps working when the other is unreachable; the link catches up on reconnect.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HOST = "sne-cloud"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ClearAllForwardings=yes",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", HOST]
REMOTE_DATA = "StockGPLMDownloads/data"


def now() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


class Stream:
    """Push channel: remote ``tail -F`` over one SSH connection, resumable by byte offset."""

    def __init__(self, remote_file: str, on_line: Callable[[str], None]):
        self.remote_file, self.on_line = remote_file, on_line
        self.offset = 0
        self.state = {"state": "CONNECTING", "since": now(), "reconnects": 0, "lines": 0, "last_line": None}
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            cmd = f"tail -c +{self.offset + 1} -F {self.remote_file} 2>/dev/null"
            try:
                p = subprocess.Popen(SSH + [cmd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.state.update(state="CONNECTED", since=now())
                buf = b""
                while True:
                    chunk = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.offset += len(line) + 1  # committed only for whole lines
                        if line.strip():
                            self.on_line(line.decode())
                            self.state["lines"] += 1
                            self.state["last_line"] = now()
                p.wait(timeout=5)
            except Exception as exc:  # noqa: BLE001
                self.state["error"] = str(exc)[:120]
            self.state.update(state="RECONNECTING", since=now())
            self.state["reconnects"] += 1
            time.sleep(5)


class CloudSync:
    def __init__(self, symbols: Sequence[str], daily_root: Path, minute_roots: Sequence[Path], staging: Path,
                 on_new: Callable[[list[str]], None] | None = None, index_root: Path | None = None):
        self.symbols = [s if s.endswith("-EQ") else f"{s}-EQ" for s in symbols if s.upper() not in ("NIFTY", "BANKNIFTY", "INDIAVIX")]
        self.daily_root, self.minute_roots = Path(daily_root), [Path(p) for p in minute_roots]
        self.staging = Path(staging)
        # official NSE index history the cloud publishes (deploy/oracle/live/index_history.py), all indices
        self.index_root = Path(index_root) if index_root else None
        self.on_new = on_new
        self.state: dict[str, Any] = {"state": "IDLE", "pulled_total": 0, "conflicts": [], "last_run": None,
                                      "pending": None, "cloud_inventory": None, "error": None}

    # remote rel path -> local destination
    def _dest(self, rel: str) -> Path:
        parts = rel.split("/")  # minute/shoonya/NSE/SYM/file  |  daily/shoonya/NSE/SYM/file  |  daily/nse-index/...
        if parts[1] == "nse-index" and self.index_root:
            return self.index_root / parts[3] / parts[4]
        return (self.minute_roots[0] if parts[0] == "minute" else self.daily_root) / parts[3] / parts[4]

    def _have(self, rel: str) -> bool:
        parts = rel.split("/")
        if parts[1] == "nse-index":
            return self._dest(rel).exists()
        if parts[0] == "minute":
            return any((r / parts[3] / parts[4]).exists() for r in self.minute_roots)
        return (self.daily_root / parts[3] / parts[4]).exists()

    def discover(self) -> list[str]:
        paths = "\n".join(f"{k}/shoonya/NSE/{s}" for s in self.symbols for k in ("minute", "daily"))
        idx = ("find daily/nse-index/NSE -mindepth 2 -maxdepth 2 -name '*.json.gz.done' 2>/dev/null; "
               if self.index_root else "")
        out = subprocess.run(SSH + [f"cd {REMOTE_DATA} && {idx}while IFS= read -r d; do find \"$d\" -maxdepth 1 "
                                    f"-name '*.json.gz.done' 2>/dev/null; done; cat ~/gplm-live/inventory.json 2>/dev/null; echo; "
                                    f"echo @@PROG@@; cat ~/gplm-live/universe_progress.json 2>/dev/null"],
                             input=paths.encode() + b"\n", capture_output=True, timeout=60)
        text, _, prog = out.stdout.decode().partition("@@PROG@@")
        lines = text.splitlines()
        try:  # Oracle stock-by-stock progress: provider window floor + universe completion
            p = json.loads(prog.strip() or "{}")
            if p:
                have = p.get("have") or {}
                self.state["universe"] = {"window": p.get("window"), "expected": p.get("expected"),
                                          "complete": len(p.get("complete") or []), "total": len(have),
                                          "started": sum(1 for v in have.values() if v), "current": p.get("current"),
                                          "generated": p.get("generated")}
                if p.get("window"):
                    self.floor = p["window"][0]
        except ValueError:
            pass
        inv = next((ln for ln in lines if ln.startswith("{")), None)
        if inv:
            try:
                i = json.loads(inv)
                self.cloud_inv_full = i
                self.state["cloud_inventory"] = {k: i.get(k) for k in ("state", "generated", "missing_total", "window",
                                                                     "last_pass", "problems")}
            except ValueError:
                pass
        return [ln[:-5] for ln in lines if ln.endswith(".json.gz.done")]  # payload rel paths

    def pull(self, payloads: list[str]) -> list[str]:
        from .cloud_retention import rpc, stamp
        ledger = [rel for rel in payloads if "/shoonya/" in rel]  # index files stay on the cloud (no retention)
        if ledger:
            rpc("begin", items=[{"rel": rel, "pc_started_at": stamp()} for rel in ledger])
        files = []
        for rel in payloads:
            files += [rel, rel.replace(".json.gz", ".meta.json"), rel + ".done"]
        # file list on stdin: Windows ssh truncates long command lines
        r = subprocess.run(SSH + [f"cd {REMOTE_DATA} && tar -cf - --verbatim-files-from -T -"],
                           input="\n".join(files).encode() + b"\n", capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[:200])
        blobs: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            for m in tf.getmembers():
                if m.isfile() and not m.name.startswith("/") and ".." not in m.name:
                    blobs[m.name] = tf.extractfile(m).read()
        done = []
        for rel in payloads:
            pay, meta, mark = blobs.get(rel), blobs.get(rel.replace(".json.gz", ".meta.json")), blobs.get(rel + ".done")
            if pay is None or meta is None or mark is None:
                continue
            mk = json.loads(mark)
            if mk.get("status") != "COMPLETE" or mk.get("bytes") != len(pay) or \
                    mk.get("sha256") != hashlib.sha256(pay).hexdigest():
                self.state["conflicts"].append({"file": rel, "why": "marker does not match payload"})
                continue
            dest = self._dest(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            ok = True
            for path, data in ((dest, pay), (dest.with_name(dest.name.replace(".json.gz", ".meta.json")), meta),
                               (dest.with_name(dest.name + ".done"), mark)):
                if path.exists():
                    if path.read_bytes() != data:
                        self.state["conflicts"].append({"file": str(path), "why": "existing local file differs"})
                        ok = False
                        break
                    continue
                tmp = path.with_name(f".sync-{os.getpid()}-{path.name}")
                tmp.write_bytes(data)
                try:
                    os.link(tmp, path)  # exclusive: fails if a file appeared meanwhile
                finally:
                    tmp.unlink(missing_ok=True)
            if ok:
                done.append(rel)
        if any("/shoonya/" in rel for rel in done):
            rpc("end", items=[{"rel": rel, "pc_completed_at": stamp()} for rel in done if "/shoonya/" in rel])
        return done

    # ---------------------------------------------------------------- watcher: what should exist locally
    def expected_sessions(self, n: int = 400) -> list[str]:
        """Every trading session from the provider window floor (oldest day Shoonya still serves) to today."""
        if not getattr(self, "calendar", None):
            raw = subprocess.run(SSH + ["cat StockGPLMDownloads/calendar/nse_2026.json"], capture_output=True,
                                 timeout=30).stdout
            self.calendar = json.loads(raw)
        from datetime import date, timedelta
        closed = set(self.calendar.get("closed_dates", []))
        t = datetime.now(IST)
        d, out = t.date(), []
        floor = getattr(self, "floor", None) or "2026-01-19"
        while len(out) < n and d.year == 2026 and d.isoformat() >= floor:
            if d.weekday() < 5 and d.isoformat() not in closed and (d < t.date() or t.strftime("%H:%M") >= "16:45"):
                out.append(d.isoformat())
            d -= timedelta(days=1)
        return out

    def local_minute(self, sym: str) -> set[str]:
        have = set()
        for r in self.minute_roots:
            p = r / sym
            if p.is_dir():
                have |= {f[:10] for f in os.listdir(p) if f.endswith(".json.gz.done")}
        return have

    def local_daily_last(self, sym: str) -> str | None:
        p = self.daily_root / sym
        ends = [f[11:21] for f in os.listdir(p) if f.endswith(".json.gz.done") and f[10] == "_"] if p.is_dir() else []
        return max(ends) if ends else None

    def run_once(self) -> None:
        self.state.update(state="CHECKING", error=None)
        remote = self.discover()
        remote_set = set(remote)
        fails: dict[str, dict[str, Any]] = self.state.setdefault("pull_failures", {})
        missing = [r for r in remote if not self._have(r) and fails.get(r, {}).get("status") != "GAVE_UP"]
        self.state["pending"] = len(missing)
        pulled: list[str] = []
        for i in range(0, len(missing), 60):
            batch = missing[i:i + 60]
            self.state["state"] = f"PULLING {i + len(batch)}/{len(missing)}"
            got = self.pull(batch)
            pulled += got
            for rel in batch:  # a file that arrives but fails verification is retried at most 3 times
                if rel in got:
                    fails.pop(rel, None)
                else:
                    f = fails.setdefault(rel, {"attempts": 0})
                    f["attempts"] += 1
                    f["status"] = "GAVE_UP" if f["attempts"] >= 3 else "RETRYING"
        # per-stock picture: expected sessions vs local, and who is responsible for each gap
        exp = self.expected_sessions()
        yday = exp[0] if exp and exp[0] < datetime.now(IST).date().isoformat() else (exp[1] if len(exp) > 1 else None)
        inv_syms = (self.cloud_inv_full or {}).get("symbols", {}) if hasattr(self, "cloud_inv_full") else {}
        report = {}
        for s in self.symbols:
            have = self.local_minute(s)
            miss = [d for d in exp if d not in have]
            on_cloud = [d for d in miss if f"minute/shoonya/NSE/{s}/{d}.json.gz" in remote_set]
            waiting = [d for d in miss if d not in on_cloud]
            bad = [r for r, f in fails.items() if f"/{s}/" in r and f.get("status") == "GAVE_UP"]
            ch = (inv_syms.get(s) or {}).get("health") or {}
            dl = self.local_daily_last(s)
            if bad or ch.get("status") == "PROBLEM":
                status = "PROBLEM"
            elif on_cloud:
                status = "SYNCING"
            elif waiting:
                status = "WAITING_CLOUD"
            else:
                status = "OK"
            report[s] = {"status": status, "missing": len(miss), "on_cloud": len(on_cloud), "waiting_cloud": waiting[:10],
                         "daily_last": dl, "daily_behind": bool(yday and (not dl or dl < yday) and not have >= {yday}),
                         "cloud_health": ch, "pull_problems": bad[:5]}
        self.state["symbols"] = report
        self.state["summary"] = {k: sum(1 for v in report.values() if v["status"] == k)
                                 for k in ("OK", "SYNCING", "WAITING_CLOUD", "PROBLEM")}
        self.state["expected_window"] = [exp[-1], exp[0]] if exp else None
        self.state.update(state="IN_SYNC" if not missing or len(pulled) == len(missing) else "PARTIAL",
                          last_run=now(), pending=len(missing) - len(pulled), remote_files=len(remote))
        self.state["pulled_total"] += len(pulled)
        if pulled:
            self.state["last_pulled"] = pulled[-5:]
            if self.on_new:
                self.on_new(pulled)

    def reset(self, sym: str) -> None:
        s = sym if sym.endswith("-EQ") else f"{sym}-EQ"
        fails = self.state.get("pull_failures", {})
        for k in [k for k in fails if f"/{s}/" in k]:
            del fails[k]
        subprocess.run(SSH + [f"~/stock-gplm-venv/bin/python ~/gplm-live/gplm_live_ctl.py reset {shlex.quote(s)}"],
                       capture_output=True, timeout=30)

    def loop(self, every: int = 30) -> None:
        """Watch continuously: every 30 s while the cloud answers, backing off to 2 min when it does not."""
        while True:
            try:
                self.run_once()
                wait = every
            except Exception as exc:  # noqa: BLE001
                self.state.update(state="CLOUD_UNREACHABLE", error=str(exc)[:200], last_run=now())
                wait = 120
            time.sleep(wait)


def start_threads(objs: Sequence[tuple[Callable[..., None], tuple]]) -> None:
    for fn, args in objs:
        threading.Thread(target=fn, args=args, daemon=True).start()


def pull_indices(config: str = "config/app/gplm.windows.json") -> int:
    """One-shot: pull every official NSE index file the cloud has published into ``data.index_root``.

        python -m stock_gplm.app.cloud_link indices
    """
    from .settings import load
    s = load(Path(config))
    if not s.index_root:
        raise SystemExit("config has no data.index_root")
    sync = CloudSync([], s.daily_root, s.minute_roots, s.index_root / "_staging", index_root=s.index_root)
    remote = [r for r in sync.discover() if "/nse-index/" in r]
    missing = [r for r in remote if not sync._have(r)]
    got: list[str] = []
    for i in range(0, len(missing), 60):
        got += sync.pull(missing[i:i + 60])
    print(f"indices: {len(remote)} files on cloud, {len(missing)} missing, {len(got)} pulled, "
          f"{len(sync.state['conflicts'])} conflicts -> {s.index_root}")
    return 0 if len(got) == len(missing) else 1


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["indices"]:
        raise SystemExit(pull_indices(*sys.argv[2:3]))
    raise SystemExit("usage: python -m stock_gplm.app.cloud_link indices [config]")
