"""Strategy scorecard (PAPER/research): which strategy works for which stock on which timeframe.

Joins two independent periods per (stock, timeframe, strategy):
  * development - research-matrix validation metrics (candles <= development_end), every run given;
  * test        - the setup replay's per-strategy alerts after development_end (own levels and exits).

Ranking uses a shrunk mean so a stock with few trades leans on the strategy's all-stock average:
    shrunk = (n * stock_mean + K * all_stock_mean) / (n + K)
Verdict per row: CONFIRMED (test n >= MIN_TEST and test mean net > 0), FAILED (test n >= MIN_TEST and <= 0),
UNTESTED (fewer test trades, e.g. 1d/1w which the intraday replay does not cover).
`stock_specific` marks rows where the stock clearly beats the strategy's all-stock average in development
AND the test period agrees - the "this stock bounces at X" kind of finding.

Usage (PYTHONPATH=src):
  python -m stock_gplm.app.strategy_scorecard --runs evidence-intraday,evidence-daily,discover-deep --replay test-period
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .live_lab import REPO

K = 30          # shrinkage strength (pseudo-trades of the all-stock average)
MIN_DEV = 20    # development (validation) trades needed to rank a row
MIN_TEST = 10   # test-period trades needed for a CONFIRMED/FAILED verdict
COLS = ["symbol", "timeframe", "strategy_id", "source", "origin", "category", "entry_rule", "params", "val_trades",
        "val_mean_net", "val_win_rate", "val_profit_factor", "val_q_value", "global_q_value", "classification"]


def _bare(s: str) -> str:
    return s[:-3] if str(s).endswith("-EQ") else str(s)


def load_dev(research_root: Path, runs: list[str]) -> pd.DataFrame:
    frames = []
    for r in runs:
        p = research_root / r / "matrix.csv"
        if not p.exists():
            print(f"  (skip: {p} not found)")
            continue
        m = pd.read_csv(p, usecols=lambda c: c in COLS, low_memory=False)
        m = m[m["val_trades"].fillna(0) > 0].copy()
        m["run"] = r
        frames.append(m)
    if not frames:
        return pd.DataFrame(columns=COLS + ["run"])
    d = pd.concat(frames, ignore_index=True)
    d["symbol"] = d["symbol"].map(_bare)
    # same strategy+lane in several runs: keep the run listed first
    d["_o"] = d["run"].map({r: i for i, r in enumerate(runs)})
    return d.sort_values("_o").drop_duplicates(["symbol", "timeframe", "strategy_id"]).drop(columns="_o")


def load_test(replay_dir: Path) -> pd.DataFrame:
    files = sorted((replay_dir / "alerts").glob("*.csv"))
    frames = [pd.read_csv(f) for f in files if f.stat().st_size > 5]
    if not frames:
        return pd.DataFrame(columns=["symbol", "timeframe", "strategy_id", "test_n", "test_mean_net", "test_win"])
    a = pd.concat(frames, ignore_index=True)
    a["net"] = a["net_pct"] / 100
    g = a.groupby(["symbol", "tf", "strategy"])["net"]
    t = pd.DataFrame({"test_n": g.size(), "test_mean_net": g.mean(), "test_win": g.apply(lambda x: (x > 0).mean())})
    return t.reset_index().rename(columns={"tf": "timeframe", "strategy": "strategy_id"})


def build(dev: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    d = dev.copy()
    n = d["val_trades"].astype(float)
    w = d["val_mean_net"].astype(float) * n
    pooled = (w.groupby([d["strategy_id"], d["timeframe"]]).transform("sum")
              / n.groupby([d["strategy_id"], d["timeframe"]]).transform("sum"))
    d["all_stock_mean_net"] = pooled
    d["shrunk_net"] = (w + K * pooled) / (n + K)
    d = d.merge(test.astype({"test_n": float, "test_mean_net": float, "test_win": float}),
                on=["symbol", "timeframe", "strategy_id"], how="left")
    d["test_n"] = d["test_n"].fillna(0).astype(int)
    d["verdict"] = np.where(d["test_n"] < MIN_TEST, "UNTESTED",
                            np.where(d["test_mean_net"] > 0, "CONFIRMED", "FAILED"))
    d["rankable"] = (d["val_trades"] >= MIN_DEV) & (d["shrunk_net"] > 0)
    se = d.groupby(["strategy_id", "timeframe"])["val_mean_net"].transform("std")
    d["stock_specific"] = (d["rankable"] & (d["val_mean_net"] - d["all_stock_mean_net"] > se.fillna(np.inf))
                           & (d["verdict"] == "CONFIRMED"))
    for c in ("val_mean_net", "all_stock_mean_net", "shrunk_net", "test_mean_net"):
        d[c.replace("net", "net_pct")] = (pd.to_numeric(d[c], errors="coerce") * 100).round(3)
    return d.sort_values(["symbol", "timeframe", "shrunk_net"], ascending=[True, True, False])


def assign_distinct(d: pd.DataFrame) -> pd.DataFrame:
    """Owner rule 2026-11-20: every stock x timeframe is judged on its own evidence (never assumed from another
    timeframe). The best strategy of each stock x timeframe is assigned; the same strategy may be assigned to
    several timeframes when it wins on each. FAILED (test period) rows are never assigned; CONFIRMED beats
    UNTESTED."""
    r = d[d["rankable"] & (d["verdict"] != "FAILED")].copy()
    r["_v"] = r["verdict"].map({"CONFIRMED": 0, "UNTESTED": 1})
    r = r.sort_values(["_v", "shrunk_net"], ascending=[True, False])
    used: set[tuple[str, str]] = set()
    done: set[tuple[str, str]] = set()
    keep = []
    for i, row in r.iterrows():
        if (row["symbol"], row["timeframe"]) in done:
            continue
        keep.append(i)
        done.add((row["symbol"], row["timeframe"]))
        used.add((row["symbol"], row["strategy_id"]))
    return r.loc[keep].drop(columns="_v").sort_values(["symbol", "timeframe"])


def report(d: pd.DataFrame, top: int) -> str:
    r = d[d["rankable"]]
    lines = ["# Strategy scorecard - best strategies per stock x timeframe",
             f"Development = validation segment (<= development_end); test = setup replay after it. Net of costs. "
             f"Ranked by shrunk net/trade (K={K}); rows need >= {MIN_DEV} development trades. "
             "PAPER/research only - not advice.", "",
             f"Rows ranked: {len(r)} of {len(d)}; verdicts among ranked: "
             f"{r['verdict'].value_counts().to_dict()}; stock-specific confirmed: {int(r['stock_specific'].sum())}", ""]
    a = assign_distinct(d)
    lines += ["## Assigned: best strategy per stock x timeframe, each timeframe judged on its own", "", "```",
              a[["symbol", "timeframe", "strategy_id", "val_trades", "val_mean_net_pct", "shrunk_net_pct",
                 "verdict", "test_n", "test_mean_net_pct", "entry_rule"]].to_string(index=False, max_colwidth=60),
              "```", ""]
    ss = r[r["stock_specific"]].sort_values("shrunk_net", ascending=False)
    if len(ss):
        lines += ["## Stock-specific edges (beats the all-stock average AND held in the test period)", "", "```",
                  ss[["symbol", "timeframe", "strategy_id", "entry_rule", "val_trades", "val_mean_net_pct",
                      "all_stock_mean_net_pct", "test_n", "test_mean_net_pct"]].head(60).to_string(index=False),
                  "```", ""]
    # strategy view: where does each strategy work? (confirmed first, then untested)
    sv = r[r["verdict"] != "FAILED"].copy()
    sv["_v"] = sv["verdict"].map({"CONFIRMED": 0, "UNTESTED": 1})
    sv = sv.sort_values(["strategy_id", "timeframe", "_v", "shrunk_net"], ascending=[True, True, True, False])
    by_s = (sv.groupby(["strategy_id", "timeframe"])
            .agg(stocks=("symbol", lambda x: ", ".join(x.head(12))), n_stocks=("symbol", "size"),
                 confirmed=("verdict", lambda x: int((x == "CONFIRMED").sum())),
                 all_stock_net_pct=("all_stock_mean_net_pct", "first"), rule=("entry_rule", "first"))
            .reset_index().sort_values(["confirmed", "n_stocks"], ascending=False))
    lines += ["## Strategy view - where each strategy works (not FAILED in the test period)", "", "```",
              by_s.head(80).to_string(index=False, max_colwidth=70), "```", ""]
    best = r.groupby(["symbol", "timeframe"]).head(top)
    lines += [f"## Top {top} per stock x timeframe", "", "```",
              best[["symbol", "timeframe", "strategy_id", "val_trades", "val_mean_net_pct", "shrunk_net_pct",
                    "val_q_value", "verdict", "test_n", "test_mean_net_pct"]].to_string(index=False), "```"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default="evidence-intraday,evidence-daily",
                    help="research-matrix run names, comma list (first wins on duplicates)")
    ap.add_argument("--replay", default="test-period", help="setup_replay --name for the test period")
    ap.add_argument("--name", default="latest")
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args()
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    runs = [r.strip() for r in a.runs.split(",") if r.strip()]
    dev = load_dev(Path(st.reports_dir) / "research", runs)
    test = load_test(REPO / "var" / "setup-replay" / a.replay)
    print(f"development rows {len(dev)} from {sorted(dev['run'].unique()) if len(dev) else []}; "
          f"test-period strategy rows {len(test)}")
    d = build(dev, test)
    out = Path(st.reports_dir) / "scorecard" / a.name
    out.mkdir(parents=True, exist_ok=True)
    d.to_csv(out / "scorecard.csv", index=False)
    assign_distinct(d).to_csv(out / "assigned.csv", index=False)
    txt = report(d, a.top)
    (out / "scorecard.md").write_text(txt, encoding="utf-8")
    print(txt[:6000])
    print(f"\nfull: {out / 'scorecard.csv'}  and  {out / 'scorecard.md'}")


if __name__ == "__main__":
    main()
