"""Rolling month-by-month check of strategy selection settings (owner 2026-11-22: confidence from SMALL recent samples).

For every test month M and every setting: score all trades that EXITED before M starts (optionally only the last
``lookback`` months; recency weighting as in confidence.py), take the ACTIVE stock x TF x strategy list, and record
the trades those combinations actually made in M. Nothing from M or later is used to choose, so the result is an
honest forward record built from many small unseen months. "all" = every trade, no selection (the bar to beat).

Settings compared (overrides on config/strategies/confidence.json "default"; add your own with --settings file.json):
  current      - confidence.json as it is
  pool         - strategy judged on ALL stocks only (universe level), no per-stock filter
  stock-shrink - per-stock, few trades pulled toward the strategy's all-stock rate, cautious confidence >= 50%
  pool+stock   - pool gate AND per-stock shrunk filter (owner's preferred design)
  pool+stock-strict - same, stricter

Usage (owner runs):  python -m stock_gplm.app.rolling_check <forward run dir or all_trades.csv> [--tf 1d]
Writes <run>/rolling/ROLLING.md, summary.csv, monthly.csv.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .confidence import load, score

PRESETS: dict[str, dict[str, Any]] = {
    "current": {},
    "pool": {"min_trades": 0, "min_confidence": 0.0, "min_conf_low": 0.0, "min_edge": -1e9,
             "min_pool_edge": 0.05, "min_pool_trades": 30},
    "stock-shrink": {"prior_source": "strategy", "prior": 5, "min_trades": 3, "min_confidence": 0.0,
                     "min_conf_low": 0.5, "min_edge": 0.0, "min_pool_edge": -1e9, "min_pool_trades": 0},
    "pool+stock": {"prior_source": "strategy", "prior": 5, "min_trades": 3, "min_confidence": 0.0,
                   "min_conf_low": 0.5, "min_edge": 0.0, "min_pool_edge": 0.05, "min_pool_trades": 30},
    "pool+stock-strict": {"prior_source": "strategy", "prior": 5, "min_trades": 3, "min_confidence": 0.0,
                          "min_conf_low": 0.55, "min_edge": 0.1, "min_pool_edge": 0.1, "min_pool_trades": 30},
}
KEY = ["stock", "timeframe", "strategy_id", "side"]


def _stats(net: np.ndarray) -> dict[str, float]:
    if not len(net):
        return {"trades": 0, "win": np.nan, "avg": np.nan, "pf": np.nan, "total": 0.0, "gw": 0.0, "gl": 0.0}
    gw, gl = float(net[net > 0].sum()), float(-net[net <= 0].sum())
    return {"trades": int(len(net)), "win": float((net > 0).mean()), "avg": float(net.mean()),
            "pf": gw / gl if gl > 0 else 99.0, "total": float(net.sum()), "gw": gw, "gl": gl}


def _months_back(month: str, k: int) -> str:
    return str((np.datetime64(month, "M") - np.timedelta64(k, "M")))


def run(trades: pd.DataFrame, settings: dict[str, dict[str, Any]], lookbacks: list[int], n_months: int,
        min_train: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = load()
    t = trades.copy()
    t["sig"] = t["signal"].astype(str)
    t["ex"] = (t["exit"] if "exit" in t.columns else t["signal"]).astype(str)
    t["m"] = t["sig"].str[:7]
    rows = []
    for tf, x in t.groupby("timeframe"):
        months = sorted(x["m"].unique())[-n_months:]
        for M in months:
            start = M + "-01"
            test = x[x["m"] == M]
            done = x[x["ex"] < start]
            if len(done) < min_train:
                continue
            rows.append({"timeframe": tf, "month": M, "setting": "all", "lookback": 0, "active": np.nan,
                         **_stats(test["net_pct"].to_numpy(float))})
            for lb in lookbacks:
                train = done if lb <= 0 else done[done["sig"] >= _months_back(M, lb) + "-01"]
                if len(train) < min_train:
                    continue
                for name, ov in settings.items():
                    doc = {**base, "default": {**(base.get("default") or {}), **ov}, "timeframes": {}}
                    sc = score(train, doc, explain=False)
                    act = sc.loc[sc["status"] == "ACTIVE", KEY]
                    sel = test.merge(act, on=KEY, how="inner")
                    rows.append({"timeframe": tf, "month": M, "setting": name, "lookback": lb, "active": len(act),
                                 **_stats(sel["net_pct"].to_numpy(float))})
            print(f"{time.strftime('%H:%M:%S')} {tf} {M} done", flush=True)
    monthly = pd.DataFrame(rows)
    summ = []
    for (tf, name, lb), g in monthly.groupby(["timeframe", "setting", "lookback"]):
        months = len(g)
        traded = g[g["trades"] > 0]
        n = int(g["trades"].sum())
        tot = float(g["total"].sum())
        wins = float((g["win"].fillna(0) * g["trades"]).sum())
        gl = float(g["gl"].sum())
        summ.append({"timeframe": tf, "setting": name, "lookback": lb, "months": months,
                     "months_traded": len(traded), "months_positive": int((traded["total"] > 0).sum()),
                     "trades": n, "trades_per_month": n / months if months else 0.0,
                     "win": wins / n if n else np.nan, "avg": tot / n if n else np.nan, "total": tot,
                     "pf": float(g["gw"].sum()) / gl if gl > 0 else np.nan,
                     "median_month_pf": float(traded["pf"].median()) if len(traded) else np.nan,
                     "median_active": float(g["active"].median()) if name != "all" else np.nan})
    summary = pd.DataFrame(summ)
    return monthly, summary


def write(out: Path, monthly: pd.DataFrame, summary: pd.DataFrame, source: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(out / "monthly.csv", index=False)
    summary.to_csv(out / "summary.csv", index=False)
    L = [f"# Rolling selection check ({source})", "",
         "Each month is chosen using ONLY trades that closed before it started, then judged on what those choices "
         "actually did that month (costs included). 'all' = every trade, no selection - the bar to beat. "
         "lookback 0 = all earlier history (recent trades weigh more), N = only the last N months.", "",
         "Good setting = most months positive, avg and median monthly PF above 'all', and still enough trades per "
         "month (PF = all trades pooled). Put the winner's numbers into config/strategies/confidence.json \"default\".", ""]
    for tf, g in summary.groupby("timeframe"):
        g = g.sort_values(["avg"], ascending=False)
        L += [f"## {tf}", "", "| Setting | Lookback | Months positive | Trades / month | Win | Avg net | "
              "PF | Median month PF | Total net | Active combos (median) |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in g.itertuples():
            act = "" if np.isnan(r.median_active) else f"{r.median_active:.0f}"
            L.append(f"| {r.setting} | {r.lookback or 'all'} | {r.months_positive} of {r.months_traded} | "
                     f"{r.trades_per_month:.0f} | {r.win:.0%} | {r.avg:+.2f}% | {r.pf:.2f} | {r.median_month_pf:.2f} | "
                     f"{r.total:+.0f}% | {act} |" if r.trades else
                     f"| {r.setting} | {r.lookback or 'all'} | 0 of {r.months} | 0 | - | - | - | - | - | {act} |")
        L.append("")
    p = out / "ROLLING.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="forward_batch output dir (with all_trades.csv) or a trades csv")
    ap.add_argument("--tf", help="comma list of timeframes (default: all in the file)")
    ap.add_argument("--months", type=int, default=24, help="number of most recent test months (default 24)")
    ap.add_argument("--lookbacks", default="0,6", help="comma list of training months, 0 = all history (default 0,6)")
    ap.add_argument("--settings", help="JSON file {name: {confidence overrides}} added to / replacing presets")
    ap.add_argument("--only", help="comma list of setting names to run")
    ap.add_argument("--min-train", type=int, default=200, help="skip a month if fewer earlier closed trades")
    ap.add_argument("--out", help="output dir (default <run>/rolling)")
    a = ap.parse_args(argv)
    src = Path(a.run)
    csv = src / "all_trades.csv" if src.is_dir() else src
    t = pd.read_csv(csv, usecols=lambda c: c in {"stock", "strategy_id", "name", "timeframe", "side", "signal",
                                                  "exit", "net_pct"})
    if a.tf:
        t = t[t["timeframe"].isin(a.tf.split(","))]
    settings = dict(PRESETS)
    if a.settings:
        settings.update(json.loads(Path(a.settings).read_text(encoding="utf-8")))
    if a.only:
        settings = {k: v for k, v in settings.items() if k in a.only.split(",")}
    lbs = [int(x) for x in a.lookbacks.split(",")]
    print(f"{len(t):,} trades, timeframes {sorted(t['timeframe'].unique())}, settings {list(settings)}, "
          f"lookbacks {lbs}", flush=True)
    monthly, summary = run(t, settings, lbs, a.months, a.min_train)
    print(write(Path(a.out) if a.out else csv.parent / "rolling", monthly, summary, csv.parent.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
