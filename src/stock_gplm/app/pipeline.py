"""Unattended research pipeline: keeps GPU, CPU and the data APIs busy in parallel (owner, 2026-11-20).

Lanes run at the same time; a step starts when the steps it depends on are finished:

  api   NSE MCP daily history -> Yahoo long daily history (network/API bound)
  gpu   up-move learner on current data: 1d, 60m, 15m for the whole universe (GPU triple screen)
  cpu   recency research, intraday 15m/60m, top ``--intraday-top`` stocks (CPU workers)
  long  after the api lane: up-move learner 1d + recency 1d/1w on the long-history config

Every underlying program is resumable, so rerunning the same ``--run`` continues where it stopped; finished
steps of the run are skipped. Logs, status.json and SUMMARY.md go to <state>/reports/pipeline/<run>/.

  $env:PYTHONPATH="src"; python -m stock_gplm.app.pipeline [--run 2026-11-20] [--universe file] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

APP = Path(__file__).resolve().parents[3]
ROOT = APP.parent


def steps(universe: str, top: int, gpu_workers: int, cpu_workers: int) -> list[dict[str, Any]]:
    py = [sys.executable, "-m"]
    cfg, long_cfg = "config/app/gplm.windows.json", "config/app/gplm.windows.longdaily.json"
    up = py + ["stock_gplm.app.upmove", "--stocks", f"@{universe}", "--workers", str(gpu_workers)]
    rec = py + ["stock_gplm.app.recency", "--universe", universe, "--workers", str(cpu_workers)]
    return [
        {"name": "nse_history", "lane": "api", "after": [],
         "cmd": py + ["stock_gplm.app.nse_history", "all", "--stocks", f"@{universe}"]},
        {"name": "yahoo_history", "lane": "api", "after": ["nse_history"],
         "cmd": py + ["stock_gplm.app.yahoo_history", "all", "--stocks", f"@{universe}"]},
        {"name": "upmove_1d", "lane": "gpu", "after": [],
         "cmd": up + ["--config", cfg, "--tf", "1d", "--name", "universe-1d"]},
        {"name": "upmove_60m", "lane": "gpu", "after": ["upmove_1d"],
         "cmd": up + ["--config", cfg, "--tf", "60m", "--name", "universe-60m"]},
        {"name": "upmove_15m", "lane": "gpu", "after": ["upmove_60m"],
         "cmd": up + ["--config", cfg, "--tf", "15m", "--name", "universe-15m"]},
        # down-move learner (owner 2026-11-20: intraday shorts / PE buys), after the long side on the GPU lane
        {"name": "downmove_1d", "lane": "gpu", "after": ["upmove_15m"],
         "cmd": up + ["--config", cfg, "--tf", "1d", "--side", "short", "--name", "universe-1d-short"]},
        {"name": "downmove_60m", "lane": "gpu", "after": ["downmove_1d"],
         "cmd": up + ["--config", cfg, "--tf", "60m", "--side", "short", "--name", "universe-60m-short"]},
        {"name": "downmove_15m", "lane": "gpu", "after": ["downmove_60m"],
         "cmd": up + ["--config", cfg, "--tf", "15m", "--side", "short", "--name", "universe-15m-short"]},
        {"name": "recency_intraday", "lane": "cpu", "after": [],
         "cmd": rec + ["--config", cfg, "--max-symbols", str(top), "--timeframes", "15m,60m", "--name",
                       "intraday-top", "--library", str(ROOT / "state-intraday" / "strategy-library")]},
        {"name": "upmove_1d_long", "lane": "long", "after": ["yahoo_history"],
         "cmd": up + ["--config", long_cfg, "--tf", "1d", "--name", "universe-1d-long"]},
        {"name": "recency_long", "lane": "long", "after": ["upmove_1d_long", "recency_intraday"],
         "cmd": rec + ["--config", long_cfg, "--timeframes", "1d,1w", "--name", "universe-long",
                       "--library", str(ROOT / "state-wide" / "strategy-library")]},
    ]


class Runner:
    def __init__(self, out: Path, plan: list[dict[str, Any]], dry: bool) -> None:
        self.out, self.plan, self.dry = out, plan, dry
        self.status: dict[str, dict[str, Any]] = {}
        sf = out / "status.json"
        if sf.is_file():
            self.status = json.loads(sf.read_text(encoding="utf-8"))
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)

    def save(self) -> None:
        (self.out / "status.json").write_text(json.dumps(self.status, indent=1), encoding="utf-8")

    def finished(self, name: str) -> bool:
        return self.status.get(name, {}).get("state") in ("DONE", "FAILED")

    def run_step(self, s: dict[str, Any]) -> None:
        with self.cv:
            self.cv.wait_for(lambda: all(self.finished(d) for d in s["after"]))
            if self.status.get(s["name"], {}).get("state") == "DONE":
                return
            self.status[s["name"]] = {"state": "RUNNING", "start": datetime.now().isoformat(timespec="seconds"),
                                      "cmd": " ".join(s["cmd"])}
            self.save()
        log = self.out / f"{s['name']}.log"
        t0 = time.monotonic()
        if self.dry:
            code = 0
        else:
            env = {**os.environ, "PYTHONPATH": str(APP / "src"), "OPENBLAS_NUM_THREADS": "1",
                   "PYTHONIOENCODING": "utf-8"}
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} {' '.join(s['cmd'])}\n")
                fh.flush()
                code = subprocess.call(s["cmd"], cwd=APP, env=env, stdout=fh, stderr=subprocess.STDOUT)
        with self.cv:
            self.status[s["name"]].update({"state": "DONE" if code == 0 else "FAILED", "exit": code,
                                           "end": datetime.now().isoformat(timespec="seconds"),
                                           "minutes": round((time.monotonic() - t0) / 60, 1)})
            self.save()
            self.cv.notify_all()

    def lane(self, name: str) -> None:
        for s in [x for x in self.plan if x["lane"] == name]:
            self.run_step(s)

    def run(self) -> None:
        threads = [threading.Thread(target=self.lane, args=(ln,), daemon=False)
                   for ln in dict.fromkeys(x["lane"] for x in self.plan)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


def summary(out: Path, status: dict[str, Any]) -> None:
    lines = [f"# Pipeline run {out.name}", "", "| Step | State | Minutes | Log |", "|---|---|---|---|"]
    for k, v in status.items():
        lines.append(f"| {k} | {v.get('state')} | {v.get('minutes')} | {k}.log |")
    rep = ROOT / "state" / "reports"
    lines += ["", "Results:",
              f"- Up-move 1d/60m/15m: {rep / 'upmove'}/universe-*/summary.md (today.csv = firing now)",
              f"- Down-move (short / PE) 1d/60m/15m: {rep / 'upmove'}/universe-*-short/summary.md",
              f"- Up-move 1d long history: {ROOT / 'state-long' / 'reports' / 'upmove' / 'universe-1d-long'}",
              f"- Recency intraday: {rep / 'recency' / 'intraday-top'}/summary.md",
              f"- Recency 1d/1w long: {ROOT / 'state-long' / 'reports' / 'recency' / 'universe-long'}/summary.md"]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=date.today().isoformat())
    ap.add_argument("--universe", default="deploy/oracle/live/universe.txt")
    ap.add_argument("--intraday-top", type=int, default=200)
    ap.add_argument("--gpu-workers", type=int, default=6)
    ap.add_argument("--cpu-workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    out = ROOT / "state" / "reports" / "pipeline" / a.run
    out.mkdir(parents=True, exist_ok=True)
    r = Runner(out, steps(a.universe, a.intraday_top, a.gpu_workers, a.cpu_workers), a.dry_run)
    for k, v in list(r.status.items()):  # a step interrupted mid-run is retried
        if v.get("state") in ("RUNNING", "FAILED"):
            r.status.pop(k)
    r.run()
    summary(out, r.status)
    print((out / "SUMMARY.md").read_text(encoding="utf-8"))
    return 0 if all(v.get("state") == "DONE" for v in r.status.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
