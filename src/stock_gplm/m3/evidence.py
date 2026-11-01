"""Session axis and reference-evidence views for point-in-time research."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Mapping

from stock_gplm.m2.models import CorporateActionCatalog, InstrumentInventory, NSETradingCalendar

SessionSource = Literal["AUTHORITATIVE_CALENDAR", "EMPIRICAL_UNION"]
MembershipStatus = Literal[
    "LISTED", "PRE_LISTING", "DELISTED", "ALIAS_MISMATCH", "UNKNOWN",
]
SESSION_STATUSES = frozenset({"TRADING_SESSION", "SPECIAL_SESSION"})


@dataclass(frozen=True)
class SessionAxis:
    """Ordered sessions that horizons and gaps are counted on.

    An empirical axis lets research code run before a calendar exists, but it
    can never satisfy the authoritative-calendar gate.
    """

    sessions: tuple[str, ...]
    source: SessionSource
    _positions: dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.sessions:
            raise ValueError("session axis must not be empty")
        if self.source not in {"AUTHORITATIVE_CALENDAR", "EMPIRICAL_UNION"}:
            raise ValueError("unsupported session axis source")
        ordered = [date.fromisoformat(item) for item in self.sessions]
        if any(left >= right for left, right in zip(ordered, ordered[1:])):
            raise ValueError("session axis must be strictly ascending")
        object.__setattr__(
            self, "_positions", {item: index for index, item in enumerate(self.sessions)},
        )

    @classmethod
    def from_calendar(cls, calendar: NSETradingCalendar) -> "SessionAxis":
        if not calendar.coverage_complete:
            raise ValueError("an authoritative session axis requires a complete calendar")
        if any(item.status == "UNKNOWN" for item in calendar.days):
            raise ValueError("an authoritative session axis cannot contain UNKNOWN days")
        return cls(
            tuple(sorted(item.date for item in calendar.days if item.status in SESSION_STATUSES)),
            "AUTHORITATIVE_CALENDAR",
        )

    def position(self, day: str) -> int:
        try:
            return self._positions[day]
        except KeyError as exc:
            raise ValueError(f"candle date is not a session on this axis: {day}") from exc

    def __len__(self) -> int:
        return len(self.sessions)


@dataclass(frozen=True)
class ReferenceEvidence:
    """Supplied reference facts; anything absent stays UNKNOWN and is never inferred."""

    inventory: InstrumentInventory | None = None
    catalogs: Mapping[str, CorporateActionCatalog] = field(default_factory=dict)

    def discontinuities(self, symbol: str) -> tuple[str, ...]:
        """Effective dates that break an unadjusted price series.

        Prices are never adjusted here, so each action not explicitly marked
        NOT_APPLICABLE restarts the series instead of being bridged.
        """
        catalog = self.catalogs.get(symbol)
        if catalog is None:
            return ()
        return tuple(sorted({
            item.effective_date for item in catalog.actions
            if item.adjustment_status != "NOT_APPLICABLE"
        }))

    def corporate_action_coverage(self, symbol: str, start: str, end: str) -> Literal["COMPLETE", "UNKNOWN"]:
        catalog = self.catalogs.get(symbol)
        if catalog is not None and catalog.covers(date.fromisoformat(start), date.fromisoformat(end)):
            return "COMPLETE"
        return "UNKNOWN"

    def membership(self, symbol: str, day: str) -> MembershipStatus:
        if self.inventory is None:
            return "UNKNOWN"
        current = date.fromisoformat(day)
        if self.inventory.resolve(symbol, current) is not None:
            return "LISTED"
        instrument = self.inventory.get(symbol)
        if instrument is None:
            return "UNKNOWN"
        status = instrument.membership_on(current)
        # Listed under another name on this date, so this series is not that instrument.
        return "ALIAS_MISMATCH" if status == "LISTED" else status

    def delisting_date(self, symbol: str) -> str | None:
        instrument = self.inventory.get(symbol) if self.inventory is not None else None
        return instrument.delisting_date if instrument is not None else None

    def alias_history_complete(self) -> bool:
        return self.inventory is not None and bool(self.inventory.instruments) and all(
            item.alias_history_status == "COMPLETE" for item in self.inventory.instruments
        )
