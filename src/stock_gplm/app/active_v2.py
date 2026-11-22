"""Stricter ACTIVE rule (v2), report only - re-judges existing walk-forward unseen trades (owner 2026-11-22).

    python -m stock_gplm.app.active_v2 <walk-forward run folder> [more folders]

Why: the v1 rule (>= 10 unseen trades, recency-weighted avg > 0, PF > 1.2, Edge > 0) let a handful of trades
decide (PF 746, PF 30 from 10 trades) because recency weights concentrate on the newest trades.
v2 = v1 AND effective trades n_eff = (sum w)^2 / sum w^2 >= 8 AND profitable in >= 2 distinct periods
(months for 2026 runs, years for long runs). PF is capped at 5 for ranking.

Test (causal, nothing rerun): for every test period P, a stock x TF x strategy is picked when its unseen trades
from periods BEFORE P pass the rule (weights by age in distinct trading days before P, same decay as
walk_forward). Its trades inside P are then counted. v1 and v2 are compared side by side, together with the
walk-forward's own "selected at the time" column and "all strategies".
Writes <run>/ACTIVE_V2.md and <run>/active_v2.json (ACTIVE now under v2).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DECAY = 100.0  # walk_forward / forward_test DECAY_SESSIONS
MIN_TRADES, MIN_PF, MIN_NEFF, MIN_POS_PERIODS, PF_CAP = 10, 1.2, 8.0, 2, 5.0
KEY = ["stock", "timeframe", "strategy_id"]


def _start(label: str) -> str:
    label = str(label)
    if len(label) == 4:
        return f"{label}-01-01"
    if "-Q" in label:
        y, q = label.split("-Q")
        return f"{y}-{3 * int(q) - 2:02d}-01"
    return f"{label}-01"


def judge(hist: pd.DataFrame, upto: str, days: np.ndarray) -> pd.DataFrame:
    """Rule stats per combo from trades in ``hist`` (all before ``upto``), recency-weighted to ``upto``."""
    if not len(hist):
        return pd.DataFrame(columns=[*KEY, "v1", "v2"])
    rank = np.searchsorted(days, hist["day"].to_numpy())
    age = np.searchsorted(days, upto) - rank
    w = (1.0 + np.maximum(age, 0) / DECAY) ** -2
    net = hist["net"].to_numpy()
    win = net > 0
    h = hist[KEY].copy()
    h["w"], h["w2"], h["wn"] = w, w * w, w * net
    h["gw"], h["gl"] = np.where(win, w * net, 0.0), np.where(~win, -w * net, 0.0)
    h["ww"], h["wl"] = np.where(win, w, 0.0), np.where(~win, w, 0.0)
    h["n"] = 1
    g = h.groupby(KEY, sort=False).sum()
    pos = (hist.assign(net=net).groupby([*KEY, "period"], sort=False)["net"].sum() > 0).groupby(KEY).sum()
    g["pos_periods"] = pos.reindex(g.index).fillna(0).to_numpy()
    g["avg"] = g["wn"] / g["w"]
    g["pf"] = np.where(g["gl"] > 0, g["gw"] / g["gl"].where(g["gl"] > 0, 1), 99.0)
    g["win"] = g["ww"] / g["w"]
    aw = g["gw"] / g["ww"].where(g["ww"] > 0, np.nan)
    al = g["gl"] / g["wl"].where(g["wl"] > 0, np.nan)
    g["edge"] = np.where(al > 0, g["win"] * (aw / al) - (1 - g["win"]), np.where(g["gw"] > 0, 1.0, -1.0))
    g["edge"] = g["edge"].fillna(-1.0)
    g["n_eff"] = g["w"] ** 2 / g["w2"]
    g["v1"] = (g["n"] >= MIN_TRADES) & (g["avg"] > 0) & (g["pf"] > MIN_PF) & (g["edge"] > 0)
    g["v2"] = g["v1"] & (g["n_eff"] >= MIN_NEFF) & (g["pos_periods"] >= MIN_POS_PERIODS)
    return g.reset_index()


def line(label: str, x: pd.DataFrame) -> str:
    if not len(x):
        return f"| {label} | 0 | - | - | - |"
    n = x["net"]
    loss = -n[n <= 0].sum()
    return f"| {label} | {len(x):,} | {(n > 0).mean():.1%} | {n.mean():+.2f}% | {(n[n > 0].sum() / loss if loss > 0 else 99):.2f} |"


def run(root: Path) -> Path:
    cols = ["stock", "timeframe", "strategy_id", "name", "side", "month", "selected", "signal", "net_pct"]
    t = pd.read_csv(root / "all_unseen_trades.csv", usecols=cols, dtype={"month": str})
    t = t.rename(columns={"net_pct": "net", "month": "period"})
    t["day"] = t["signal"].astype(str).str[:10]
    days = np.unique(t["day"].to_numpy())
    periods = sorted(t["period"].unique(), key=_start)
    picks = {"v1": [], "v2": []}
    for P in periods[1:]:  # the first period has no unseen history to judge from
        st = _start(P)
        j = judge(t[t["day"] < st], st, days)
        inP = t[t["period"] == P]
        for rule in ("v1", "v2"):
            ok = j.loc[j[rule], KEY]
            picks[rule].append(inP.merge(ok, on=KEY, how="inner"))
    pv = {r: (pd.concat(v, ignore_index=True) if v else t.iloc[:0]) for r, v in picks.items()}
    later = t[t["period"].isin(periods[1:])]
    sel = later[later["selected"].astype(str).str.lower() == "true"]

    head = ["| Set | Trades | Win | Avg net | PF |", "|---|---|---|---|---|"]
    L = [f"# ACTIVE rule v2 - {root.name}", "",
         f"v1: >= {MIN_TRADES} unseen trades, recency-weighted avg > 0, PF > {MIN_PF}, Edge > 0. "
         f"v2: v1 + effective trades >= {MIN_NEFF:g} + profitable in >= {MIN_POS_PERIODS} distinct periods "
         f"(PF capped at {PF_CAP:g} for ranking).", "",
         "Causal test: each period's picks are judged only on unseen trades from earlier periods. "
         f"Periods {periods[1]} .. {periods[-1]} (the first period, {periods[0]}, only feeds history).", "",
         "## Headline (same periods for every row)", "", *head,
         line("Walk-forward 'selected at the time' (train rule)", sel),
         line("v1 on earlier unseen trades", pv["v1"]),
         line("v2 on earlier unseen trades", pv["v2"]),
         line("All strategies", later), "",
         "## Per period", "",
         "| Period | v1 trades | v1 win | v1 avg | v1 PF | v2 trades | v2 win | v2 avg | v2 PF | All avg |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for P in periods[1:]:
        cells = []
        for r in ("v1", "v2"):
            x = pv[r][pv[r]["period"] == P]["net"] if len(pv[r]) else pd.Series(dtype=float)
            loss = -x[x <= 0].sum()
            cells.append(f"{len(x):,} | {(x > 0).mean():.0%} | {x.mean():+.2f}% | "
                         f"{(x[x > 0].sum() / loss if loss > 0 else 99):.2f}" if len(x) else "0 | - | - | -")
        L.append(f"| {P} | {cells[0]} | {cells[1]} | {later[later['period'] == P]['net'].mean():+.2f}% |")
    L += ["", "## Per timeframe (v2)", "", *head,
          *[line(tf, pv["v2"][pv["v2"]["timeframe"] == tf]) for tf in sorted(t["timeframe"].unique())]]

    # ACTIVE now: everything up to the latest day
    now = judge(t, "9999-12-31", days)
    names = t.drop_duplicates(KEY).set_index(KEY)[["name", "side"]]
    now = now.join(names, on=KEY)
    a1, a2 = now[now["v1"]], now[now["v2"]].copy()
    a2["pf_rank"] = a2["pf"].clip(upper=PF_CAP)
    a2 = a2.sort_values(["edge", "pf_rank"], ascending=False)
    L += ["", f"## ACTIVE now: v1 {len(a1):,} combos on {a1['stock'].nunique():,} stocks -> "
              f"v2 {len(a2):,} combos on {a2['stock'].nunique():,} stocks", "",
          "| Stock | TF | Strategy | Side | Trades | n_eff | Pos periods | Recent avg | PF (cap 5) | Recent win | Edge |",
          "|---|---|---|---|---|---|---|---|---|---|---|",
          *[f"| {r.stock} | {r.timeframe} | {r.name} | {r.side} | {int(r.n)} | {r.n_eff:.1f} | {int(r.pos_periods)} | "
            f"{r.avg:+.2f}% | {min(r.pf, PF_CAP):.2f} | {r.win:.0%} | {r.edge:+.2f} |" for r in a2.head(150).itertuples()]]
    amap: dict[str, dict[str, list[str]]] = {}
    for r in a2.itertuples():
        amap.setdefault(r.stock, {}).setdefault(r.timeframe, []).append(r.strategy_id)
    (root / "active_v2.json").write_text(json.dumps(amap, indent=1), encoding="utf-8")
    out = root / "ACTIVE_V2.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", help="walk-forward run folders (with all_unseen_trades.csv)")
    a = ap.parse_args(argv)
    for r in a.runs:
        print(run(Path(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
