"""M6.1 — persistent indicator store with full provenance.

Each stored value is keyed by symbol, timeframe and bar end time, and records the indicator, its
actual parameters, the output name, the input revision (the lane's data fingerprint), the calculation
version (lane feature-space hash + :data:`INDICATOR_STORE_CONTRACT`) and a quality status:

* ``OK`` -- finite value computed from completed bars;
* ``UNAVAILABLE`` -- insufficient history or undefined (never stored as 0; ``value`` is NULL);
* ``QUARANTINED`` -- the bar is blocked/incomplete or inside the quarantine window after a data
  discontinuity (the value, if any, is kept for audit but must not be used for decisions).

Rows are append-only; recomputation with a new input revision or calculation version adds rows and
never rewrites earlier ones, so a report can always show the exact value a decision used.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .db import AuditDB, jdump, utc_now
from .targets import ns_label

INDICATOR_STORE_CONTRACT = "stock-gplm.indicator-store.m61.v1"
OK, UNAVAILABLE, QUARANTINED = "OK", "UNAVAILABLE", "QUARANTINED"


def calc_version(feature_config_sha: str) -> str:
    return f"{INDICATOR_STORE_CONTRACT}:{feature_config_sha[:16]}"


def lane_rows(lane: Any, bar_indices: Iterable[int], features: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Indicator rows of ``lane`` at the given bar indices (pure; nothing is written)."""
    names = list(lane.names)
    cols = [names.index(f) for f in features if f in names] if features else list(range(len(names)))
    m = lane.market
    rev, ver = lane.data_fingerprint, calc_version(lane.feature_config_sha)
    out: list[dict[str, Any]] = []
    for i in bar_indices:
        i = int(i)
        if i < 0 or i >= len(m):
            continue
        quarantined = bool(m.blocked[i] or m.no_signal[i])
        for j in cols:
            name = names[j]
            d = lane.defs.get(name)
            v = float(lane.X[i, j])
            finite = math.isfinite(v)
            out.append({"symbol": lane.symbol, "timeframe": lane.timeframe, "end_ns": int(m.end_ns[i]),
                        "bar_label": ns_label(int(m.end_ns[i])), "feature": name,
                        "indicator": getattr(d, "indicator", name), "params": dict(getattr(d, "params", ()) or ()),
                        "output": getattr(d, "output", None), "value": v if finite else None,
                        "status": QUARANTINED if quarantined else (OK if finite else UNAVAILABLE),
                        "input_revision": rev, "calc_version": ver})
    return out


def store(db: AuditDB, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Append rows (``INSERT OR IGNORE`` on the full provenance key). Returns inserted/kept counts."""
    ins = 0
    now = utc_now()
    with db.tx() as c:
        for r in rows:
            ins += c.execute(
                "INSERT OR IGNORE INTO indicator_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["symbol"], r["timeframe"], int(r["end_ns"]), r["bar_label"], r["feature"], r["indicator"],
                 jdump(r["params"]), r.get("output"), r["value"], r["status"], r["input_revision"], r["calc_version"],
                 now, db.run_id)).rowcount
    if ins:
        db.event("INDICATORS_STORED", {"inserted": ins, "offered": len(rows),
                                       "contract": INDICATOR_STORE_CONTRACT}, subject=rows[0]["symbol"] if rows else None)
    return {"inserted": ins, "already_present": len(rows) - ins}


def snapshot_lane(db: AuditDB, lane: Any, last: int = 1, features: Sequence[str] | None = None) -> dict[str, Any]:
    """Store the last ``last`` bars of a lane."""
    n = len(lane.market)
    rows = lane_rows(lane, range(max(0, n - int(last)), n), features)
    res = store(db, rows)
    st: dict[str, int] = {}
    for r in rows:
        st[r["status"]] = st.get(r["status"], 0) + 1
    return {"lane": lane.key, "bars": min(n, int(last)), "values": len(rows), "status_counts": st, **res}


def values_at(db: AuditDB, symbol: str, timeframe: str, end_ns: int, features: Sequence[str] | None = None
              ) -> list[dict[str, Any]]:
    """Latest-calculated stored values of one bar (most recent input revision / version first)."""
    rows = db.q("SELECT * FROM indicator_values WHERE symbol=? AND timeframe=? AND end_ns=? ORDER BY created_utc DESC",
                (symbol, timeframe, int(end_ns)))
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["feature"] in seen or (features and r["feature"] not in features):
            continue
        seen.add(r["feature"])
        r["params"] = json.loads(r.pop("params_json"))
        out.append(r)
    return out


def as_of_value(lane: Any, feature: str, i: int) -> dict[str, Any]:
    """One value with its status, for reports (no DB)."""
    rows = lane_rows(lane, [i], [feature])
    return rows[0] if rows else {"feature": feature, "status": UNAVAILABLE, "value": None}


__all__ = ["INDICATOR_STORE_CONTRACT", "OK", "QUARANTINED", "UNAVAILABLE", "as_of_value", "calc_version", "lane_rows",
           "snapshot_lane", "store", "values_at"]
