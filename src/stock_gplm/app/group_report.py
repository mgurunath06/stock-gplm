"""ACTIVE learnings split by index (Nifty 50 / Bank Nifty / Nifty 100 / Nifty 200) and by sector (owner 2026-11-21).

    python -m stock_gplm.app.group_report --learnings <CURRENT_LEARNINGS.csv> [--out GROUPS.md]

Constituents + sectors: official NSE Indices files in data/reference/nifty-constituents/ (ind_*.csv;
Industry column = sector). Learnings CSV: one row per ACTIVE stock x TF x strategy with trades, win_rate,
avg_net_pct, max_trade, worst_trade, total, edge, exit.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

REF = Path(__file__).resolve().parents[4] / "data" / "reference" / "nifty-constituents"
GROUPS = [("Nifty 50", "ind_nifty50list"), ("Bank Nifty", "ind_niftybanklist"),
          ("Nifty 100", "ind_nifty100list"), ("Nifty 200", "ind_nifty200list")]


def _members(f: str) -> pd.DataFrame:
    return pd.read_csv(REF / f"{f}.csv")[["Symbol", "Industry"]]


def sectors() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted(REF.glob("ind_*.csv")):
        out.update({s: i for s, i in _members(f.stem).itertuples(index=False) if s not in out})
    return out


def _strategy_table(a: pd.DataFrame) -> list[str]:
    a = a.assign(wins=a.trades * a.win_rate, tot=a.trades * a.avg_net_pct)
    g = a.groupby(["name", "side"]).agg(stocks=("stock", "nunique"), tfs=("timeframe", lambda x: ",".join(sorted(set(x)))),
                                        trades=("trades", "sum"), wins=("wins", "sum"), tot=("tot", "sum"),
                                        best=("max_trade", "max"), worst=("worst_trade", "min"))
    g = g.sort_values("tot", ascending=False)
    L = ["| Strategy | Side | TF | Stocks | Trades | Win | Avg net | Total | Best trade | Worst trade |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for (n, s), r in g.iterrows():
        L.append(f"| {n} | {s} | {r.tfs} | {r.stocks} | {r.trades:.0f} | {r.wins / r.trades:.0%} | "
                 f"{r.tot / r.trades:+.2f}% | {r.tot:+.0f}% | {r.best:+.1f}% | {r.worst:+.1f}% |")
    return L


def _best_table(a: pd.DataFrame, sec: dict[str, str]) -> list[str]:
    b = a.sort_values(["stock", "edge"], ascending=[True, False]).groupby("stock").head(1)
    n = a.groupby("stock").size()
    L = ["| Stock | Sector | Best strategy | TF | Side | Trades | Win | Avg net | Best trade | Worst trade | Exit | ACTIVE combos |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in b.sort_values("total", ascending=False).itertuples():
        L.append(f"| {r.stock} | {sec.get(r.stock, '-')} | {r.name} | {r.timeframe} | {r.side} | {r.trades} | "
                 f"{r.win_rate:.0%} | {r.avg_net_pct:+.2f}% | {r.max_trade:+.1f}% | {r.worst_trade:+.1f}% | {r.exit} | {n[r.stock]} |")
    return L


def build(learn: Path) -> str:
    a = pd.read_csv(learn)
    sec = sectors()
    a["sector"] = a.stock.map(sec).fillna("Unknown (not in NSE index files)")
    L = ["# ACTIVE learnings by index and sector", "",
         f"Source: `{learn.name}` (2026, in-sample). Constituents/sectors: NSE Indices files in `{REF}`.", ""]
    for title, f in GROUPS:
        mem = set(_members(f).Symbol)
        g = a[a.stock.isin(mem)]
        L += [f"## {title}", "", f"{g.stock.nunique()} of {len(mem)} stocks have ACTIVE strategies "
              f"({len(g)} combos). Missing: {', '.join(sorted(mem - set(g.stock))) or '-'}.", "",
              "### Strategies", "", *_strategy_table(g), "", "### Best strategy per stock", "", *_best_table(g, sec), ""]
    L += ["## Sectors overview", "", "| Sector | Stocks | Combos | Trades | Win | Avg net | Top strategy (by total) |",
          "|---|---|---|---|---|---|---|"]
    a2 = a.assign(wins=a.trades * a.win_rate, tot=a.trades * a.avg_net_pct)
    for s, g in sorted(a2.groupby("sector"), key=lambda x: -x[1].tot.sum()):
        top = g.groupby("name").tot.sum().idxmax()
        L.append(f"| {s} | {g.stock.nunique()} | {len(g)} | {g.trades.sum()} | {g.wins.sum() / g.trades.sum():.0%} | "
                 f"{g.tot.sum() / g.trades.sum():+.2f}% | {top} |")
    L.append("")
    for s, g in sorted(a.groupby("sector"), key=lambda x: -(x[1].trades * x[1].avg_net_pct).sum()):
        L += [f"## Sector: {s}", "", "### Strategies", "", *_strategy_table(g), "", "### Best strategy per stock", "",
              *_best_table(g, sec), ""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--learnings", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    out = a.out or a.learnings.with_name(a.learnings.stem.replace("CURRENT_LEARNINGS", "GROUPS") + ".md")
    out.write_text(build(a.learnings), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
