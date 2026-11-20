"""Setup replay (PAPER/research): re-run the live lab's setup logic on PAST sessions and score the outcomes.

For every stock x 5m/15m/30m/60m bar in [--start, --end] it builds the same setups the live lab would have
shown (rising edges of long library strategies, median ATR stop/target, valid until max-hold or 15:29), then
checks on the 1-minute bars what actually happened: STOP / TARGET / EXPIRED / EOD, net of intraday costs.

Confidence is recorded as it would have been known at that moment:
  * live    - the lab's live-replay rule (same strategies, last --live-sessions sessions on that stock)
  * backtest- rule C from research evidence (evidence.py; validation trades <= development_end only),
              applied at summary time so new evidence runs only need a cheap rerun of this command.

Checkpointed per stock (`<out>/symbols/<SYM>.csv`); rerunning the same --name skips finished stocks.
Usage (PowerShell, PYTHONPATH=src):
  python -m stock_gplm.app.setup_replay --start 2026-08-14 --end 2026-11-19 --name test-period
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import strategy_dsl as dsl
from .live_lab import INDICES, REPO, TFS, build_frame, confidence, load_strats, past_trades

# intraday round trip from config costs.intraday (buy 0.7 + sell 2.9 statutory, 3+3 brokerage, 5+5 slippage bps)
COST_PCT = (0.7 + 2.9 + 3.0 * 2 + 5.0 * 2) / 100

_STRATS: list[dict[str, Any]] | None = None


def read_minute(roots: list[Path], sym: str, sessions: set[str]) -> list[dict[str, Any]]:
    """Raw Shoonya minute partitions (first root wins per session) -> [{t,o,h,l,c,v}], 09:15-15:29."""
    seen: set[str] = set()
    bars: list[dict[str, Any]] = []
    for root in roots:
        d = Path(root) / f"{sym}-EQ"
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json.gz")):
            day = p.name[:10]
            if day in seen or day not in sessions or not Path(f"{p}.done").exists():
                continue
            seen.add(day)
            try:
                rows = json.load(gzip.open(p))
            except (OSError, ValueError):
                continue
            for r in rows if isinstance(rows, list) else []:
                if r.get("stat") != "Ok":
                    continue
                t = datetime.strptime(r["time"], "%d-%m-%Y %H:%M:%S")
                hm = t.hour * 60 + t.minute
                if not (555 <= hm <= 929):
                    continue
                bars.append({"t": t.strftime("%Y-%m-%dT%H:%M"), "o": float(r["into"]), "h": float(r["inth"]),
                             "l": float(r["intl"]), "c": float(r["intc"]), "v": float(r.get("intv") or 0)})
    bars.sort(key=lambda b: b["t"])
    return bars


def available_sessions(roots: list[Path], sym: str) -> list[str]:
    out: set[str] = set()
    for root in roots:
        d = Path(root) / f"{sym}-EQ"
        if d.is_dir():
            out |= {f[:10] for f in os.listdir(d) if f.endswith(".json.gz.done") and f[10] == "."}
    return sorted(out)


def _outcome(days: dict[str, tuple], closed: str, until: str, entry: float, stop: float | None,
             tgt: float | None) -> tuple[str, str | None, float | None]:
    """Same order as the live lab: stop checked before target on each 1m bar; time exit at `until`."""
    if closed[:10] not in days:
        return "NO_DATA", None, None
    T, Lo, Hi, Cl = days[closed[:10]]
    j = int(np.searchsorted(T, closed))
    for t, lo, hi, c in zip(T[j:], Lo[j:], Hi[j:], Cl[j:]):
        if stop and lo <= stop:
            return "STOP", t[11:16], stop
        if tgt and hi >= tgt:
            return "TARGET", t[11:16], tgt
        if t >= until:
            return ("EOD" if t.endswith("15:29") else "EXPIRED"), t[11:16], c
    return "NO_DATA", None, None


def replay_symbol(sym: str, roots: list[Path], start: str, end: str, warmup: int, live_sessions: int,
                  out: Path, libs: list[Path]) -> tuple[str, int, str]:
    global _STRATS
    if _STRATS is None:
        _STRATS = load_strats(libs, out.parent / "_scratch")
    sess = available_sessions(roots, sym)
    test = [s for s in sess if start <= s <= end]
    if not test:
        return sym, 0, "no sessions in range"
    first = sess.index(test[0])
    use = set(sess[max(0, first - warmup): sess.index(test[-1]) + 1])
    bars = read_minute(roots, sym, use)
    if not bars:
        return sym, 0, "no bars"
    m1 = pd.DataFrame(bars)
    days = {d: (g["t"].to_numpy(str), g["l"].to_numpy(float), g["h"].to_numpy(float), g["c"].to_numpy(float))
            for d, g in m1.groupby(m1["t"].str[:10])}
    rows: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []  # every strategy's own alert (own levels/exit): scorecard test-period stats
    for tf, k in TFS.items():
        f = build_frame(bars, k, "9999-12-31")
        if len(f) < 30:
            continue
        ctx = dsl.Context.from_frame(f, tf, True)
        O, H, L, C = (f[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        pc = np.concatenate([[C[0]], C[:-1]])
        tr = np.maximum(H - L, np.maximum(abs(H - pc), abs(L - pc)))
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        longs: list[tuple[dict[str, Any], np.ndarray]] = []
        bear = np.zeros(len(f), int)
        applicable = 0
        for s in _STRATS:
            if tf not in s["tfs"] or not s["apply"](sym, tf):
                continue
            try:
                m = np.asarray(dsl._bool(ctx, dsl.evaluate(s["node"], ctx)), bool)
            except Exception:
                continue
            if len(m) != len(f):
                continue
            if s["dir"] == "short":
                bear += m
            else:
                applicable += 1
                longs.append((s, m))
        sess_arr = f["session"].to_numpy()
        sess_list = sorted(set(sess_arr))
        busy: dict[str, str] = {}  # strategy -> exit minute of its open alert (live lab: no re-alert while open)
        for i in np.flatnonzero((sess_arr >= start) & (sess_arr <= end)):
            if i < 1 or not np.isfinite(atr[i]):
                continue
            label = f["label"].iloc[i]
            closed_dt = datetime.fromisoformat(label.replace(" ", "T")) + timedelta(minutes=k)
            closed = closed_dt.strftime("%Y-%m-%dT%H:%M")
            added = [(s, m) for s, m in longs if m[i] and not m[i - 1] and busy.get(s["id"], "") < closed]
            if not added:
                continue
            e, a = C[i], atr[i]
            for s, _ in added:  # each strategy's own alert: own levels, exits like the live lab
                ua = min(closed_dt + timedelta(minutes=s["max_hold"] * k), closed_dt.replace(hour=15, minute=29))
                w_, at_, px_ = _outcome(days, closed, ua.strftime("%Y-%m-%dT%H:%M"), e,
                                        e - s["stop_atr"] * a if s["stop_atr"] else None,
                                        e + s["target_atr"] * a if s["target_atr"] else None)
                busy[s["id"]] = f"{closed[:11]}{at_}" if at_ else f"{closed[:11]}23:59"
                if px_:
                    alerts.append({"date": sess_arr[i], "symbol": sym, "tf": tf, "bar": label, "strategy": s["id"],
                                   "outcome": w_, "net_pct": round((px_ / e - 1) * 100 - COST_PCT, 3)})
            stops = [e - s["stop_atr"] * a for s, _ in added if s["stop_atr"]]
            tgts = [e + s["target_atr"] * a for s, _ in added if s["target_atr"]]
            stop = float(np.median(stops)) if stops else None
            tgt = float(np.median(tgts)) if tgts else None
            hold = int(np.median([s["max_hold"] * k for s, _ in added]))
            until_dt = min(closed_dt + timedelta(minutes=hold), closed_dt.replace(hour=15, minute=29))
            until = until_dt.strftime("%Y-%m-%dT%H:%M")
            why, at, px = _outcome(days, closed, until, e, stop, tgt)
            # live-lab confidence: same strategies, last `live_sessions` sessions incl. the signal day, up to bar i
            si = sess_list.index(sess_arr[i])
            lo = int(np.searchsorted(sess_arr, sess_list[max(0, si - live_sessions + 1)]))
            trades: list[float] = []
            for s, m in added:
                trades += past_trades(m[lo:i + 1], O[lo:i + 1], H[lo:i + 1], L[lo:i + 1], C[lo:i + 1],
                                      atr[lo:i + 1], s["stop_atr"], s["target_atr"], s["max_hold"])
            lc = confidence(trades, conflict=bool(bear[i]))
            rrs = [s["target_atr"] / s["stop_atr"] for s, _ in added if s["stop_atr"] and s["target_atr"]]
            gross = (px / e - 1) * 100 if px else None
            rows.append({"date": sess_arr[i], "symbol": sym, "tf": tf, "bar": label, "n_agree": len(added),
                         "applicable": applicable, "strategies": ";".join(s["id"] for s, _ in added),
                         "rr": round(min(rrs), 2) if rrs else None, "bear": int(bear[i]),
                         "entry": round(e, 2), "stop": round(stop, 2) if stop else None,
                         "target": round(tgt, 2) if tgt else None, "until": until[11:], "outcome": why,
                         "exit_at": at, "exit_px": round(px, 2) if px else None,
                         "gross_pct": round(gross, 3) if gross is not None else None,
                         "net_pct": round(gross - COST_PCT, 3) if gross is not None else None,
                         "live_label": lc["label"], "live_n": lc["n"], "live_p": lc["p"], "live_avg": lc["avg"]})
    df = pd.DataFrame(rows)
    (out.parent / "alerts").mkdir(exist_ok=True)
    pd.DataFrame(alerts).to_csv(out.parent / "alerts" / f"{sym}.csv", index=False)  # before the checkpoint file
    tmp = out / f"{sym}.csv.tmp"
    df.to_csv(tmp, index=False)
    tmp.replace(out / f"{sym}.csv")
    return sym, len(df), "ok"


def _table(d: pd.DataFrame, by: str) -> pd.DataFrame:
    g = d.groupby(by, dropna=False)
    t = pd.DataFrame({
        "setups": g.size(),
        "target_hit_%": g["outcome"].apply(lambda x: (x == "TARGET").mean() * 100),
        "stop_hit_%": g["outcome"].apply(lambda x: (x == "STOP").mean() * 100),
        "time_exit_%": g["outcome"].apply(lambda x: x.isin(["EXPIRED", "EOD"]).mean() * 100),
        "net_win_%": g["net_pct"].apply(lambda x: (x > 0).mean() * 100),
        "avg_net_%": g["net_pct"].mean(), "median_net_%": g["net_pct"].median(), "sum_net_%": g["net_pct"].sum(),
    })
    return t.round(2)


def summarize(out: Path, research_root: Path, runs: list[str], start: str) -> str:
    from .evidence import Evidence
    files = sorted((out / "symbols").glob("*.csv"))
    frames = [pd.read_csv(f) for f in files if f.stat().st_size > 5]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return "no setups"
    d = pd.concat(frames, ignore_index=True)
    d = d[d["outcome"] != "NO_DATA"].copy()
    ev = Evidence(research_root, runs)
    ev.refresh()
    bt = [ev.lookup(str(r.strategies).split(";"), r.symbol, r.tf, r.rr if r.rr == r.rr else None, bool(r.bear))
          for r in d.itertuples()]
    d["bt_label"] = [(b or {}).get("label", "NO_BACKTEST") for b in bt]
    d["bt_basis"] = [(b or {}).get("basis", "-") for b in bt]
    d["bt_n"] = [(b or {}).get("n") for b in bt]
    d["agree_bucket"] = pd.cut(d["n_agree"], [0, 1, 4, 9, 10_000], labels=["1", "2-4", "5-9", "10+"])
    d.to_csv(out / "setups_scored.csv", index=False)
    lines = [f"# Setup replay: {d['date'].min()} .. {d['date'].max()} ({d['date'].nunique()} sessions, "
             f"{d['symbol'].nunique()} stocks, {len(d)} setups)",
             f"Net = after ~{COST_PCT:.3f}% intraday round-trip costs. PAPER/research only - not advice.",
             f"Backtest evidence runs available: {sorted(ev.trades['run'].unique().tolist()) or 'none'}"
             + ("" if start > "2026-08-13" else "  WARNING: start <= development_end; backtest confidence "
                "overlaps its own training window"), ""]
    for title, by in (("Backtest confidence (rule C)", ["bt_label"]),
                      ("Live-lab confidence (what the page showed)", ["live_label"]),
                      ("Timeframe", ["tf"]), ("Strategies agreeing", ["agree_bucket"]),
                      ("Timeframe x backtest confidence", ["tf", "bt_label"])):
        lines += [f"## {title}", "", "```", _table(d, by).to_string(), "```", ""]
    txt = "\n".join(lines)
    (out / "summary.md").write_text(txt, encoding="utf-8")
    return txt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-08-14")
    ap.add_argument("--end", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--name", default="test-period")
    ap.add_argument("--stocks", default=None, help="comma list; default = live watchlist")
    ap.add_argument("--warmup", type=int, default=10, help="sessions loaded before --start for indicators")
    ap.add_argument("--live-sessions", type=int, default=8, help="sessions the live-lab confidence looks back")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--evidence-run", action="append", default=None)
    ap.add_argument("--force", action="store_true", help="redo stocks already finished")
    a = ap.parse_args()
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    roots = [Path(r) for r in st.minute_roots]
    if a.stocks:
        syms = [s.strip().upper() for s in a.stocks.replace(" ", ",").split(",") if s.strip()]
    else:
        wl = (REPO / "deploy" / "oracle" / "live" / "watchlist.txt").read_text().splitlines()
        syms = [s for ln in wl for s in ln.split("#", 1)[0].split()]
    syms = [s for s in syms if s not in INDICES]
    out = REPO / "var" / "setup-replay" / a.name
    sym_dir = out / "symbols"
    sym_dir.mkdir(parents=True, exist_ok=True)
    todo = [s for s in syms if a.force or not (sym_dir / f"{s}.csv").exists()]
    # repo library + ML-discovered strategies (research-matrix saves them under <home>/strategy-library)
    homes = [Path(st.home), Path(st.home).parent / "state-intraday", Path(st.home).parent / "state-wide"]
    libs = [REPO / "config" / "strategies" / "library"] + [p for h in homes if (p := h / "strategy-library").is_dir()]
    print(f"setup replay {a.start}..{a.end}: {len(syms)} stocks ({len(syms) - len(todo)} already done) -> {out}")
    if todo:
        with ProcessPoolExecutor(a.workers) as pool:
            futs = [pool.submit(replay_symbol, s, roots, a.start, a.end, a.warmup, a.live_sessions, sym_dir, libs)
                    for s in todo]
            for n, fu in enumerate(as_completed(futs), 1):
                try:
                    s, cnt, msg = fu.result()
                    print(f"  [{n}/{len(todo)}] {s}: {cnt} setups ({msg})", flush=True)
                except Exception as exc:  # one bad stock never stops the run; it is retried next time
                    print(f"  [{n}/{len(todo)}] FAILED: {exc}", file=sys.stderr, flush=True)
    print(summarize(out, Path(st.reports_dir) / "research", a.evidence_run or ["evidence-intraday", "evidence-daily"],
                    a.start))


if __name__ == "__main__":
    main()
