"""Pull universe stocks from Oracle as soon as Oracle has completed their minute history (stock-by-stock filler).

Reads ~/gplm-live/universe_progress.json (written by cloud_sync.py --order symbol), and for every stock in its
"complete" list that is not complete locally, pulls the missing minute files with CloudSync.pull (sha-verified,
never overwrites). Watchlist stocks are left to the live lab's own sync. Loop every --every seconds.
Usage (PYTHONPATH=src): python -m stock_gplm.app.universe_sync
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .cloud_link import SSH, CloudSync
from .live_lab import REPO


def log(msg: str) -> None:
    p = REPO / "var" / "runs" / "universe-sync.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def local_count(roots: list[Path], sym: str) -> int:
    have: set[str] = set()
    for r in roots:
        d = r / sym
        if d.is_dir():
            have |= {f[:10] for f in os.listdir(d) if f.endswith(".json.gz.done") and f[10] == "."}
    return len(have)


def run_once(sync_roots: tuple[Path, list[Path]], watch: set[str], staging: Path) -> None:
    daily_root, minute_roots = sync_roots
    r = subprocess.run(SSH + ["cat ~/gplm-live/universe_progress.json 2>/dev/null"], capture_output=True, timeout=60)
    try:
        prog = json.loads(r.stdout.decode() or "{}")
    except ValueError:
        log("progress file unreadable")
        return
    # pull every stock (watchlist too: the live lab only syncs its last 60 sessions) where Oracle has more days
    remote = prog.get("have") or {}
    todo = [s for s, n in remote.items() if n and local_count(minute_roots, s) < n]
    log(f"oracle complete {len(prog.get('complete', []))}, current {prog.get('current')}, to pull {len(todo)}")
    for i in range(0, len(todo), 5):
        batch = todo[i:i + 5]
        cs = CloudSync(batch, daily_root, minute_roots, staging)
        rels = [x for x in cs.discover() if x.startswith("minute/") and not cs._have(x)]
        got = 0
        for j in range(0, len(rels), 300):
            got += len(cs.pull(rels[j:j + 300]))
        log(f"pulled {got} files for {','.join(batch)}; conflicts {len(cs.state['conflicts'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=int, default=600)
    a = ap.parse_args()
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    wl = (REPO / "deploy" / "oracle" / "live" / "watchlist.txt").read_text().splitlines()
    watch = {s + "-EQ" for ln in wl for s in ln.split("#", 1)[0].split()}
    roots = (Path(st.daily_root), [Path(r) for r in st.minute_roots])
    while True:
        try:
            run_once(roots, watch, REPO / "var" / "live-lab" / "_staging-universe")
        except Exception as exc:  # network etc.: next round
            log(f"error: {exc}")
        time.sleep(a.every)


if __name__ == "__main__":
    main()
