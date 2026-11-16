"""Registered exchange-session evidence for the application (M5.9A).

The only accepted source is the M2 registered NSE calendar (``stock-gplm.nse-calendar.v1``: a
checksum-bound payload plus manifest imported with :func:`stock_gplm.m2.reference_data.import_nse_calendar_v1`).
Nothing is inferred from weekdays: a date the calendar does not list is ``UNKNOWN``, and every
question whose answer needs an ``UNKNOWN`` date returns ``None`` so callers abstain.

M5.9B: the **active calendar of the governance registry** (``gplm calendar register/activate``; an
owner-supplied local file, see :mod:`.governance`) takes precedence and is the only calendar that can
support ``LIVE`` (its ``calendar_id`` is LIVE evidence). The legacy M2 configuration
(``"calendar": {"payload": "<path>", "manifest": "<path>"}``) is still honoured for research when no
registry calendar is active, but it carries no ``calendar_id`` and therefore can never yield ``LIVE``.
Without any calendar, weekly bars are never final, staleness needs an explicit ``--as-of`` session, and
no output can be ``LIVE`` (reason ``CALENDAR_UNKNOWN``).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

SESSION_STATUSES = ("TRADING_SESSION", "SPECIAL_SESSION")
SESSION_CLOSE = time(15, 30)


class SessionCalendar:
    """Read-only view over a registered :class:`~stock_gplm.m2.models.NSETradingCalendar`."""

    def __init__(self, calendar: Any, source: str, calendar_id: str | None = None,
                 close_times: Mapping[str, str] | None = None):
        self.calendar = calendar
        self.source = source
        self.calendar_id = calendar_id  # set only for an active governance-registry calendar
        self._status = {d.date: d.status for d in calendar.days}
        self._close = dict(close_times or {})
        self.coverage = (min(self._status), max(self._status)) if self._status else (None, None)

    @classmethod
    def from_registry(cls, calendar_id: str, days: Mapping[str, Mapping[str, Any]], sha256: str) -> "SessionCalendar":
        """Build from a registered (validated, hash-bound) governance calendar's day table."""
        from types import SimpleNamespace

        status_map = {"OPEN": "TRADING_SESSION", "SPECIAL": "SPECIAL_SESSION", "PARTIAL": "SPECIAL_SESSION",
                      "CLOSED": "CLOSED"}
        rows = tuple(SimpleNamespace(date=d, status=status_map[v["status"]]) for d, v in sorted(days.items()))
        closes = {d: str(v["close"]) for d, v in days.items() if v.get("close")}
        return cls(SimpleNamespace(days=rows), f"registry:{calendar_id}@{sha256[:12]}", calendar_id, closes)

    def close_time(self, day: str | date) -> time:
        """Registered session close (exchange local time); the standard close for the legacy M2 calendar."""
        key = day.isoformat() if isinstance(day, date) else str(day)[:10]
        c = self._close.get(key)
        return time.fromisoformat(c) if c else SESSION_CLOSE

    def covers(self, *days: str | None) -> bool:
        """True when every given date is listed (inside coverage); a None date is not covered."""
        return all(d is not None and self.status(str(d)[:10]) != "UNKNOWN" for d in days)

    @classmethod
    def from_statuses(cls, statuses: Mapping[str, str], *, source: str, as_of_date: str) -> "SessionCalendar":
        """Build from explicit per-date statuses (tests and already-registered evidence only)."""
        from ..m2.models import CalendarDay, NSETradingCalendar, Provenance

        prov = Provenance(source=source, as_of_date=as_of_date)
        days = tuple(CalendarDay(date=d, status=s, provenance=prov) for d, s in sorted(statuses.items()))
        return cls(NSETradingCalendar(days=days), source)

    def status(self, day: str | date) -> str:
        key = day.isoformat() if isinstance(day, date) else str(day)[:10]
        return self._status.get(key, "UNKNOWN")

    def is_session(self, day: str | date) -> bool | None:
        s = self.status(day)
        return None if s == "UNKNOWN" else s in SESSION_STATUSES

    def sessions_between(self, start: date, end: date) -> list[str] | None:
        """Sessions in [start, end]; None if any date in the range is UNKNOWN."""
        out, d = [], start
        while d <= end:
            s = self.is_session(d)
            if s is None:
                return None
            if s:
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    def next_sessions(self, after: str, n: int, max_days: int = 60) -> list[str] | None:
        """The ``n`` sessions strictly after ``after``; None if an UNKNOWN date is met first."""
        out, d = [], date.fromisoformat(after[:10])
        for _ in range(max_days):
            d += timedelta(days=1)
            s = self.is_session(d)
            if s is None:
                return None
            if s:
                out.append(d.isoformat())
                if len(out) == n:
                    return out
        return None

    def previous_session(self, before: str, max_days: int = 60) -> str | None:
        d = date.fromisoformat(before[:10])
        for _ in range(max_days):
            d -= timedelta(days=1)
            s = self.is_session(d)
            if s is None:
                return None
            if s:
                return d.isoformat()
        return None

    def week_sessions(self, any_day: str) -> list[str] | None:
        """Sessions of the ISO week containing ``any_day`` (Mon..Sun); None if any day is UNKNOWN."""
        d = date.fromisoformat(any_day[:10])
        monday = d - timedelta(days=d.weekday())
        return self.sessions_between(monday, monday + timedelta(days=6))


def load_registered_calendar(settings: Any, db: Any = None, exchange: str = "NSE") -> SessionCalendar | None:
    """The active governance-registry calendar when ``db`` has one; otherwise the configured legacy M2
    calendar; otherwise None (callers abstain). A configured but invalid/corrupt calendar raises: broken
    evidence is never silently ignored, and nothing is inferred from weekdays."""
    if db is not None:
        from .governance import active_calendar

        active = active_calendar(db, exchange)
        if active is not None:
            return active
    try:
        cfg = settings.section("calendar") or {}
    except Exception:  # settings objects without sections
        return None
    payload, manifest = cfg.get("payload"), cfg.get("manifest")
    if not payload or not manifest:
        return None
    from ..m2.reference_data import import_nse_calendar_v1

    imported = import_nse_calendar_v1(Path(payload), Path(manifest))
    return SessionCalendar(imported.calendar, f"registered:{Path(manifest).name}")


def expected_completed_session(calendar: SessionCalendar | None, as_of: str | None,
                               now: datetime | None = None) -> tuple[str | None, bool]:
    """The exchange session whose completed bar should be the latest one.

    Returns ``(session, calendar_confirmed)``. An explicit ``as_of`` date is honoured as the requested
    session (historical replay); the calendar, when present, confirms it or moves a closed date back to
    the previous session. Without ``as_of`` the session is derived from ``now`` (IST) and needs the
    calendar; without one the result is ``(None, False)`` -> ``CALENDAR_UNKNOWN``.
    """
    if as_of:
        day = as_of[:10]
        if calendar is None:
            return day, False
        s = calendar.is_session(day)
        if s is None:
            return None, False
        return (day, True) if s else (calendar.previous_session(day), calendar.previous_session(day) is not None)
    if calendar is None or now is None:
        return None, False
    today = now.date().isoformat()
    s = calendar.is_session(today)
    if s is None:
        return None, False
    if s and now.time() >= calendar.close_time(today):
        return today, True
    prev = calendar.previous_session(today)
    return prev, prev is not None


__all__ = ["SessionCalendar", "load_registered_calendar", "expected_completed_session", "SESSION_STATUSES"]
