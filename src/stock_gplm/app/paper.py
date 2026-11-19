"""M6.6 — forward paper-trading ledger.

A *source* (``strategy:<id>@<version>`` or ``model:<artifact id>``) is enrolled with a ``START`` event.
From then on, only bars that **complete after enrolment** can create signals: paper trading is
forward-only and never backfills history.

Per trade the ledger appends ``SIGNAL`` (bar close) -> ``ENTRY`` (next bar open) -> ``EXIT`` (stop,
target, gap, session close or time), or ``CANCEL`` (e.g. the next bar is blocked or in another
session for intraday). Fills use the same conventions and per-side costs as backtests; every fill has
``fill_basis = PAPER`` (simulated, never presented as an actual broker fill).

Processing is idempotent: events are unique per ``(trade_id, kind)``, so re-running over the same bars
after a crash or restart adds nothing twice. :func:`summary` produces the evidence the strategy
lifecycle requires before ``PAPER_TESTED``.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

import numpy as np

from .db import AuditDB, jdump, sha, utc_now
from .targets import ns_label

PAPER_CONTRACT = "stock-gplm.paper-ledger.m66.v1"


def _ins(db: AuditDB, trade_id: str, kind: str, symbol: str, tf: str, source: str, bar_ns: int, price: float | None,
         payload: Mapping[str, Any]) -> bool:
    with db.tx() as c:
        n = c.execute("INSERT OR IGNORE INTO paper_events (trade_id, kind, symbol, timeframe, source, bar_label, bar_ns, "
                      "price, fill_basis, payload_json, ts_utc, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (trade_id, kind, symbol, tf, source, ns_label(bar_ns) if bar_ns else "", int(bar_ns), price,
                       "PAPER", jdump(payload), utc_now(), db.run_id)).rowcount
    return bool(n)


def enrol(db: AuditDB, source: str, actor: str, start_ns: int, note: str = "") -> dict[str, Any]:
    """Start forward paper trading for ``source`` at ``start_ns`` (normally 'now')."""
    tid = f"start:{source}"
    new = _ins(db, tid, "START", "*", "*", source, int(start_ns), None, {"actor": actor, "note": note,
                                                                          "contract": PAPER_CONTRACT})
    if new:
        db.event("PAPER_ENROLLED", {"source": source, "actor": actor, "start": ns_label(int(start_ns))}, subject=source)
    row = db.one("SELECT bar_ns FROM paper_events WHERE trade_id=? AND kind='START'", (tid,))
    return {"source": source, "enrolled": True, "new": new, "start": ns_label(int(row["bar_ns"]))}


def start_ns(db: AuditDB, source: str) -> int | None:
    row = db.one("SELECT bar_ns FROM paper_events WHERE trade_id=? AND kind='START'", (f"start:{source}",))
    return int(row["bar_ns"]) if row else None


def process(db: AuditDB, source: str, symbol: str, m: Any, signal: np.ndarray, exit_cfg: Mapping[str, Any],
            buy: float, sell: float) -> dict[str, int]:
    """Advance the paper book of ``source`` on one lane ``m`` (LaneMarket-like) with a boolean ``signal``.

    ``exit_cfg``: ``max_hold_bars``, optional ``stop_atr``, ``target_atr``. Long only (cash equity)."""
    st = start_ns(db, source)
    if st is None:
        raise ValueError(f"{source} is not enrolled for paper trading (gplm paper enrol)")
    n = len(m.close)
    counts = {"signals": 0, "entries": 0, "exits": 0, "cancels": 0}
    tf = m.timeframe
    hold = int(exit_cfg["max_hold_bars"])
    sa, ta = exit_cfg.get("stop_atr"), exit_cfg.get("target_atr")
    # new signals (forward-only, completed bars, no overlap with an open trade of the same source/lane)
    open_rows = db.q("SELECT trade_id FROM paper_events WHERE source=? AND symbol=? AND timeframe=? AND kind='SIGNAL' "
                     "AND trade_id NOT IN (SELECT trade_id FROM paper_events WHERE kind IN ('EXIT','CANCEL'))",
                     (source, symbol, tf))
    busy = bool(open_rows)
    for t in range(n):
        if int(m.end_ns[t]) < st or not signal[t] or m.no_signal[t] or busy:
            continue
        tid = "pt-" + sha({"s": source, "sym": symbol, "tf": tf, "bar": int(m.end_ns[t])})[:20]
        atr = float(m.natr[t]) * float(m.close[t]) if math.isfinite(float(m.natr[t])) else None
        if _ins(db, tid, "SIGNAL", symbol, tf, source, int(m.end_ns[t]), float(m.close[t]),
                {"atr": atr, "max_hold_bars": hold, "stop_atr": sa, "target_atr": ta, "signal_index_hint": t}):
            counts["signals"] += 1
        busy = True
    # advance every open trade of this lane
    sig_rows = db.q("SELECT * FROM paper_events WHERE source=? AND symbol=? AND timeframe=? AND kind='SIGNAL'",
                    (source, symbol, tf))
    pos = {int(v): i for i, v in enumerate(np.asarray(m.end_ns))}
    for s in sig_rows:
        tid = s["trade_id"]
        done = db.one("SELECT kind FROM paper_events WHERE trade_id=? AND kind IN ('EXIT','CANCEL')", (tid,))
        if done:
            continue
        t = pos.get(int(s["bar_ns"]))
        if t is None or t + 1 >= n:
            continue  # signal bar not in this frame, or the entry bar has not completed yet
        p = json.loads(s["payload_json"])
        e = t + 1
        if m.blocked[e] or (m.intraday and m.session_id[e] != m.session_id[t]):
            if _ins(db, tid, "CANCEL", symbol, tf, source, int(m.end_ns[e]), None,
                    {"reason": "next bar blocked" if m.blocked[e] else "next bar is another session (intraday)"}):
                counts["cancels"] += 1
            continue
        entry = float(m.open[e])
        if _ins(db, tid, "ENTRY", symbol, tf, source, int(m.end_ns[e]), entry, {"cost_buy": buy}):
            counts["entries"] += 1
        atr = p.get("atr")
        stop = entry - p["stop_atr"] * atr if p.get("stop_atr") and atr else -math.inf
        tgt = entry + p["target_atr"] * atr if p.get("target_atr") and atr else math.inf
        last_bar = t + int(p["max_hold_bars"])
        exit_px = why = ex = None
        for j in range(e, min(n, last_bar + 1)):
            if j > e and m.open[j] <= stop:
                exit_px, why, ex = float(m.open[j]), "STOP_GAP", j
            elif m.low[j] <= stop:
                exit_px, why, ex = stop, "STOP", j
            elif j > e and m.open[j] >= tgt:
                exit_px, why, ex = float(m.open[j]), "TARGET_GAP", j
            elif m.high[j] >= tgt:
                exit_px, why, ex = tgt, "TARGET", j
            elif m.intraday and j + 1 < n and m.session_id[j + 1] != m.session_id[j]:
                exit_px, why, ex = float(m.close[j]), "SESSION_CLOSE", j
            elif j == last_bar:
                exit_px, why, ex = float(m.close[j]), "TIME", j
            if exit_px is not None:
                break
        if exit_px is None:
            continue
        net = exit_px * (1 - sell) / (entry * (1 + buy)) - 1
        if _ins(db, tid, "EXIT", symbol, tf, source, int(m.end_ns[ex]), exit_px,
                {"reason": why, "entry": entry, "gross_return": exit_px / entry - 1, "net_return": net,
                 "bars_held": ex - t, "cost_sell": sell}):
            counts["exits"] += 1
    return counts


def summary(db: AuditDB, source: str) -> dict[str, Any]:
    rows = db.q("SELECT * FROM paper_events WHERE source=? ORDER BY seq", (source,))
    exits = [json.loads(r["payload_json"]) | {"bar": r["bar_label"]} for r in rows if r["kind"] == "EXIT"]
    nets = np.array([e["net_return"] for e in exits], float)
    st = start_ns(db, source)
    return {"source": source, "contract": PAPER_CONTRACT, "enrolled": st is not None,
            "start": ns_label(st) if st else None,
            "signals": sum(r["kind"] == "SIGNAL" for r in rows), "closed": len(exits),
            "cancelled": sum(r["kind"] == "CANCEL" for r in rows),
            "open": sum(r["kind"] == "ENTRY" for r in rows) - len(exits),
            "mean_net": round(float(nets.mean()), 6) if nets.size else None,
            "win_rate": round(float((nets > 0).mean()), 4) if nets.size else None,
            "worst": round(float(nets.min()), 6) if nets.size else None,
            "first_exit": exits[0]["bar"] if exits else None, "last_exit": exits[-1]["bar"] if exits else None,
            "fill_basis": "PAPER (simulated forward fills)",
            "evidence_ref": f"paper:{source}:{len(exits)}" if exits else None}


def paper_gate(db: AuditDB, source: str, min_trades: int) -> list[str]:
    """Reasons the paper evidence is insufficient (empty list = sufficient)."""
    s = summary(db, source)
    if not s["enrolled"]:
        return [f"{source} was never enrolled for paper trading"]
    if s["closed"] < min_trades:
        return [f"{source} has {s['closed']} closed paper trades; {min_trades} required"]
    return []


__all__ = ["PAPER_CONTRACT", "enrol", "paper_gate", "process", "start_ns", "summary"]
