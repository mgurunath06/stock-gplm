"""Confidence per strategy x stock x timeframe, and which strategies are ACTIVE (owner 2026-11-20).

Same definitions as the up-move learner:
  Confidence = chance a trade ends in profit (net of costs). Recent trades weigh more: weight (1 + age/100)^-2,
               age in sessions before the latest one. Few trades are pulled toward the stock x TF x side base
               rate (all strategies together) by ``prior`` pseudo-trades - a handful of lucky trades cannot look sure.
  RR         = average win / average loss (same weighting, same pull toward the stock x TF averages).
  Edge       = Confidence x RR - (1 - Confidence): expected gain per unit of risk.
Pooled (owner 2026-11-22, small recent samples): the same strategy on ALL stocks of a TF x side gives
               pool_conf / pool_rr / pool_edge (many recent trades -> real confidence). With
               prior_source = "strategy" a stock's few trades are pulled toward that pooled strategy rate instead of
               the stock's all-strategy rate, and conf_low = cautious lower estimate (Beta quantile lb_quantile) - few
               trades can count, at an honest level.
ACTIVE when trades >= min_trades, Confidence >= min_confidence, conf_low >= min_conf_low, Edge >= min_edge and the
pooled strategy passes min_pool_edge / min_pool_trades. Thresholds live in config/strategies/confidence.json
(default < timeframes.<tf> < stocks.SYM."*" < stocks.SYM.<tf>), so they can be set per TF and per stock x TF. Check settings causally with
``python -m stock_gplm.app.rolling_check <run>``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
CONF_FILE = REPO / "config" / "strategies" / "confidence.json"
DEFAULTS: dict[str, Any] = {"min_confidence": 0.45, "min_edge": 0.0, "min_trades": 10, "prior": 5.0, "decay_sessions": 100.0,
            "prior_source": "stock", "lb_quantile": 0.2, "min_conf_low": 0.0, "pool_prior": 20.0,
            "min_pool_edge": -1e9, "min_pool_trades": 0}


def load(path: Path | None = None) -> dict[str, Any]:
    p = path or CONF_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def thresholds(doc: dict[str, Any], stock: str, tf: str) -> dict[str, float]:
    st = (doc.get("stocks") or {}).get(stock.upper().removesuffix("-EQ")) or {}
    out = {**DEFAULTS, **(doc.get("default") or {})}
    for part in ((doc.get("timeframes") or {}).get(tf), st.get("*"), st.get(tf)):
        if part:
            out.update({k: v for k, v in part.items() if k in DEFAULTS})
    return out


def _ratio(num: pd.Series, den: pd.Series, default: float) -> pd.Series:
    return (num / den.where(den > 0)).fillna(default)


def score(trades: pd.DataFrame, doc: dict[str, Any] | None = None, explain: bool = True) -> pd.DataFrame:
    """trades: columns stock, timeframe, strategy_id, name, side, signal, net_pct. One row per stock x TF x strategy.
    explain=False skips the per-row 'why' text (fast path for rolling_check)."""
    from scipy.stats import beta
    doc = load() if doc is None else doc
    cols = ["stock", "timeframe", "strategy_id", "name", "side"]
    t = trades[cols + ["signal", "net_pct"]].copy()
    if t.empty:
        return pd.DataFrame()
    t["day"] = t["signal"].astype(str).str[:10]
    t["win"] = (t["net_pct"] > 0).astype(float)
    pairs = t[["stock", "timeframe"]].drop_duplicates()
    th = pd.DataFrame([{"stock": r.stock, "timeframe": r.timeframe, **thresholds(doc, r.stock, r.timeframe)}
                       for r in pairs.itertuples()])
    t = t.merge(th[["stock", "timeframe", "decay_sessions"]], on=["stock", "timeframe"], how="left")
    rank = t.groupby(["stock", "timeframe"])["day"].rank(method="dense")
    age = rank.groupby([t["stock"], t["timeframe"]]).transform("max") - rank
    t["w"] = (1.0 + age / t["decay_sessions"].astype(float)) ** -2
    t["ww"] = t["w"] * t["win"]                      # weighted wins
    t["wl"] = t["w"] - t["ww"]                       # weighted losses
    t["wgain"] = t["ww"] * t["net_pct"]              # weighted win size
    t["wloss"] = -t["wl"] * t["net_pct"]             # weighted loss size (positive)
    sums = ["w", "ww", "wl", "wgain", "wloss"]

    def bases(g: pd.DataFrame, pre: str) -> pd.DataFrame:
        return pd.DataFrame({f"{pre}conf": _ratio(g["ww"], g["w"], 0.0), f"{pre}aw": _ratio(g["wgain"], g["ww"], 0.0),
                             f"{pre}al": _ratio(g["wloss"], g["wl"], 1.0)})

    stock_base = bases(t.groupby(["stock", "timeframe", "side"])[sums].sum(), "sb_").reset_index()
    tf_base = bases(t.groupby(["timeframe", "side"])[sums].sum(), "tb_").reset_index()
    pool = t.groupby(["timeframe", "side", "strategy_id"]).agg(
        **{k: (k, "sum") for k in sums}, pool_trades=("win", "size"), pool_stocks=("stock", "nunique")).reset_index()
    pool = pool.merge(tf_base, on=["timeframe", "side"], how="left")
    gdef = thresholds(doc, "", "")
    pp = float(gdef["pool_prior"])
    pool["pool_conf"] = (pool["ww"] + pp * pool["tb_conf"]) / (pool["w"] + pp)
    pool["pool_aw"] = (pool["wgain"] + pp * pool["tb_aw"]) / (pool["ww"] + pp)
    pool["pool_al"] = (pool["wloss"] + pp * pool["tb_al"]) / (pool["wl"] + pp)
    pool["pool_rr"] = pool["pool_aw"] / pool["pool_al"].where(pool["pool_al"] > 1e-9)
    pool["pool_edge"] = pool["pool_conf"] * pool["pool_rr"] - (1 - pool["pool_conf"])
    pool = pool[["timeframe", "side", "strategy_id", "pool_trades", "pool_stocks", "pool_conf", "pool_aw",
                 "pool_al", "pool_rr", "pool_edge"]]

    g = t.groupby(cols).agg(**{k: (k, "sum") for k in sums}, trades=("win", "size"), win_rate=("win", "mean"),
                            avg_net_pct=("net_pct", "mean"), total_net_pct=("net_pct", "sum"),
                            last_trade=("day", "max")).reset_index()
    g = g.merge(stock_base, on=["stock", "timeframe", "side"], how="left")          .merge(pool, on=["timeframe", "side", "strategy_id"], how="left")          .merge(th, on=["stock", "timeframe"], how="left")
    use_pool = (g["prior_source"] == "strategy") & (g["pool_stocks"] > 1)   # one stock only: no pooling possible
    b_conf = g["pool_conf"].where(use_pool, g["sb_conf"])
    b_aw = g["pool_aw"].where(use_pool, g["sb_aw"])
    b_al = g["pool_al"].where(use_pool, g["sb_al"])
    pr = g["prior"].astype(float)
    g["confidence"] = (g["ww"] + pr * b_conf) / (g["w"] + pr)
    aw = (g["wgain"] + pr * b_aw) / (g["ww"] + pr)
    al = (g["wloss"] + pr * b_al) / (g["wl"] + pr)
    g["rr"] = (aw / al.where(al > 1e-9)).fillna(float("inf"))
    g["edge"] = g["confidence"] * g["rr"] - (1 - g["confidence"])
    a_ = (g["ww"] + pr * b_conf).clip(lower=1e-6)
    b_ = (g["wl"] + pr * (1 - b_conf)).clip(lower=1e-6)
    g["conf_low"] = beta.ppf(g["lb_quantile"].astype(float), a_, b_)
    checks = [("trades", g["trades"] < g["min_trades"], lambda r: f"trades {r.trades} < {r.min_trades:g}"),
              ("conf", g["confidence"] < g["min_confidence"],
               lambda r: f"confidence {r.confidence:.0%} < {r.min_confidence:.0%}"),
              ("low", g["conf_low"] < g["min_conf_low"], lambda r: f"cautious confidence {r.conf_low:.0%} < "
                                                                   f"{r.min_conf_low:.0%}"),
              ("edge", g["edge"] < g["min_edge"], lambda r: f"edge {r.edge:+.2f} < {r.min_edge:+.2f}"),
              ("pedge", g["pool_edge"].fillna(-9) < g["min_pool_edge"],
               lambda r: f"strategy edge on all stocks {r.pool_edge:+.2f} < {r.min_pool_edge:+.2f}"),
              ("ptr", g["pool_trades"] < g["min_pool_trades"],
               lambda r: f"strategy trades on all stocks {r.pool_trades} < {r.min_pool_trades:g}")]
    bad = pd.concat([c[1].rename(c[0]) for c in checks], axis=1)
    g["status"] = np.where(bad.any(axis=1), "INACTIVE", "ACTIVE")
    why = [""] * len(g)
    for i in (np.flatnonzero(bad.any(axis=1).to_numpy()) if explain else ()):
        r = g.iloc[i]
        why[i] = "; ".join(fmt(r) for (name, _, fmt) in checks if bad.iloc[i][name])
    g["why"] = why
    return g[cols + ["trades", "win_rate", "avg_net_pct", "total_net_pct", "confidence", "conf_low", "rr", "edge",
                     "pool_trades", "pool_stocks", "pool_conf", "pool_rr", "pool_edge", "last_trade", "status", "why",
                     "min_confidence"]].reset_index(drop=True)


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
