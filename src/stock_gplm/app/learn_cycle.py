"""Continuous learning outside market hours (owner 2026-11-21): daily, weekly, monthly; stock x TF and sector x TF.

    python -m stock_gplm.app.learn_cycle daily|weekly|monthly [--workers 20] [--skip-fetch]

daily   (Mon-Fri evening) 1. new daily candles from the official NSE MCP for every universe stock whose
          Shoonya daily is behind (only dates after Shoonya's last one -> data/derived/nse-mcp-recent/NSE);
        2. every strategy re-tested on the last 12 months with the CURRENT exits (1d: whole universe;
          5m/15m/60m: stocks with complete minute data) - the new outcomes are the learning;
        3. re-scored: stock x TF and sector x TF Confidence / RR / Edge / ACTIVE (recent trades weigh more).
weekly  (Saturday) as daily, plus exits re-learned on the same 12 months into a staged copy of exits.json,
        promoted (old one kept in history) only when the run finished.
monthly (first Sunday) as weekly, plus the long-history daily check: 2010..latest with exits re-learned into
        state-long/.../exits-long.json and COMPARE.md against this month's 12-month run (not promoted: owner).
Outputs: <state>/learning/current/ (active.json = what live alerts use, sector_active.json, CURRENT_LEARNINGS.csv,
BEST_STRATEGY_PER_STOCK.csv, stock_tf.csv, sector_tf.csv, SECTOR_TF.md, GROUPS.md, CHANGES.md, RUN.md) and a copy
per run in <state>/learning/history/<date>-<cadence>/. A lock stops two cycles overlapping. Logs: learning/cycle.log.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .settings import load

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "config" / "app" / "gplm.windows.json"
LONG_CFG = REPO / "config" / "app" / "gplm.windows.longdaily.json"
UNIVERSE = REPO / "deploy" / "oracle" / "live" / "universe.txt"
RECENT = REPO.parent / "data" / "derived" / "nse-mcp-recent" / "NSE"
EXITS = REPO / "config" / "strategies" / "exits.json"
INTRADAY_FROM = "2026-01-19"  # first day of complete 1-minute history
KEEP = {"daily": 7, "weekly": 8, "monthly": 1000}  # run dirs kept per cadence


class Cycle:
    def __init__(self, cadence: str, workers: int) -> None:
        self.cadence, self.workers = cadence, workers
        self.s = load(CFG)
        self.home = self.s.reports_dir.parent
        self.root = self.home / "learning"
        self.today = date.today().isoformat()
        self.tag = f"{self.today}-{cadence}"
        # owner 2026-11-21: no pre-2026 data without explicit permission -> window never starts before 2026-01-01
        self.start = max((date.today() - timedelta(days=365)).isoformat(), "2026-01-01")
        self.runs = self.s.reports_dir / "forward" / "cycle"

    def log(self, msg: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "cycle.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{self.tag}] {msg}\n")

    # ---------------------------------------------------------------- 1. data
    def fetch_daily(self, stocks: list[str]) -> None:
        behind = []
        for s in stocks:
            last = _last_daily(self.s.daily_root / f"{s}-EQ")
            if last and last < self.today:
                behind.append((s, last))
        self.log(f"fetch: {len(behind)} of {len(stocks)} stocks have Shoonya daily before {self.today}")
        if behind:
            n = asyncio.run(_fetch_recent(behind, self.today, lanes=4))
            self.log(f"fetch: wrote recent daily candles for {n} stocks")

    def intraday_stocks(self, stocks: list[str]) -> list[str]:
        cnt = {}
        for s in stocks:
            have = set()
            for r in self.s.minute_roots:
                d = r / f"{s}-EQ"
                if d.is_dir():
                    have |= {f[:10] for f in os.listdir(d) if f.endswith(".json.gz.done") and f[:10] >= INTRADAY_FROM}
            cnt[s] = len(have)
        # 2026-11-21: a stock listed / renamed after INTRADAY_FROM is complete with every session since that day
        # (data/reference/listing_dates.json from universe_build.py); reference calendar = the fullest stock.
        full = max(cnt.values(), default=0)
        if not full:
            return []
        ref_sym = max(cnt, key=cnt.get)
        cal = sorted({f[:10] for r in self.s.minute_roots if (r / f"{ref_sym}-EQ").is_dir()
                      for f in os.listdir(r / f"{ref_sym}-EQ") if f.endswith(".json.gz.done") and f[:10] >= INTRADAY_FROM})
        lf = REPO.parent / "data" / "reference" / "listing_dates.json"
        listed = json.loads(lf.read_text(encoding="utf-8")) if lf.is_file() else {}
        out = []
        for s, n in cnt.items():
            first = listed.get(s, INTRADAY_FROM)
            expected = sum(1 for d in cal if d >= first) or full
            if n >= 0.97 * expected and n >= 20:
                out.append(s)
        return out

    # ---------------------------------------------------------------- 2. trades
    def batch(self, name: str, stocks: list[str], start: str, tfs: str, learn: bool, cfg: Path = CFG,
              env: dict | None = None) -> Path:
        lst = self.runs / f"{self.tag}-{name}.txt"
        lst.parent.mkdir(parents=True, exist_ok=True)
        lst.write_text("\n".join(stocks) + "\n", encoding="utf-8")
        cmd = [sys.executable, "-m", "stock_gplm.app.forward_batch", "--stocks", f"@{lst}", "--from", start,
               "--timeframes", tfs, "--config", str(cfg), "--name", f"cycle/{self.tag}/{name}", "--workers", str(self.workers)]
        if learn:
            cmd.append("--learn-exits")
        t0 = time.monotonic()
        rc = subprocess.run(cmd, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src"), **(env or {})},
                            capture_output=True, text=True).returncode
        self.log(f"batch {name}: {len(stocks)} stocks, {tfs}, from {start}, learn={learn}, rc={rc}, "
                 f"{(time.monotonic() - t0) / 60:.0f} min")
        if rc:
            raise RuntimeError(f"forward_batch {name} failed (rc={rc})")
        return load(cfg).reports_dir / "forward" / "cycle" / self.tag / name

    # ---------------------------------------------------------------- 3. scores + reports
    def report(self, run_dirs: list[Path], exits_file: Path) -> Path:
        from .confidence import active_map, score
        from .group_report import build as groups, sectors
        frames = [pd.read_csv(d / "all_trades.csv") for d in run_dirs if (d / "all_trades.csv").is_file()]
        t = pd.concat(frames, ignore_index=True)
        cur = self.root / "current"
        prev_active = json.loads((cur / "active.json").read_text(encoding="utf-8")) if (cur / "active.json").is_file() else {}
        out = self.root / "history" / self.tag
        out.mkdir(parents=True, exist_ok=True)
        sec = sectors()
        # stock x TF
        sc = score(t)
        sc["sector"] = sc.stock.map(sec).fillna("Unknown")
        agg = t.groupby(["stock", "timeframe", "strategy_id"]).net_pct.agg(max_trade="max", worst_trade="min").reset_index()
        sc = sc.merge(agg, on=["stock", "timeframe", "strategy_id"], how="left")
        ex = json.loads(exits_file.read_text(encoding="utf-8")).get("stocks", {})
        sc["exit"] = [_exit_text(ex, r.stock, r.timeframe, r.strategy_id) for r in sc.itertuples()]
        sc.to_csv(out / "stock_tf.csv", index=False)
        wf_file = self.root / "walkforward" / "active.json"
        wf_map = json.loads(wf_file.read_text(encoding="utf-8")) if wf_file.is_file() else None
        if wf_map is not None:  # owner 2026-11-21: tables follow the walk-forward ACTIVE list (unseen months)
            keys = {(s, tf, i) for s, x in wf_map.items() for tf, ids in x.items() for i in ids}
            act = sc[[k in keys for k in zip(sc.stock, sc.timeframe, sc.strategy_id)]].copy()
        else:
            act = sc[sc.status == "ACTIVE"].copy()
        act["total"] = (act.trades * act.avg_net_pct).round(1)
        cols = ["stock", "sector", "timeframe", "name", "strategy_id", "side", "trades", "win_rate", "avg_net_pct",
                "max_trade", "worst_trade", "total", "confidence", "rr", "edge", "exit", "last_trade"]
        act[cols].sort_values(["stock", "timeframe", "edge"], ascending=[True, True, False]).to_csv(
            out / "CURRENT_LEARNINGS.csv", index=False)
        act.sort_values(["stock", "edge"], ascending=[True, False]).groupby("stock").head(1)[cols].to_csv(
            out / "BEST_STRATEGY_PER_STOCK.csv", index=False)
        (out / "active_insample.json").write_text(json.dumps(active_map(sc), indent=1), encoding="utf-8")
        wf_active = self.root / "walkforward" / "active.json"  # owner 2026-11-21: ACTIVE = walk-forward (unseen) only
        new_active = json.loads(wf_active.read_text(encoding="utf-8")) if wf_active.is_file() else {}
        (out / "active.json").write_text(json.dumps(new_active, indent=1), encoding="utf-8")
        # sector x TF: the same scoring with all stocks of a sector pooled (a prior for stocks with few trades)
        st = t.assign(stock=t.stock.map(sec).fillna("Unknown"))
        ss = score(st).rename(columns={"stock": "sector"})
        ss.to_csv(out / "sector_tf.csv", index=False)
        sact: dict[str, dict[str, list[str]]] = {}
        for r in ss[ss.status == "ACTIVE"].sort_values("edge", ascending=False).itertuples():
            sact.setdefault(r.sector, {}).setdefault(r.timeframe, []).append(r.strategy_id)
        (out / "sector_active.json").write_text(json.dumps(sact, indent=1), encoding="utf-8")
        (out / "SECTOR_TF.md").write_text(_sector_md(ss, self.tag), encoding="utf-8")
        (out / "GROUPS.md").write_text(groups(out / "CURRENT_LEARNINGS.csv"), encoding="utf-8")
        (out / "CHANGES.md").write_text(_changes(prev_active, new_active, sc, self.tag), encoding="utf-8")
        (out / "RUN.md").write_text(
            f"# Learning cycle {self.tag}\n\nWindow {self.start} .. latest (12 months). Trades {len(t):,}; stocks "
            f"{t.stock.nunique()}; stock x TF x strategy scored {len(sc):,}; ACTIVE {len(act)} on {act.stock.nunique()} "
            f"stocks; sector x TF ACTIVE {int((ss.status == 'ACTIVE').sum())}. Exits: `{exits_file}`.\n"
            f"Runs: {', '.join(f'`{d}`' for d in run_dirs)}.\n", encoding="utf-8")
        tmp = self.root / ".current.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(out, tmp)
        shutil.rmtree(cur, ignore_errors=True)
        os.replace(tmp, cur)
        self.log(f"report: ACTIVE {len(act)} on {act.stock.nunique()} stocks; sector x TF ACTIVE "
                 f"{int((ss.status == 'ACTIVE').sum())} -> {cur}")
        return out

    def prune(self) -> None:
        for cad, keep in KEEP.items():
            dirs = sorted(d for d in self.runs.glob(f"*-{cad}") if d.is_dir())
            for d in dirs[:-keep]:
                shutil.rmtree(d, ignore_errors=True)
                self.log(f"pruned {d.name}")

    # ---------------------------------------------------------------- main
    def run(self, fetch: bool = True, only: list[str] | None = None) -> None:
        stocks = only or [x.strip().removesuffix("-EQ") for x in UNIVERSE.read_text(encoding="utf-8").split() if x.strip()]
        if fetch:
            self.fetch_daily(stocks)
        intra = self.intraday_stocks(stocks)
        learn = self.cadence in ("weekly", "monthly")
        self.log(f"start: universe {len(stocks)}, intraday-complete {len(intra)}, walk-forward={learn}")
        if learn:  # owner 2026-11-21: ACTIVE + exits come from the walk-forward (unseen months only)
            self.walk_forward(stocks, intra)
        dirs = [self.batch("daily", stocks, self.start, "1d,1w,1mo", False)]
        if intra:
            dirs.append(self.batch("intraday", intra, max(self.start, INTRADAY_FROM), "5m,15m,30m,60m", False))
        self.report(dirs, EXITS)
        if self.cadence == "monthly":
            self.long_check(stocks, dirs[0])
        self.prune()
        (self.root / f"{self.cadence}.done").write_text(self.tag + "\n", encoding="utf-8")

    def walk_forward(self, stocks: list[str], intra: list[str]) -> None:
        """Expanding monthly walk-forward over 2026 (walk_forward.py): ACTIVE = unseen record passes the rule;
        exits learned on all data are promoted into exits.json (previous copy kept in history)."""
        from .walk_forward import report as wf_report  # noqa: F401  (import check before a long run)
        runs = [("wf-daily", stocks, "2026-01-01", "1d,1w,1mo")]
        if intra:
            runs.append(("wf-intraday", intra, INTRADAY_FROM, "5m,15m,30m,60m"))
        active: dict[str, dict[str, list[str]]] = {}
        learned: dict[str, dict] = {}
        out = self.root / "history" / self.tag
        out.mkdir(parents=True, exist_ok=True)
        for name, syms, start, tfs in runs:
            lst = self.runs / f"{self.tag}-{name}.txt"
            lst.parent.mkdir(parents=True, exist_ok=True)
            lst.write_text("\n".join(syms) + "\n", encoding="utf-8")
            cmd = [sys.executable, "-m", "stock_gplm.app.walk_forward", "--stocks", f"@{lst}", "--from", start,
                   "--timeframes", tfs, "--name", f"cycle/{self.tag}/{name}", "--workers", str(self.workers)]
            t0 = time.monotonic()
            rc = subprocess.run(cmd, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
                                capture_output=True, text=True).returncode
            self.log(f"walk-forward {name}: {len(syms)} stocks, {tfs}, rc={rc}, {(time.monotonic() - t0) / 60:.0f} min")
            if rc:
                raise RuntimeError(f"walk_forward {name} failed (rc={rc})")
            d = self.s.reports_dir / "forward" / "cycle" / self.tag / name
            for s, tfm in json.loads((d / "active.json").read_text(encoding="utf-8")).items():
                active.setdefault(s, {}).update(tfm)
            for s, tfm in json.loads((d / "exits_learned.json").read_text(encoding="utf-8")).get("stocks", {}).items():
                learned.setdefault(s, {}).update(tfm)
            shutil.copy2(d / "WALK_FORWARD.md", out / f"WALK_FORWARD-{name}.md")
            shutil.copy2(d / "unseen.csv", out / f"unseen-{name}.csv")
        (out / "active_walkforward.json").write_text(json.dumps(active, indent=1), encoding="utf-8")
        wf = self.root / "walkforward"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "active.json").write_text(json.dumps(active, indent=1), encoding="utf-8")
        (wf / "tag.txt").write_text(self.tag + "\n", encoding="utf-8")
        shutil.copy2(EXITS, out / "exits.before.json")
        doc = json.loads(EXITS.read_text(encoding="utf-8"))
        for s, tfm in learned.items():
            doc.setdefault("stocks", {}).setdefault(s, {}).update(tfm)
        tmp = EXITS.with_name(".exits.promote.tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, EXITS)
        self.log(f"walk-forward ACTIVE {sum(len(v) for x in active.values() for v in x.values())} combos on "
                 f"{len(active)} stocks; exits.json updated for {len(learned)} stocks")

    def long_check(self, stocks: list[str], insample: Path) -> None:
        base = load(LONG_CFG).reports_dir / "forward" / "long-1d"
        exl = base / "exits-long.json"
        exl.parent.mkdir(parents=True, exist_ok=True)
        if not exl.is_file():
            shutil.copy2(EXITS, exl)
        d = self.batch("long-full", stocks, "2010-01-01", "1d", True, cfg=LONG_CFG, env={"GPLM_EXITS_FILE": str(exl)})
        from .long_compare import build
        oos = base / "oos-frozen2026"
        txt = build(insample, oos, d) if (oos / "all_trades.csv").is_file() else "no out-of-sample run yet\n"
        (self.root / "history" / self.tag / "LONG_COMPARE.md").write_text(txt, encoding="utf-8")
        self.log("long check written (exits-long.json is not promoted automatically)")


# -------------------------------------------------------------------- helpers
def _last_daily(d: Path) -> str | None:
    ends = [f.split("_")[1][:10] for f in os.listdir(d) if f.endswith(".json.gz.done") and "_" in f] if d.is_dir() else []
    return max(ends) if ends else None


async def _fetch_recent(behind: list[tuple[str, str]], today: str, lanes: int = 4) -> int:
    """One get_stock_history call per stock (3 months to today); keeps dates after Shoonya's last daily date.
    Skips stocks whose first new open is >40% from Shoonya's last close (likely split/bonus: wait for Shoonya)."""
    from ..m39.client import NSEMCPClient
    from ..m39.comparison import tool_payload
    from .nse_history import parse_rows, shoonya_closes, write_part
    from .settings import load as _load
    sroot = _load(CFG).daily_root
    q: asyncio.Queue = asyncio.Queue()
    for x in behind:
        q.put_nowait(x)
    done = 0

    async def lane() -> None:
        nonlocal done
        async with NSEMCPClient("bhavcopy", connect_timeout=10, read_timeout=60, retries=3) as cl:
            await cl.connect()
            await cl.list_tools()
            while not q.empty():
                sym, last = q.get_nowait()
                if (date.fromisoformat(today) - date.fromisoformat(last)).days > 85:
                    continue  # more than one call's window behind: left to the collector / nse_history
                try:
                    p = tool_payload(await cl.call_tool("get_stock_history", {"symbol": sym, "months": 3, "endDate": today}))
                except Exception:
                    await asyncio.sleep(2)
                    continue
                rows = [r for r in parse_rows(p, sym) if r[0] > last]
                if not rows:
                    continue
                closes = shoonya_closes(sroot / f"{sym}-EQ")
                ref = closes.get(last)
                if ref and not (0.6 < rows[0][1] / ref < 1.67):
                    continue
                folder = RECENT / f"{sym}-EQ"
                shutil.rmtree(folder, ignore_errors=True)  # rebuilt each time from Shoonya's last date: no overlap
                write_part(folder, rows[0][0], rows[-1][0], [(d, o, h, l, c, v) for d, o, h, l, c, _, v in rows],
                           source="nse-mcp-recent")
                done += 1
                await asyncio.sleep(0.2)

    await asyncio.gather(*(lane() for _ in range(lanes)))
    return done


def _exit_text(ex: dict, stock: str, tf: str, sid: str) -> str:
    e = (ex.get(stock) or {}).get(tf) or {}
    s = (e.get("strategies") or {}).get(sid, e)
    if s.get("rr") is None:
        return "default"
    mx = s.get("max_swing_pct")
    return f"{'swing' if mx is None else f'{mx:.1%}'} / {s['rr']:g}x"


def _sector_md(ss: pd.DataFrame, tag: str) -> str:
    L = [f"# Sector x TF learnings ({tag})", "", "All stocks of a sector pooled per timeframe; same Confidence / RR / "
         "Edge / ACTIVE rules as stock x TF. Use: prior for a stock with few trades of its own.", ""]
    for (sector, tf), g in sorted(ss.groupby(["sector", "timeframe"]), key=lambda x: (x[0][0], x[0][1])):
        a = g[g.status == "ACTIVE"].sort_values("edge", ascending=False)
        if a.empty:
            continue
        L += [f"## {sector} - {tf}", "", "| Strategy | Side | Trades | Win | Avg net | Confidence | RR | Edge |",
              "|---|---|---|---|---|---|---|---|"]
        L += [f"| {r.name} | {r.side} | {r.trades} | {r.win_rate:.0%} | {r.avg_net_pct:+.2f}% | {r.confidence:.0%} | "
              f"{r.rr:.2f} | {r.edge:+.2f} |" for r in a.itertuples()] + [""]
    return "\n".join(L) + "\n"


def _changes(prev: dict, new: dict, sc: pd.DataFrame, tag: str) -> str:
    flat = lambda m: {(s, tf, i) for s, tfs in m.items() for tf, ids in tfs.items() for i in ids}  # noqa: E731
    a, b = flat(prev), flat(new)
    names = dict(zip(sc.strategy_id, sc.name))
    why = {(r.stock, r.timeframe, r.strategy_id): r.why for r in sc.itertuples()}
    L = [f"# Changes in ACTIVE ({tag})", "", f"Before {len(a)}, now {len(b)}: +{len(b - a)} new, -{len(a - b)} dropped.", ""]
    L += ["## New", "", *[f"- {s} {tf} {names.get(i, i)}" for s, tf, i in sorted(b - a)], "",
          "## Dropped", "", *[f"- {s} {tf} {names.get(i, i)}: {why.get((s, tf, i), 'no trades in window')}"
                              for s, tf, i in sorted(a - b)], ""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cadence", choices=["daily", "weekly", "monthly"])
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--stocks", help="comma list instead of the universe (smoke runs)")
    ap.add_argument("--fetch-only", action="store_true", help="only refresh recent daily candles from NSE MCP")
    ap.add_argument("--home", help="state home override for smoke runs (keeps learning/current untouched)")
    a = ap.parse_args(argv)
    c = Cycle(a.cadence, a.workers)
    if a.home:
        c.root = Path(a.home) / "learning"
    lock = c.root / ".cycle.lock"
    c.root.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock.stat().st_mtime
        if age < 20 * 3600:
            c.log(f"another cycle is running (lock {age / 3600:.1f} h old); skipped")
            return 0
        lock.unlink(missing_ok=True)  # stale (killed run)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        if a.fetch_only:
            c.fetch_daily(a.stocks.upper().split(",") if a.stocks else
                          [x.strip() for x in UNIVERSE.read_text(encoding="utf-8").split() if x.strip()])
        else:
            c.run(fetch=not a.skip_fetch, only=a.stocks.upper().split(",") if a.stocks else None)
    except Exception as e:
        c.log(f"FAILED: {type(e).__name__}: {e}")
        raise
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
