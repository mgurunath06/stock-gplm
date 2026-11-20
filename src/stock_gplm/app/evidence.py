"""Backtest evidence for live-lab confidence (owner rule C, 2026-11-20). PAPER/research only.

Source: research-matrix audit trades (`<home>/reports/research/<run>/audit/trades/<SYMBOL>/<tf>.csv.gz`),
validation segment only (all candles <= development_end; never the test period). Net returns include costs.

Independence: agreeing strategies that enter on the same bar of the same stock are ONE observation
(their net returns are averaged). Scope: the stock's own evidence when it has >= MEDIUM_N independent
trades, else all stocks pooled (MARKET_WIDE, labelled).

Rule C: HIGH  n >= 100 & cautious net/trade > +0.15% & win rate > break-even for the R:R
        MEDIUM n >= 40 & cautious net/trade > 0
        else LOW; one level down on a bearish conflict.
cautious net = one-sided 90% lower bound of the mean (mean - 1.2816 * sd / sqrt(n)).
break-even win rate = stop / (stop + target) from the strategy exits; if no fixed R:R, the empirical
avg loss / (avg win + avg loss).
"""
from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

HIGH_N, MEDIUM_N = 100, 40
HIGH_NET = 0.0015
Z90 = 1.2816


def _bare(sym: str) -> str:
    return sym[:-3] if sym.endswith("-EQ") else sym


class Evidence:
    """Loads audit trades from the named research runs; reloads when a run's audit index changes."""

    def __init__(self, research_root: Path, runs: Iterable[str]):
        self.root, self.runs = Path(research_root), list(runs)
        self.lock = threading.Lock()
        self._stamp: tuple = ()
        self.trades = pd.DataFrame(columns=["run", "symbol", "tf", "strategy_id", "entry_bar", "net"])
        self.index: dict[tuple[str, str], pd.DataFrame] = {}  # (tf, strategy) -> trades

    def _index_files(self) -> list[Path]:
        return [p for r in self.runs if (p := self.root / r / "audit" / "index.jsonl").exists()]

    def refresh(self) -> None:
        idx = self._index_files()
        stamp = tuple((str(p), p.stat().st_mtime) for p in idx)
        if stamp == self._stamp:
            return
        frames = []
        for p in idx:
            run = p.parent.parent.name
            for f in (p.parent / "trades").glob("*/*.csv.gz"):
                t = pd.read_csv(f, usecols=["strategy_id", "segment", "entry_bar", "net_return"])
                t = t[t["segment"] == "validation"]
                if t.empty:
                    continue
                frames.append(pd.DataFrame({"run": run, "symbol": _bare(f.parent.name),
                                            "tf": f.name.split(".")[0], "strategy_id": t["strategy_id"].astype(str),
                                            "entry_bar": t["entry_bar"].astype(str), "net": t["net_return"].astype(float)}))
        with self.lock:
            if frames:
                d = pd.concat(frames, ignore_index=True)
                # a later run in self.runs never overrides an earlier one for the same lane
                order = {r: i for i, r in enumerate(self.runs)}
                d["_o"] = d["run"].map(order)
                first = d.groupby(["symbol", "tf"])["_o"].transform("min")
                self.trades = d[d["_o"] == first].drop(columns="_o")
                self.index = {k: g for k, g in self.trades.groupby(["tf", "strategy_id"])}
            self._stamp = stamp

    def lookup(self, strategies: Iterable[str], symbol: str, tf: str, rr: float | None,
               conflict: bool) -> dict[str, Any] | None:
        """Rule-C confidence for a setup, or None when no backtest evidence exists for these strategies x tf."""
        with self.lock:
            parts = [g for i in set(strategies) if (g := self.index.get((tf, i))) is not None]
        if not parts:
            return None
        t = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
        own = t[t["symbol"] == _bare(symbol)].groupby("entry_bar")["net"].mean()
        if len(own) >= MEDIUM_N:
            x, scope = own, "SYMBOL"
        else:
            x, scope = t.groupby(["symbol", "entry_bar"])["net"].mean(), "MARKET_WIDE"
        return rule_c(x.to_numpy(float), rr, conflict) | {
            "basis": f"BACKTEST_{scope}", "own_n": int(len(own)), "runs": sorted(t["run"].unique().tolist())}


def rule_c(net: Any, rr: float | None, conflict: bool) -> dict[str, Any]:
    n = len(net)
    mean = float(net.mean()) if n else 0.0
    sd = float(net.std(ddof=1)) if n > 1 else float("inf")
    cautious = mean - Z90 * sd / math.sqrt(n) if n > 1 else float("-inf")
    wins, losses = net[net > 0], net[net <= 0]
    p = len(wins) / n if n else 0.0
    if rr and rr > 0:
        be = 1 / (1 + rr)
    else:
        aw, al = (float(wins.mean()) if len(wins) else 0.0), (float(-losses.mean()) if len(losses) else 0.0)
        be = al / (aw + al) if aw + al > 0 else 0.5
    lab = ("HIGH" if n >= HIGH_N and cautious > HIGH_NET and p > be
           else "MEDIUM" if n >= MEDIUM_N and cautious > 0 else "LOW")
    if conflict and lab != "LOW":
        lab = "MEDIUM" if lab == "HIGH" else "LOW"
    from .opportunity import wilson_lower
    return {"label": lab, "n": n, "p": round(p, 3), "p_lo": round(wilson_lower(p, n, 1.6449), 3) if n else None,
            "avg": round(mean * 100, 3), "cautious_pct": round(cautious * 100, 3) if math.isfinite(cautious) else None,
            "break_even": round(be, 3), "conflict": conflict,
            "rule": "C: HIGH n>=100 & cautious net>+0.15% & win>break-even; MEDIUM n>=40 & cautious net>0; else LOW"}
