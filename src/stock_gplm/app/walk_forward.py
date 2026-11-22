"""Walk-forward judgement (owner 2026-11-21): learn on the past, judge only on months the learning never saw.

    python -m stock_gplm.app.walk_forward --stocks @deploy/oracle/live/universe.txt --name wf-2026 \
        [--from 2026-01-01] [--first-test 2026-04-01] [--timeframes 1d,1w,1mo] [--workers 20]

Per stock x timeframe, for every month M from --first-test to the latest (expanding window):
  1. learn exits on everything before M (stop / target grid, best recency-weighted expectancy; per strategy with
     >= 8 trades, else all strategies of the stock x TF with >= 20, else the strategy's default exit);
  2. select at time M: strategies whose learning-period trades pass the ACTIVE rule (recency-weighted);
  3. trade month M with those exits - entries inside M, exits may run past it (stop / target / 15:15 intraday),
     never using any candle after M for the choices above.
Unseen trades of every strategy are kept (column `selected` says whether it was picked at that time).
ACTIVE now = the stock x TF x strategy's unseen record passes the rule; its live exit = learned on all data.
Rule (owner 2026-11-21, recency-weighted, recent trades weigh more): >= 10 trades, weighted average net > 0,
weighted profit factor > 1.2, Edge > 0 (confidence.py).
Outputs <home>/reports/forward/<name>/: <SYM>/wf_trades.csv + exits.json per stock; WALK_FORWARD.md (headline:
the trades of strategies selected at the time = what live trading would have done), active.json, unseen.csv,
exits_learned.json (exits.json format, for promotion). Resumable per stock; CPU only.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
MIN_TRADES, MIN_PF = 10, 1.2


# ------------------------------------------------------------------ rule
def rule_stats(net: np.ndarray, w: np.ndarray) -> dict[str, float]:
    """Recency-weighted: trades, avg net, profit factor, win rate, edge (= win x avg win / avg loss - loss rate)."""
    if not len(net) or w.sum() <= 0:
        return {"trades": 0, "avg": 0.0, "pf": 0.0, "win": 0.0, "edge": -1.0}
    win = net > 0
    gw = float((w * net)[win].sum())
    gl = float(-(w * net)[~win].sum())
    wr = float(w[win].sum() / w.sum())
    aw = gw / w[win].sum() if win.any() else 0.0
    al = gl / w[~win].sum() if (~win).any() else 0.0
    edge = wr * (aw / al) - (1 - wr) if al > 0 else (1.0 if aw > 0 else -1.0)
    return {"trades": int(len(net)), "avg": float((w * net).sum() / w.sum()), "pf": gw / gl if gl > 0 else 99.0,
            "win": wr, "edge": float(edge)}


def passes(st: dict[str, float]) -> bool:
    return st["trades"] >= MIN_TRADES and st["avg"] > 0 and st["pf"] > MIN_PF and st["edge"] > 0


# ------------------------------------------------------------------ one stock
def _start(label: str) -> str:
    """First day of a period label: 2026-04 (month), 2026-Q2 (quarter), 2026 (year)."""
    if len(label) == 4:
        return f"{label}-01-01"
    if "-Q" in label:
        y, q = label.split("-Q")
        return f"{y}-{3 * int(q) - 2:02d}-01"
    return f"{label}-01"


def periods(sessions, first_test: str, step: str) -> list[str]:
    """Test period labels from ``first_test`` on (owner 2026-11-21: monthly for 2026, yearly for long history)."""
    out = []
    for d in sessions:
        d = str(d)
        lab = d[:4] if step == "year" else (f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}" if step == "quarter" else d[:7])
        if _start(lab) >= first_test[:10] and (not out or out[-1] != lab):
            out.append(lab)
    return out


def run(stock: str, start: str, first_test: str, tfs: list[str], config: str, name: str, step: str = "month") -> Path:
    from . import strategy_dsl as dsl
    from .context import App
    from .forward_test import exit_grid, pick_exit, recency_weights, _fmt
    from .ingest import ingest_many
    from .lanes import build_lane, research_bars
    from .research import _ctx_for
    from .strategy_engine import Exit, simulate, swing_settings
    from .strategy_library import StrategyLibrary

    app = App(config, None, quiet=True)
    out = app.s.reports_dir / "forward" / name / stock
    out.mkdir(parents=True, exist_ok=True)
    lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], out / "_lib").load(record=False)
    strategies = [s for s in lib.strategies.values() if s.doc.get("enabled", True) and not s.blocked]
    data = next(iter(ingest_many(app.s, app.db, [stock], use_minute=True).values()))
    bars = research_bars(data)
    refs = {}  # official Nifty 50 for bench() (owner 2026-11-22: index history from 2024 via NSE MCP)
    try:
        idx = next(iter(ingest_many(app.s, app.db, ["IDX_NIFTY_50-EQ"], use_minute=False).values()), None)
        if idx is not None:
            refs["IDX_NIFTY_50-EQ"] = idx
    except Exception:  # noqa: BLE001
        pass
    costs = app.s.section("costs")
    rows: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    sym = stock.upper().removesuffix("-EQ")
    for tf in tfs:
        if tf not in bars or not len(bars[tf]):
            continue
        lane = build_lane(data, tf, costs, bars=bars)
        m = lane.market
        ctx = _ctx_for(bars[tf], tf, lane, bars, refs, None)
        ses = np.asarray(m.sessions).astype(str)
        n = len(m)
        a0 = int(np.searchsorted(ses, start))
        sigs = []
        for s in strategies:
            if tf not in s.timeframes:
                continue
            try:
                mask = dsl.signal(s.entry_node, ctx, s.defaults())
                xm = dsl.signal(s.exit_node, ctx, s.defaults()) if s.exit_node is not None else None
            except (dsl.MissingData, dsl.DSLError):
                continue
            sigs.append((s, mask, xm))
        if not sigs or a0 >= n:
            continue
        tests = periods(ses[a0:], first_test, step)
        ids = [s.id for s, _, _ in sigs]
        for k, mo in enumerate(tests + [None]):
            M = int(np.searchsorted(ses, _start(mo))) if mo else n
            if M - a0 < 20:
                continue
            per = exit_grid(stock, m, sigs, (a0, M), lane.costs.buy, lane.costs.sell)
            tf_best = pick_exit(per, ids, 20)
            chosen: dict[str, tuple] = {}
            for s, _, _ in sigs:
                r = pick_exit(per, [s.id], 8) or tf_best
                if r:
                    chosen[s.id] = (r[2], r[3])
            if mo is None:  # live exits: learned on everything
                entry: dict[str, Any] = {}
                if tf_best:
                    entry = {"max_swing_pct": tf_best[2], "rr": tf_best[3],
                             "learned": {"from": start, "to": "latest", "trades": tf_best[1], "method": "walk-forward"}}
                entry["strategies"] = {i: {"max_swing_pct": v[0], "rr": v[1]} for i, v in chosen.items()}
                final[tf] = entry
                break
            nxt = int(np.searchsorted(ses, _start(tests[k + 1]))) if k + 1 < len(tests) else n
            base = swing_settings(stock, "*")
            for s, mask, xm in sigs:
                d = dict(s.doc["exit"])
                if d.get("swing_stop") is not None and s.id in chosen:
                    d["swing_stop"] = {**base, "max_swing_pct": chosen[s.id][0], "rr": chosen[s.id][1]}
                ex = Exit.of(d)
                short = s.direction == "short"
                tr_train = simulate(m, mask, (a0, M), ex, lane.costs.buy, lane.costs.sell, short, xm)
                st = rule_stats(np.array([x["net_return"] for x in tr_train]),
                                recency_weights(m, [x["t"] for x in tr_train], M))
                sel = passes(st)
                fired = np.nonzero(mask[M:nxt] & ~m.no_signal[M:nxt])[0] + M
                for t in simulate(m, mask, (M, n), ex, lane.costs.buy, lane.costs.sell, short, xm, fired=fired):
                    rows.append({"stock": sym, "timeframe": tf, "strategy_id": s.id, "name": s.doc.get("name", s.id),
                                 "side": s.direction, "month": mo, "selected": sel, "signal": m.label(t["t"]),
                                 "exit": m.label(t["exit_index"]), "exit_reason": t["exit_reason"],
                                 "net_pct": round(100 * t["net_return"], 3), "bars_held": t["bars_held"],
                                 "exit_setting": _fmt(*chosen[s.id]) if s.id in chosen else "default",
                                 "train_trades": st["trades"], "train_avg_pct": round(100 * st["avg"], 3)})
    cols = ["stock", "timeframe", "strategy_id", "name", "side", "month", "selected", "signal", "exit", "exit_reason",
            "net_pct", "bars_held", "exit_setting", "train_trades", "train_avg_pct"]
    with (out / "wf_trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    (out / "exits.json").write_text(json.dumps({sym: final}, indent=1), encoding="utf-8")
    (out / "done").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    return out


# ------------------------------------------------------------------ report
def _weights_by_day(days: pd.Series) -> np.ndarray:
    """Recency weights for a report: age in distinct signal days before the latest one (same decay)."""
    d = days.astype(str).str[:10]
    order = {x: i for i, x in enumerate(sorted(d.unique()))}
    age = len(order) - 1 - d.map(order).to_numpy()
    return (1.0 + age / 100.0) ** -2


def report(root: Path, stocks: list[str], title: str) -> Path:
    frames = [pd.read_csv(root / s / "wf_trades.csv") for s in stocks
              if (root / s / "wf_trades.csv").is_file() and (root / s / "wf_trades.csv").stat().st_size > 200]
    if not frames:
        (root / "WALK_FORWARD.md").write_text("no trades\n", encoding="utf-8")
        return root / "WALK_FORWARD.md"
    t = pd.concat(frames, ignore_index=True)
    t.to_csv(root / "all_unseen_trades.csv", index=False)
    t["w"] = _weights_by_day(t["signal"])

    def stats(x: pd.DataFrame) -> dict[str, float]:
        return rule_stats(x["net_pct"].to_numpy() / 100, x["w"].to_numpy())

    def line(label: str, x: pd.DataFrame) -> str:
        if not len(x):
            return f"| {label} | 0 | - | - | - | - |"
        n = x["net_pct"]
        loss = -n[n <= 0].sum()
        return (f"| {label} | {len(x):,} | {(n > 0).mean():.1%} | {n.mean():+.2f}% | "
                f"{(n[n > 0].sum() / loss if loss > 0 else 99):.2f} | {100 * stats(x)['avg']:+.2f}% |")

    sel = t[t["selected"]]
    combos = []
    for (s, tf, sid), g in t.groupby(["stock", "timeframe", "strategy_id"]):
        st = stats(g)
        combos.append({"stock": s, "timeframe": tf, "strategy_id": sid, "name": g["name"].iloc[0],
                       "side": g["side"].iloc[0], "unseen_trades": st["trades"], "recent_avg_pct": round(100 * st["avg"], 3),
                       "recent_pf": round(st["pf"], 2), "recent_win": round(st["win"], 3), "edge": round(st["edge"], 3),
                       "plain_avg_pct": round(g["net_pct"].mean(), 3), "plain_win": round((g["net_pct"] > 0).mean(), 3),
                       "last_month": g["month"].max(), "active": passes(st)})
    c = pd.DataFrame(combos)
    c.to_csv(root / "unseen.csv", index=False)
    act = c[c["active"]].sort_values("edge", ascending=False)
    amap: dict[str, dict[str, list[str]]] = {}
    for r in act.itertuples():
        amap.setdefault(r.stock, {}).setdefault(r.timeframe, []).append(r.strategy_id)
    (root / "active.json").write_text(json.dumps(amap, indent=1), encoding="utf-8")
    exits: dict[str, Any] = {}
    for s in stocks:
        f = root / s / "exits.json"
        if f.is_file():
            exits.update(json.loads(f.read_text(encoding="utf-8")))
    (root / "exits_learned.json").write_text(json.dumps({"stocks": exits}, indent=1), encoding="utf-8")
    head = ["| Set | Trades | Win | Avg net | PF | Recent-weighted avg |", "|---|---|---|---|---|---|"]
    L = [f"# Walk-forward {title}", "",
         "Every trade below is UNSEEN: its exits and selection were learned only on earlier months. Recent trades "
         "weigh more in the rule (half weight after ~40 sessions).", "", "## Headline", "", *head,
         line("Selected at the time (what live trading would have done)", sel),
         line("All strategies, unseen", t), "", "## Selected trades per month", "", *head,
         *[line(mo, sel[sel["month"] == mo]) for mo in sorted(t["month"].unique())], "",
         "## Selected trades per timeframe", "", *head,
         *[line(tf, sel[sel["timeframe"] == tf]) for tf in sorted(t["timeframe"].unique())], "",
         f"## ACTIVE now: {len(act)} stock x TF x strategy on {act['stock'].nunique() if len(act) else 0} stocks", "",
         f"Rule: >= {MIN_TRADES} unseen trades, recency-weighted avg net > 0, PF > {MIN_PF}, Edge > 0.", "",
         "| Stock | TF | Strategy | Side | Unseen trades | Recent avg | Recent PF | Recent win | Plain avg | Edge |",
         "|---|---|---|---|---|---|---|---|---|---|",
         *[f"| {r.stock} | {r.timeframe} | {r.name} | {r.side} | {r.unseen_trades} | {r.recent_avg_pct:+.2f}% | "
           f"{r.recent_pf:.2f} | {r.recent_win:.0%} | {r.plain_avg_pct:+.2f}% | {r.edge:+.2f} |"
           for r in act.head(300).itertuples()], ""]
    (root / "WALK_FORWARD.md").write_text("\n".join(L), encoding="utf-8")
    return root / "WALK_FORWARD.md"


# ------------------------------------------------------------------ batch
def read_stocks(arg: str) -> list[str]:
    txt = Path(arg[1:]).read_text(encoding="utf-8") if arg.startswith("@") else arg
    raw = [x for ln in txt.splitlines() for x in ln.split("#", 1)[0].replace(",", " ").split()]
    return [s.upper().removesuffix("-EQ") for s in dict.fromkeys(raw)
            if s.upper() not in {"NIFTY", "BANKNIFTY", "INDIAVIX", "FINNIFTY"}]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stocks", required=True, help="comma list or @file")
    ap.add_argument("--one", help=argparse.SUPPRESS)  # internal: run one stock in this process
    ap.add_argument("--from", dest="start", default="2026-01-01")
    ap.add_argument("--first-test", default="2026-04-01")
    ap.add_argument("--timeframes", default="1d,1w,1mo")
    ap.add_argument("--config", default=str(REPO / "config" / "app" / "gplm.windows.json"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--step", choices=["month", "quarter", "year"], default="month",
                    help="length of each unseen period (expanding window); year for long history")
    a = ap.parse_args(argv)
    tfs = a.timeframes.split(",")
    if a.one:
        run(a.one, a.start, a.first_test, tfs, a.config, a.name, a.step)
        return 0
    from .settings import load
    stocks = read_stocks(a.stocks)
    root = load(Path(a.config)).reports_dir / "forward" / a.name
    root.mkdir(parents=True, exist_ok=True)
    log = root / "batch.log"
    todo = [s for s in stocks if a.force or not (root / s / "done").is_file()]

    def one(sym: str) -> int:
        cmd = [sys.executable, "-m", "stock_gplm.app.walk_forward", "--stocks", sym, "--one", sym, "--from", a.start,
               "--first-test", a.first_test, "--timeframes", a.timeframes, "--config", a.config, "--name", a.name,
               "--step", a.step]
        t0 = time.monotonic()
        (root / sym).mkdir(parents=True, exist_ok=True)
        with (root / sym / "run.log").open("w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO).returncode
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {sym} rc={rc} {time.monotonic() - t0:.0f}s\n")
        return rc

    with log.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} start: {len(stocks)} stocks, {len(todo)} to run, {tfs}\n")
    with ThreadPoolExecutor(a.workers) as ex:
        failed = [s for s, rc in zip(todo, ex.map(one, todo)) if rc]
    for s in failed:
        one(s)
    out = report(root, stocks, f"{a.start} .. latest, unseen {a.step}s from {a.first_test}")
    (root / "batch.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
