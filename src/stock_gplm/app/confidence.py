"""Confidence per strategy x stock x timeframe, and which strategies are ACTIVE (owner 2026-11-20).

Same definitions as the up-move learner:
  Confidence = chance a trade ends in profit (net of costs). Recent trades weigh more: weight (1 + age/100)^-2,
               age in sessions before the latest one. Few trades are pulled toward the stock x TF x side base
               rate (all strategies together) by ``prior`` pseudo-trades - a handful of lucky trades cannot look sure.
  RR         = average win / average loss (same weighting, same pull toward the stock x TF averages).
  Edge       = Confidence x RR - (1 - Confidence): expected gain per unit of risk.
ACTIVE when trades >= min_trades, Confidence >= min_confidence and Edge >= min_edge. Thresholds live in
config/strategies/confidence.json (default < stocks.SYM."*" < stocks.SYM.<tf>), so they can be set per stock x TF.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
CONF_FILE = REPO / "config" / "strategies" / "confidence.json"
DEFAULTS = {"min_confidence": 0.45, "min_edge": 0.0, "min_trades": 10, "prior": 5.0, "decay_sessions": 100.0}


def load(path: Path | None = None) -> dict[str, Any]:
    p = path or CONF_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def thresholds(doc: dict[str, Any], stock: str, tf: str) -> dict[str, float]:
    st = (doc.get("stocks") or {}).get(stock.upper().removesuffix("-EQ")) or {}
    out = {**DEFAULTS, **(doc.get("default") or {})}
    for part in (st.get("*"), st.get(tf)):
        if part:
            out.update({k: v for k, v in part.items() if k in DEFAULTS})
    return out


def score(trades: pd.DataFrame, doc: dict[str, Any] | None = None) -> pd.DataFrame:
    """trades: columns stock, timeframe, strategy_id, name, side, signal, net_pct. One row per stock x TF x strategy."""
    doc = load() if doc is None else doc
    t = trades.copy()
    t["day"] = t["signal"].astype(str).str[:10]
    t["win"] = t["net_pct"] > 0
    rows = []
    for (stock, tf), g in t.groupby(["stock", "timeframe"]):
        th = thresholds(doc, stock, tf)
        days = np.sort(g["day"].unique())
        idx = {d: i for i, d in enumerate(days)}
        age = len(days) - 1 - g["day"].map(idx).to_numpy()
        g = g.assign(w=(1.0 + age / th["decay_sessions"]) ** -2)
        for side, gs in g.groupby("side"):
            sw = gs["w"].sum()
            base = float((gs["w"] * gs["win"]).sum() / sw) if sw else 0.0
            wins, losses = gs[gs["win"]], gs[~gs["win"]]
            base_aw = float((wins["w"] * wins["net_pct"]).sum() / wins["w"].sum()) if len(wins) else 0.0
            base_al = float((losses["w"] * -losses["net_pct"]).sum() / losses["w"].sum()) if len(losses) else 1.0
            pr = th["prior"]
            for (sid, name), x in gs.groupby(["strategy_id", "name"]):
                w = x["w"]
                conf = float(((w * x["win"]).sum() + pr * base) / (w.sum() + pr))
                xw, xl = x[x["win"]], x[~x["win"]]
                aw = float(((xw["w"] * xw["net_pct"]).sum() + pr * base_aw) / (xw["w"].sum() + pr))
                al = float(((xl["w"] * -xl["net_pct"]).sum() + pr * base_al) / (xl["w"].sum() + pr))
                rr = aw / al if al > 1e-9 else float("inf")
                edge = conf * rr - (1 - conf)
                n = len(x)
                active = n >= th["min_trades"] and conf >= th["min_confidence"] and edge >= th["min_edge"]
                why = [] if active else [r for r, bad in (
                    (f"trades {n} < {th['min_trades']:g}", n < th["min_trades"]),
                    (f"confidence {conf:.0%} < {th['min_confidence']:.0%}", conf < th["min_confidence"]),
                    (f"edge {edge:+.2f} < {th['min_edge']:+.2f}", edge < th["min_edge"])) if bad]
                rows.append({"stock": stock, "timeframe": tf, "strategy_id": sid, "name": name, "side": side,
                             "trades": n, "win_rate": float(x["win"].mean()), "avg_net_pct": float(x["net_pct"].mean()),
                             "total_net_pct": float(x["net_pct"].sum()), "confidence": conf, "rr": rr, "edge": edge,
                             "last_trade": str(x["day"].max()), "status": "ACTIVE" if active else "INACTIVE",
                             "why": "; ".join(why), "min_confidence": th["min_confidence"]})
    return pd.DataFrame(rows)


def active_map(scored: pd.DataFrame) -> dict[str, dict[str, list[str]]]:
    """{stock: {tf: [strategy ids]}} of ACTIVE rows, best Edge first - what live alerts should use."""
    out: dict[str, dict[str, list[str]]] = {}
    for r in scored[scored["status"] == "ACTIVE"].sort_values("edge", ascending=False).itertuples():
        out.setdefault(r.stock, {}).setdefault(r.timeframe, []).append(r.strategy_id)
    return out


TFS = ("1mo", "1w", "1d", "60m", "30m", "15m", "5m")


def write_reports(root: Path, trades: pd.DataFrame, title: str = "") -> Path:
    """confidence.csv (every stock x TF x strategy), active.json (ACTIVE per stock x TF) and ALL_STOCKS.md."""
    sc = score(trades)
    sc.to_csv(root / "confidence.csv", index=False)
    (root / "active.json").write_text(json.dumps(active_map(sc), indent=1), encoding="utf-8")
    act = sc[sc["status"] == "ACTIVE"]
    lines = [f"# All stocks {title}: {sc['stock'].nunique()} stocks, {len(trades)} trades", "",
             "Confidence = chance a trade ends in profit (recent trades weigh more, few trades pulled toward the "
             "stock's base rate); Edge = Confidence x RR - (1 - Confidence). ACTIVE = trades, Confidence and Edge "
             "at or above config/strategies/confidence.json (default "
             f"{json.dumps((load().get('default') or DEFAULTS))}). Re-score after changing it: "
             f"`python -m stock_gplm.app.confidence {root}`.", "",
             f"ACTIVE: {len(act)} of {len(sc)} stock x TF x strategy combinations.", ""]
    for tf in TFS:
        x = sc[sc["timeframe"] == tf]
        if x.empty:
            continue
        g = x.groupby(["strategy_id", "name", "side"]).agg(
            stocks=("stock", "size"), active=("status", lambda s: int((s == "ACTIVE").sum())),
            conf=("confidence", "median"), edge=("edge", "median"), trades=("trades", "sum"),
            total=("total_net_pct", "sum")).reset_index()
        best = x[x["status"] == "ACTIVE"].sort_values("edge", ascending=False).groupby("strategy_id").head(4) \
            .groupby("strategy_id")["stock"].apply(lambda s: ", ".join(s)).rename("active_on")
        g = g.merge(best, on="strategy_id", how="left").sort_values(["active", "edge"], ascending=False)
        lines += [f"## {tf}: per strategy across stocks", "",
                  "| Strategy | Side | Active on | Median confidence | Median edge | Trades | Total net | Best active stocks |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in g.itertuples():
            lines.append(f"| {r.name} ({r.strategy_id}) | {r.side} | {r.active} of {r.stocks} | {r.conf:.0%} | {r.edge:+.2f} | "
                         f"{r.trades} | {r.total:+.1f}% | {r.active_on if isinstance(r.active_on, str) else ''} |")
        lines.append("")
    lines += ["## ACTIVE strategies per stock x TF (best edge first)", "", "| Stock | TF | Active | Strategies (confidence, edge, trades) |",
              "|---|---|---|---|"]
    for (stock, tf), x in sc.groupby(["stock", "timeframe"]):
        a = x[x["status"] == "ACTIVE"].sort_values("edge", ascending=False)
        lst = "; ".join(f"{r.name} {r.confidence:.0%}/{r.edge:+.2f}/{r.trades}" for r in a.itertuples())
        lines.append(f"| {stock} | {tf} | {len(a)} | {lst} |")
    out = root / "ALL_STOCKS.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    """Re-score a finished forward_batch run with the current confidence.json (no trades are recomputed)."""
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("run", help="forward_batch output directory (contains all_trades.csv)")
    a = ap.parse_args(argv)
    root = Path(a.run)
    print(write_reports(root, pd.read_csv(root / "all_trades.csv"), root.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
