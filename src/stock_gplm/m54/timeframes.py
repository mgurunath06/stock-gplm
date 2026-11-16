"""Multi-timeframe bars from verified source candles, and causal cross-timeframe alignment.

Every bar carries two instants (nanoseconds since the epoch, UTC):

* ``end_ns``   when the bar's last source candle has closed (its values are final);
* ``avail_ns`` when a *different* timeframe may first read the bar. For resampled minute
  bars and weekly bars this equals ``end_ns``. For daily candles read from an intraday lane
  it is 00:00 IST of the next calendar day, because the provider's daily publication time
  is unknown (the M3.4 convention), so an intraday bar only ever sees the prior session.

:func:`align` maps a higher-timeframe series onto base bars with the rule *latest HTF bar
whose avail_ns <= base end_ns*. A base bar's own signal is formed at its close, so no
HTF value is used before its source candle closed.

Resampling never fills: a bucket missing source minutes is still built from what exists
and is flagged ``incomplete`` so the engine suppresses signals on it. Off-session minutes
were already dropped by the loader. Weekly bars group sessions by ISO week; no holiday is
inferred, and a week is final only with registered session-calendar evidence (M5.9A).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .candles import ist_ns

INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "4h": 240}
ALL_TIMEFRAMES = (*INTRADAY_MINUTES, "1d", "1w")
SESSION_OPEN = "09:15"
SESSION_MINUTES = 375  # 09:15 .. 15:29 labels, from the observed (non-authoritative) session policy


@dataclass
class Bars:
    """One timeframe's bars. ``frame`` columns: session, open, high, low, close, volume, end_ns, avail_ns,
    incomplete, and for intraday lanes vwap_pv (price*volume, for session VWAP) and minute_of_session."""

    timeframe: str
    frame: pd.DataFrame
    source: str  # e.g. "daily candles", "resampled from 1m"

    @property
    def intraday(self) -> bool:
        return self.timeframe in INTRADAY_MINUTES

    def __len__(self) -> int:
        return len(self.frame)

    def sessions(self) -> int:
        return int(self.frame["session"].nunique()) if len(self.frame) else 0

    def summary(self) -> dict[str, Any]:
        f = self.frame
        return {"timeframe": self.timeframe, "source": self.source, "bars": len(f), "sessions": self.sessions(),
                "first": str(f["session"].iloc[0]) if len(f) else None,
                "last": str(f["session"].iloc[-1]) if len(f) else None,
                "incomplete_bars": int(f["incomplete"].sum()) if len(f) else 0}


def daily_bars(daily: pd.DataFrame) -> Bars:
    if daily.empty:
        return Bars("1d", pd.DataFrame(), "daily candles")
    f = pd.DataFrame({"session": daily["date"].astype(str), **{c: daily[c].astype(float) for c in
                                                             ("open", "high", "low", "close", "volume")}})
    f["end_ns"] = [ist_ns(s, "15:30") for s in f["session"]]
    # A daily candle is read by other timeframes only from the next calendar day (publication time unknown).
    f["avail_ns"] = [ist_ns((date.fromisoformat(s) + timedelta(days=1)).isoformat(), "00:00") for s in f["session"]]
    f["incomplete"] = False
    return Bars("1d", f.reset_index(drop=True), "daily candles (ShoonyaDailyV1)")


WEEK_COMPLETE = "COMPLETE"
WEEK_PARTIAL = "PARTIAL"
WEEK_CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
WEEK_MISSING_SESSION = "MISSING_EXPECTED_BAR"
WEEK_CALENDAR_CONFLICT = "CALENDAR_CONFLICT"


def _week_status(observed: Sequence[str], calendar: Any) -> tuple[str, str | None]:
    """Completeness of one ISO week from registered session evidence (M5.9A).

    Returns ``(status, last_expected_session)``. Only ``COMPLETE`` weeks are final. Without a calendar,
    or when any day of the week is UNKNOWN, the week is ``CALENDAR_UNKNOWN``; Friday is never assumed.
    """
    if calendar is None:
        return WEEK_CALENDAR_UNKNOWN, None
    expected = calendar.week_sessions(observed[0])
    if expected is None:
        return WEEK_CALENDAR_UNKNOWN, None
    seen = set(observed)
    if seen - set(expected):
        return WEEK_CALENDAR_CONFLICT, expected[-1] if expected else None
    missing = [d for d in expected if d not in seen]
    if not missing:
        return WEEK_COMPLETE, expected[-1]
    if all(d > max(observed) for d in missing):
        return WEEK_PARTIAL, expected[-1]  # the week is still open (or its tail is not yet loaded)
    return WEEK_MISSING_SESSION, expected[-1]


def weekly_bars(daily: Bars, calendar: Any = None) -> Bars:
    """ISO-week bars. A bar is final (``incomplete=False``) only when a registered session calendar
    proves every session of its week is present; ``week_status`` records why otherwise.

    ``avail_ns`` (when other timeframes may read the bar): with a calendar, the close of the week's
    last expected session (holiday-shortened weeks become readable on their real last session);
    without one, the legacy non-authoritative rule ``max(last session close, Friday 15:30 IST)`` is
    kept for higher-timeframe feature alignment only -- such bars are never decision/target bars.
    """
    if not len(daily):
        return Bars("1w", pd.DataFrame(), "resampled from 1d")
    f = daily.frame
    iso = [date.fromisoformat(s).isocalendar() for s in f["session"]]
    key = pd.Series([f"{y}-W{w:02d}" for y, w, _ in iso])
    g = f.groupby(key, sort=False)
    out = pd.DataFrame({"session": g["session"].last(), "open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum(),
                        "end_ns": g["end_ns"].last(), "sessions_in_week": g["session"].count()})
    members = g["session"].apply(list)
    statuses, avail = [], []
    for wk, last in zip(out.index, out["session"]):
        status, last_expected = _week_status(members[wk], calendar)
        statuses.append(status)
        if last_expected is not None:
            avail.append(max(ist_ns(last_expected, "15:30"), ist_ns(last, "15:30")))
        else:
            d = date.fromisoformat(last)
            avail.append(max(ist_ns(last, "15:30"), ist_ns((d + timedelta(days=4 - d.weekday())).isoformat(), "15:30")))
    out["avail_ns"] = np.array(avail, dtype=np.int64)
    out["week_status"] = statuses
    out["incomplete"] = out["week_status"] != WEEK_COMPLETE
    src = (f"resampled from 1d (ISO week; registered calendar {getattr(calendar, 'source', '?')})" if calendar is not None
           else "resampled from 1d (ISO week; no registered calendar: weeks are not final)")
    return Bars("1w", out.reset_index(drop=True), src)


def minute_frame(minute: pd.DataFrame) -> Bars:
    """1-minute bars from the loader's accepted regular-session minutes."""
    if minute.empty:
        return Bars("1m", pd.DataFrame(), "1-minute candles (ShoonyaMinuteV1)")
    f = minute.copy()
    start = pd.to_datetime(f["start_ist"])
    mos = (start.dt.hour * 60 + start.dt.minute) - (9 * 60 + 15)
    f["minute_of_session"] = mos.astype(int)
    # Bar label semantics are undocumented; the bar is treated as final one minute after its label (M3.4).
    f["end_ns"] = (start.dt.tz_localize("Asia/Kolkata").astype("int64") + 60_000_000_000).to_numpy()
    f["avail_ns"] = f["end_ns"]
    f["vwap_pv"] = (f["high"] + f["low"] + f["close"]) / 3 * f["volume"]
    f["incomplete"] = False
    cols = ["session", "open", "high", "low", "close", "volume", "end_ns", "avail_ns", "incomplete", "vwap_pv",
            "minute_of_session"]
    return Bars("1m", f[cols].reset_index(drop=True), "1-minute candles (ShoonyaMinuteV1)")


def resample_intraday(one: Bars, timeframe: str) -> Bars:
    n = timeframe_minutes(timeframe)
    if timeframe == "1m" or not len(one):
        return one if timeframe == "1m" else Bars(timeframe, pd.DataFrame(), f"resampled from 1m")
    f = one.frame
    bucket = f["minute_of_session"] // n
    key = [f["session"], bucket]
    g = f.groupby(key, sort=True)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "vwap_pv": g["vwap_pv"].sum(),
                        "count": g["open"].count(), "first_mos": g["minute_of_session"].min()}).reset_index()
    out.columns = ["session", "bucket", *out.columns[2:]]
    # A withheld (NaN) source volume makes the bucket's volume unknown rather than a partial sum.
    vol_nan = f["volume"].isna().groupby(key, sort=True).any().to_numpy()
    if vol_nan.any():
        out.loc[vol_nan, ["volume", "vwap_pv"]] = np.nan
    expected = np.minimum(n, SESSION_MINUTES - out["bucket"] * n)
    out["incomplete"] = out["count"] < expected
    out["minute_of_session"] = out["bucket"] * n
    end_mos = np.minimum((out["bucket"] + 1) * n, SESSION_MINUTES)
    base = np.array([ist_ns(s, SESSION_OPEN) for s in out["session"]], dtype=np.int64)
    out["end_ns"] = base + end_mos.to_numpy(dtype=np.int64) * 60_000_000_000
    out["avail_ns"] = out["end_ns"]
    cols = ["session", "open", "high", "low", "close", "volume", "end_ns", "avail_ns", "incomplete", "vwap_pv",
            "minute_of_session"]
    return Bars(timeframe, out[cols].reset_index(drop=True), "resampled from 1m (session-anchored at 09:15; no fill)")


def build_all(daily: pd.DataFrame, minute: pd.DataFrame,
              timeframes: Sequence[str] | None = None, calendar: Any = None) -> dict[str, Bars]:
    """Build configured lanes without filling absent source minutes.

    Any positive ``Nm`` or ``Nh`` duration can be requested. Durations are anchored at the 09:15
    session open. A final short bucket is a legitimate session remainder (4h is 240 + 135 minutes),
    while missing source minutes still mark the affected bucket incomplete.
    """
    d = daily_bars(daily)
    one = minute_frame(minute)
    lanes = {"1d": d, "1w": weekly_bars(d, calendar)}
    for tf in tuple(timeframes or INTRADAY_MINUTES):
        if tf in ("1d", "1w"):
            continue
        lanes[tf] = resample_intraday(one, tf)
    return lanes


def timeframe_minutes(timeframe: str) -> int:
    """Parse a bounded intraday ``Nm`` or ``Nh`` duration."""
    if timeframe in INTRADAY_MINUTES:
        return INTRADAY_MINUTES[timeframe]
    unit = timeframe[-1:] if timeframe else ""
    raw = timeframe[:-1]
    if unit not in {"m", "h"} or not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"unsupported intraday timeframe: {timeframe!r}")
    minutes = int(raw) * (60 if unit == "h" else 1)
    if minutes > SESSION_MINUTES:
        raise ValueError(f"intraday timeframe exceeds the {SESSION_MINUTES}-minute NSE session: {timeframe!r}")
    return minutes


def align(base_end_ns: np.ndarray, htf_avail_ns: np.ndarray, htf_values: np.ndarray) -> np.ndarray:
    """Causal as-of join: for each base bar, the latest HTF value with avail_ns <= the base bar's end_ns."""
    idx = np.searchsorted(htf_avail_ns, base_end_ns, side="right") - 1
    out = np.full(len(base_end_ns), np.nan)
    ok = idx >= 0
    out[ok] = htf_values[idx[ok]]
    return out


def align_index(base_end_ns: np.ndarray, htf_avail_ns: np.ndarray) -> np.ndarray:
    return np.searchsorted(htf_avail_ns, base_end_ns, side="right") - 1


def bars_per_year(timeframe: str) -> float:
    if timeframe == "1d":
        return 248.0
    if timeframe == "1w":
        return 52.0
    return 248.0 * SESSION_MINUTES / timeframe_minutes(timeframe)


def session_ids(sessions: Sequence[str]) -> np.ndarray:
    _, inv = np.unique(np.asarray(sessions), return_inverse=True)
    return inv.astype(np.int64)


def frame_hash_input(bars: Mapping[str, Bars]) -> dict[str, Any]:
    return {tf: b.summary() for tf, b in bars.items()}
