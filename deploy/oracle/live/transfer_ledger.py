"""Owner-authorized cloud staging receipts. Retain this ledger after deleting triplets."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / "StockGPLMDownloads/data"
DB = Path.home() / "gplm-live/transfer-ledger.sqlite"
REL = re.compile(r"(minute|daily)/shoonya/NSE/([A-Z0-9&_.-]+-EQ)/(\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?)\.json\.gz")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def connect():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA synchronous=FULL")
    c.execute("CREATE TABLE IF NOT EXISTS receipts(rel TEXT PRIMARY KEY, manifest TEXT NOT NULL, state TEXT NOT NULL, pc TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT NOT NULL, item TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)")
    return c


_ARCHIVE_CONN = None


def archive_connection():
    """Reuse one read connection; opening and schema-checking per symbol is costly."""
    global _ARCHIVE_CONN
    if _ARCHIVE_CONN is None:
        _ARCHIVE_CONN = sqlite3.connect(DB, timeout=60)
    return _ARCHIVE_CONN


def event(c, item, kind, detail=None):
    c.execute("INSERT INTO events(at,item,event,detail) VALUES(?,?,?,?)", (stamp(), item, kind, json.dumps(detail or {})))


def download_event(item, kind, detail=None):
    with connect() as c:
        event(c, item, kind, detail)


def paths(rel):
    if not REL.fullmatch(rel):
        raise ValueError("invalid triplet path")
    names = [rel, rel.replace(".json.gz", ".meta.json"), rel + ".done"]
    out = [ROOT / n for n in names]
    for p in out:
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("symlink or escaped staging path")
    return out


def digest(p):
    if p.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("triplet component exceeds bound")
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return {"bytes": p.stat().st_size, "sha256": h.hexdigest()}


def manifest(rel):
    pp = paths(rel)
    hashes = [digest(p) for p in pp]
    mk = json.loads(pp[2].read_text())
    json.loads(pp[1].read_text())
    if mk.get("status") != "COMPLETE" or any(mk.get(k) != hashes[0][k] for k in ("bytes", "sha256")):
        raise ValueError("publication marker disagrees with payload")
    return hashes


def archived(kind, sym):
    """PC-verified receipts remain logical coverage even when cloud bytes are gone."""
    if not DB.exists():
        return set()
    prefix = f"{kind}/shoonya/NSE/{sym}/"
    rows = archive_connection().execute(
        "SELECT rel FROM receipts WHERE state IN ('VERIFIED','DELETED') AND rel GLOB ?", (prefix + "*",))
    return {r[0][len(prefix):-8] for r in rows if r[0].startswith(prefix)}


def listing(limit=200):
    out = []
    with connect() as c:
        # A crash after partial deletion is recovered using the durable receipt.
        for rel, m in c.execute("SELECT rel,manifest FROM receipts WHERE state='VERIFIED' LIMIT ?", (limit,)):
            out.append({"rel": rel, "manifest": json.loads(m)})
        seen = {x["rel"] for x in out}
        for kind in ("minute", "daily"):
            for p in sorted((ROOT / kind / "shoonya/NSE").glob("*-EQ/*.json.gz.done")):
                if len(out) >= limit:
                    return out
                rel = p.relative_to(ROOT).as_posix()[:-5]
                if rel in seen:
                    continue
                try:
                    m = manifest(rel)
                except (ValueError, OSError):
                    continue  # remains on cloud; never delete invalid publications
                old = c.execute("SELECT manifest,state FROM receipts WHERE rel=?", (rel,)).fetchone()
                if old and old[0] != json.dumps(m):
                    raise ValueError(f"immutable triplet changed: {rel}")
                if old and old[1] == "DELETED":
                    continue
                if not old:
                    c.execute("INSERT INTO receipts VALUES(?,?,?,NULL)", (rel, json.dumps(m), "STAGED"))
                    event(c, rel, "CLOUD_PUBLICATION_VERIFIED", {"manifest": m,
                          "historical_download_times": "UNKNOWN unless present in download events"})
                out.append({"rel": rel, "manifest": m})
    return out


def acknowledge(item):
    rel, m = item["rel"], item["manifest"]
    pp = paths(rel)
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT manifest,state FROM receipts WHERE rel=?", (rel,)).fetchone()
        if not row or json.loads(row[0]) != m:
            raise ValueError("PC receipt does not match registered cloud manifest")
        if row[1] == "DELETED":
            return
        for p, expected in zip(pp, m):
            if p.exists():
                if digest(p) != expected:
                    raise ValueError("cloud file changed before deletion")
            elif row[1] != "VERIFIED":
                raise ValueError("cloud file missing before verification")
        if row[1] != "VERIFIED":
            event(c, rel, "PC_VERIFIED", item)
            c.execute("UPDATE receipts SET state='VERIFIED',pc=? WHERE rel=?", (json.dumps(item), rel))
        event(c, rel, "DELETE_INTENT", {"manifest": m})
    # Durable PC receipt precedes unlink; retries tolerate partial deletion.
    for i in (2, 0, 1):
        p = pp[i]
        if p.exists():
            if digest(p) != m[i]:
                raise ValueError("cloud component changed during deletion")
            p.unlink()
    with connect() as c:
        event(c, rel, "CLOUD_DELETED", {"manifest": m})
        c.execute("UPDATE receipts SET state='DELETED' WHERE rel=?", (rel,))


def main():
    req = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024))
    if req["op"] == "list":
        result = listing()
    elif req["op"] in ("begin", "end"):
        with connect() as c:
            for item in req["items"]:
                paths(item["rel"])
                event(c, item["rel"], "PC_TRANSFER_STARTED" if req["op"] == "begin" else "PC_TRANSFER_COMPLETED", item)
        result = {"ok": True}
    elif req["op"] == "ack":
        result = []
        for item in req["items"]:
            acknowledge(item)
            result.append(item["rel"])
    else:
        raise ValueError("unknown operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
