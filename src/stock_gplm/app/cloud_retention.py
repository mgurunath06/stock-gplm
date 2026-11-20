"""Verify local triplets, acknowledge to Oracle, and retire matching cloud staging bytes."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .cloud_link import CloudSync, SSH
from .live_lab import REPO
from .settings import load


def stamp():
    return datetime.now(timezone.utc).isoformat()


def rpc(op, **kwargs):
    r = subprocess.run(SSH + ["~/stock-gplm-venv/bin/python ~/gplm-live/transfer_ledger.py"],
                       input=json.dumps({"op": op, **kwargs}).encode(), capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace")[-1000:])
    return json.loads(r.stdout)


def local_receipt(sync, item):
    rel = item["rel"]
    parts = rel.split("/")
    roots = sync.minute_roots if parts[0] == "minute" else [sync.daily_root]
    for root in roots:
        p = root / parts[3] / parts[4]
        pp = [p, p.with_name(p.name.replace(".json.gz", ".meta.json")), p.with_name(p.name + ".done")]
        try:
            actual = []
            for f in pp:
                if f.is_symlink() or not f.resolve().is_relative_to(root.resolve()):
                    raise ValueError("unsafe local path")
                h = hashlib.sha256()
                with f.open("rb") as stream:
                    for b in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(b)
                actual.append({"bytes": f.stat().st_size, "sha256": h.hexdigest()})
            if actual == item["manifest"]:
                return {**item, "pc_host": socket.gethostname(), "pc_paths": [str(f) for f in pp],
                        "pc_verified_at": stamp()}
        except (OSError, ValueError):
            pass
    return None


def cycle(sync, audit):
    items = rpc("list")
    if not items:
        return 0
    missing = [i["rel"] for i in items if local_receipt(sync, i) is None]
    if missing:
        sync.pull(missing)
    receipts = [r for i in items if (r := local_receipt(sync, i)) is not None]
    # Keep a second audit copy on the PC, flushed before the destructive acknowledgement.
    with audit.open("a", encoding="utf-8") as f:
        for r in receipts:
            f.write(json.dumps({"event": "PC_VERIFIED", **r}) + "\n")
        f.flush()
        os.fsync(f.fileno())
    deleted = rpc("ack", items=receipts) if receipts else []
    with audit.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "CLOUD_DELETE_CONFIRMED", "at": stamp(), "files": deleted}) + "\n")
    print(f"{stamp()} checked={len(items)} verified={len(receipts)} deleted={len(deleted)}", flush=True)
    return len(deleted)


def main():
    s = load(REPO / "config/app/gplm.windows.json")
    sync = CloudSync([], s.daily_root, s.minute_roots, REPO / "var/live-lab/_retention")
    audit = REPO / "var/runs/cloud-transfer-audit.jsonl"
    while True:
        try:
            count = cycle(sync, audit)
        except Exception as exc:
            print(f"{stamp()} ERROR {exc}", flush=True)
            count = 0
        time.sleep(5 if count else 60)


if __name__ == "__main__":
    main()
