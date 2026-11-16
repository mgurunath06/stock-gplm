"""Exact target-horizon contract (M5.9A).

The target of an ``h``-bar forecast is the ``h``-th eligible completed bar after the decision bar.
It is derived here -- never accepted on trust -- from, in order of authority:

1. the intraday session grid (09:15-15:30 IST, bars anchored at 09:15) for targets inside the
   decision session;
2. the registered session calendar (daily sessions, ISO weeks, next-session intraday bars);
3. for research replay only, the observed sequence of eligible (complete) bars already in the data.

If none applies the target is ``UNAVAILABLE`` with a reason; nothing is manufactured. Labels:
daily -> ``YYYY-MM-DD``; weekly and intraday -> ISO-8601 IST timestamp of the target bar's end.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..m54.candles import ist_ns
from ..m54.timeframes import SESSION_MINUTES, timeframe_minutes
from .contracts import CALENDAR_UNKNOWN, END_OF_SESSION, TARGET_HORIZON_MISMATCH

EOD_UNAVAILABLE = "unavailable"
EOD_NEXT_SESSION = "next_session_first_bar"
_NS_MIN = 60_000_000_000


def ns_label(ns: int) -> str:
    return pd.Timestamp(int(ns), unit="ns", tz="UTC").tz_convert("Asia/Kolkata").isoformat()


def label_ns(label: str) -> int:
    if "T" in label:
        stamp = pd.Timestamp(label)
        stamp = stamp.tz_localize("Asia/Kolkata") if stamp.tzinfo is None else stamp
        return int(stamp.tz_convert("UTC").value)
    return ist_ns(label[:10], "15:30")


def _bucket_end_mos(bucket: int, n: int) -> int:
    return min((bucket + 1) * n, SESSION_MINUTES)


def grid_position(end_ns: int, session: str, timeframe: str) -> tuple[int, int]:
    """(bucket index of the bar ending at ``end_ns``, number of grid bars left in the session after it)."""
    n = timeframe_minutes(timeframe)
    end_mos = (int(end_ns) - ist_ns(session, "09:15")) // _NS_MIN
    bucket = max(0, int((end_mos - 1) // n))
    total = -(-SESSION_MINUTES // n)  # ceil: a short final bucket is a legitimate grid bar
    return bucket, total - bucket - 1


def _result(target: str | None, derivation: str, reason: str | None = None) -> dict[str, Any]:
    return {"status": "DERIVED" if target else "UNAVAILABLE", "target": target,
            "target_end_ns": label_ns(target) if target else None, "derivation": derivation, "reason": reason}


def _observed(frame: pd.DataFrame | None, decision_end_ns: int, h: int, daily: bool) -> str | None:
    if frame is None or not len(frame):
        return None
    ok = ~frame["incomplete"].to_numpy(dtype=bool)
    ends = frame["end_ns"].to_numpy(dtype=np.int64)
    after = np.flatnonzero(ok & (ends > int(decision_end_ns)))
    if len(after) < h:
        return None
    row = int(after[h - 1])
    return str(frame["session"].iloc[row]) if daily else ns_label(int(ends[row]))


def derive_target(timeframe: str, decision_session: str, decision_end_ns: int, horizon: int, *,
                  calendar: Any = None, observed_frame: pd.DataFrame | None = None,
                  eod_policy: str = EOD_UNAVAILABLE) -> dict[str, Any]:
    """Derive the exact target bar of an ``horizon``-bar forecast from ``decision``."""
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if timeframe == "1d":
        if calendar is not None:
            nxt = calendar.next_sessions(decision_session, horizon)
            if nxt is not None:
                return _result(nxt[-1], "REGISTERED_CALENDAR")
        obs = _observed(observed_frame, decision_end_ns, horizon, daily=True)
        return _result(obs, "OBSERVED_ELIGIBLE_BARS") if obs else _result(None, "NONE", CALENDAR_UNKNOWN)
    if timeframe == "1w":
        if calendar is None:
            return _result(None, "NONE", CALENDAR_UNKNOWN)
        d = date.fromisoformat(decision_session[:10])
        monday = d - timedelta(days=d.weekday())
        found = 0
        for _ in range(horizon + 8):
            monday += timedelta(days=7)
            sessions = calendar.week_sessions(monday.isoformat())
            if sessions is None:
                return _result(None, "NONE", CALENDAR_UNKNOWN)
            if sessions:
                found += 1
                if found == horizon:
                    return _result(ns_label(ist_ns(sessions[-1], "15:30")), "REGISTERED_CALENDAR")
        return _result(None, "NONE", CALENDAR_UNKNOWN)
    n = timeframe_minutes(timeframe)
    bucket, left = grid_position(decision_end_ns, decision_session, timeframe)
    if horizon <= left:
        end = _bucket_end_mos(bucket + horizon, n)
        return _result(ns_label(ist_ns(decision_session, "09:15") + end * _NS_MIN), "SESSION_GRID")
    if eod_policy != EOD_NEXT_SESSION:
        return _result(None, "NONE", END_OF_SESSION)
    into_next = horizon - left  # 1 -> first bar of the next session
    if calendar is not None:
        nxt = calendar.next_sessions(decision_session[:10], 1)
        if nxt is not None and into_next <= -(-SESSION_MINUTES // n):
            end = _bucket_end_mos(into_next - 1, n)
            return _result(ns_label(ist_ns(nxt[0], "09:15") + end * _NS_MIN), "REGISTERED_CALENDAR+SESSION_GRID")
    obs = _observed(observed_frame, decision_end_ns, horizon, daily=False)
    return _result(obs, "OBSERVED_ELIGIBLE_BARS") if obs else _result(None, "NONE", CALENDAR_UNKNOWN)


def same_target(a: str, b: str) -> bool:
    if ("T" in a) != ("T" in b):
        return False
    return label_ns(a) == label_ns(b) if "T" in a else a[:10] == b[:10]


def check_explicit_target(explicit: str | None, derived: dict[str, Any], horizon: int) -> str:
    """Return the frozen target; reject an explicit target that disagrees with the horizon."""
    if derived["status"] != "DERIVED":
        raise ValueError(f"{derived['reason']}: cannot derive the {horizon}-bar target; no forecast is frozen")
    if explicit is not None and not same_target(explicit, derived["target"]):
        raise ValueError(f"{TARGET_HORIZON_MISMATCH}: explicit target {explicit} != horizon-derived target "
                         f"{derived['target']} for horizon={horizon} ({derived['derivation']})")
    return str(derived["target"])


def eligible_bars_between(frame: pd.DataFrame, decision_end_ns: int, target_end_ns: int) -> int:
    """Eligible (complete) bars in (decision, target]: the bars actually held."""
    ok = ~frame["incomplete"].to_numpy(dtype=bool)
    ends = frame["end_ns"].to_numpy(dtype=np.int64)
    return int(np.count_nonzero(ok & (ends > int(decision_end_ns)) & (ends <= int(target_end_ns))))


__all__ = ["derive_target", "check_explicit_target", "eligible_bars_between", "grid_position", "same_target",
           "ns_label", "label_ns", "EOD_UNAVAILABLE", "EOD_NEXT_SESSION"]
