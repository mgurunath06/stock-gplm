"""Generic long-only portfolio execution simulator for evaluated V1 signals.

Signal generation (``evaluator``) is separate from allocation and execution
here. Nothing in this module inspects strategy identity: the only strategy
inputs are the validated risk, holding-period and sizing sections plus the
boolean signal arrays computed by the shared evaluator.

Accounting conventions (fixed, documented, not tuned):

* Cash never goes negative and gross exposure never exceeds
  ``max_gross_exposure`` (at most 1.0, so no margin). Quantities are whole
  shares at entry.
* Entries size from equity at the previous completed close.
* Slippage moves each fill price against the trade; commission and statutory
  charges are basis points of fill notional, debited from cash.
* Orders execute only on a session where the instrument has an observed bar.
  A missing entry session cancels the entry. A missing exit session defers the
  exit to the instrument's next observed open. No price is ever filled in.
* Equity marks use each instrument's latest observed close. A stale mark (no
  bar on that session) is counted, not hidden.
* Positions open at the end stay open and are reported as unrealized at the
  final mark. They are never converted into synthetic trades.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from .execution import EXECUTION_POLICY, INTRABAR_POLICY, resting_exit

SIMULATOR_VERSION = "stock-gplm.v1-portfolio-simulator.v1"


@dataclass(frozen=True)
class ShareEvent:
    """A validated split/bonus effective at ``ex_date`` for an open position.

    ``price_factor`` is the backward price factor (old/new shares). The held
    quantity is divided by it and price levels are multiplied by it, so economic
    value is unchanged at the ex-date boundary.
    """

    ex_date: str
    price_factor: float
    event_id: str


@dataclass(frozen=True)
class InstrumentSignals:
    symbol: str
    bars: tuple[Mapping[str, Any], ...]
    entry_signals: tuple[bool, ...]
    exit_signals: tuple[bool, ...]
    share_events: tuple[ShareEvent, ...] = ()

    def __post_init__(self) -> None:
        if not (len(self.bars) == len(self.entry_signals) == len(self.exit_signals)):
            raise ValueError(f"{self.symbol}: bars and signal arrays differ in length")
        dates = [str(bar["date"]) for bar in self.bars]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise ValueError(f"{self.symbol}: bars must be unique and ascending")


@dataclass(frozen=True)
class CostModel:
    """Per-side costs in basis points of fill notional; all values explicit."""

    commission_bps: float
    slippage_bps: float
    buy_statutory_bps: float
    sell_statutory_bps: float

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"cost {name} must be a finite non-negative number")
        if self.slippage_bps >= 10_000 or self.commission_bps + max(self.buy_statutory_bps, self.sell_statutory_bps) >= 10_000:
            raise ValueError("costs must stay below 100% of notional")

    @property
    def buy_fee_rate(self) -> float:
        return (self.commission_bps + self.buy_statutory_bps) / 10_000.0

    @property
    def sell_fee_rate(self) -> float:
        return (self.commission_bps + self.sell_statutory_bps) / 10_000.0

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000.0


@dataclass(frozen=True)
class PortfolioSettings:
    initial_capital: float
    max_open_positions: int
    max_position_fraction: float
    max_gross_exposure: float
    evaluation_start: str
    evaluation_end: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if type(self.max_open_positions) is not int or not 1 <= self.max_open_positions <= 500:
            raise ValueError("max_open_positions must be an integer from 1 through 500")
        if not 0 < self.max_position_fraction <= 1:
            raise ValueError("max_position_fraction must be in (0, 1]")
        if not 0 < self.max_gross_exposure <= 1:
            raise ValueError("max_gross_exposure must be in (0, 1]; margin is unsupported")
        if self.evaluation_start > self.evaluation_end:
            raise ValueError("evaluation_start is after evaluation_end")


@dataclass
class _Position:
    symbol: str
    signal_date: str
    entry_date: str
    quantity: float
    entry_reference: float
    entry_fill: float
    entry_cash: float
    entry_fee: float
    stop_price: float
    target_price: float
    held_sessions: int = 0
    share_events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SimulationResult:
    simulator_version: str
    execution_policy: str
    intrabar_policy: str
    signals: tuple[dict[str, Any], ...]
    orders: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    open_positions: tuple[dict[str, Any], ...]
    equity_curve: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def simulate_portfolio(
    config: Mapping[str, Any],
    instruments: Sequence[InstrumentSignals],
    sessions: Sequence[str],
    settings: PortfolioSettings,
    costs: CostModel,
) -> SimulationResult:
    """Simulate validated signals on the explicit session axis ``sessions``."""
    axis = [day for day in sessions if settings.evaluation_start <= day <= settings.evaluation_end]
    if not axis:
        raise ValueError("no sessions inside the evaluation range")
    if list(sessions) != sorted(set(sessions)):
        raise ValueError("session axis must be unique and ascending")
    symbols = sorted(item.symbol for item in instruments)
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate instrument in simulation")
    by_symbol = {item.symbol: item for item in instruments}
    index = {item.symbol: {str(bar["date"]): i for i, bar in enumerate(item.bars)} for item in instruments}
    events = {item.symbol: {event.ex_date: event for event in item.share_events} for item in instruments}
    fraction = float(config["position_sizing"]["value"])
    stop_pct = float(config["risk"]["stop_loss"]["value"]) / 100.0
    target_pct = float(config["risk"]["take_profit"]["value"]) / 100.0
    max_hold = int(config["exit"]["max_holding_sessions"])

    cash = float(settings.initial_capital)
    positions: dict[str, _Position] = {}
    pending_entries: dict[str, str] = {}
    pending_exits: dict[str, tuple[str, str]] = {}
    last_close: dict[str, float] = {}
    previous_equity = cash
    signals: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    warnings: list[str] = []

    def bar(symbol: str, day: str) -> Mapping[str, Any] | None:
        position = index[symbol].get(day)
        return None if position is None else by_symbol[symbol].bars[position]

    def close_position(day: str, position: _Position, reference: float, fill: float, reason: str,
                       decision_date: str | None) -> None:
        nonlocal cash
        notional = position.quantity * fill
        fee = notional * costs.sell_fee_rate
        cash += notional - fee
        net = notional - fee - position.entry_cash
        gross = position.quantity * reference - position.quantity * position.entry_reference
        trades.append({
            "symbol": position.symbol, "signal_date": position.signal_date, "entry_date": position.entry_date,
            "entry_reference_price": position.entry_reference, "entry_fill_price": position.entry_fill,
            "quantity": position.quantity, "exit_decision_date": decision_date, "exit_date": day,
            "exit_reference_price": reference, "exit_fill_price": fill, "exit_reason": reason,
            "holding_sessions": position.held_sessions, "gross_pnl": gross, "fees": position.entry_fee + fee,
            "net_pnl": net, "net_return": net / position.entry_cash, "share_events": list(position.share_events),
        })
        orders.append({"date": day, "symbol": position.symbol, "side": "SELL", "status": "FILLED",
                       "reason": reason, "quantity": position.quantity, "fill_price": fill})
        del positions[position.symbol]

    for day_number, day in enumerate(axis):
        # 1. Corporate-action share events effective at today's open.
        for symbol in sorted(positions):
            event = events[symbol].get(day)
            if event is not None:
                position = positions[symbol]
                position.quantity /= event.price_factor
                for name in ("entry_reference", "entry_fill", "stop_price", "target_price"):
                    setattr(position, name, getattr(position, name) * event.price_factor)
                position.share_events.append(event.event_id)
        # 2. Exits decided at the previous close execute at this open.
        for symbol in sorted(pending_exits):
            candle = bar(symbol, day)
            if candle is None:
                warnings.append(f"{day} {symbol}: exit deferred, no observed session bar")
                continue
            reason, decided = pending_exits.pop(symbol)
            reference = float(candle["open"])
            close_position(day, positions[symbol], reference, reference * (1 - costs.slippage_rate), reason, decided)
        # 3. Entries decided at the previous close execute at this open.
        for symbol in sorted(pending_entries):
            signal_date = pending_entries.pop(symbol)
            candle = bar(symbol, day)
            record = {"date": day, "symbol": symbol, "side": "BUY", "signal_date": signal_date}
            if candle is None:
                orders.append(record | {"status": "CANCELLED", "reason": "ENTRY_SESSION_MISSING"})
                continue
            if len(positions) >= settings.max_open_positions:
                orders.append(record | {"status": "REJECTED", "reason": "MAX_OPEN_POSITIONS"})
                continue
            exposure = sum(p.quantity * last_close.get(s, p.entry_fill) for s, p in positions.items())
            budget = min(fraction, settings.max_position_fraction) * previous_equity
            budget = min(budget, settings.max_gross_exposure * previous_equity - exposure)
            reference = float(candle["open"])
            fill = reference * (1 + costs.slippage_rate)
            unit_cost = fill * (1 + costs.buy_fee_rate)
            quantity = math.floor(min(budget, cash) / unit_cost) if budget > 0 else 0
            if quantity < 1:
                reason = "INSUFFICIENT_CASH" if cash < unit_cost <= budget else "BUDGET_OR_EXPOSURE_BELOW_ONE_SHARE"
                orders.append(record | {"status": "REJECTED", "reason": reason})
                continue
            notional = quantity * fill
            fee = notional * costs.buy_fee_rate
            cash -= notional + fee
            positions[symbol] = _Position(
                symbol, signal_date, day, float(quantity), reference, fill, notional + fee, fee,
                fill * (1 - stop_pct), fill * (1 + target_pct),
            )
            orders.append(record | {"status": "FILLED", "reason": "ENTRY_SIGNAL", "quantity": float(quantity),
                                    "fill_price": fill})
        # 4. Resting stop/target orders against today's bar.
        for symbol in sorted(positions):
            candle = bar(symbol, day)
            if candle is None:
                continue
            position = positions[symbol]
            position.held_sessions += 1
            fill = resting_exit(candle, position.stop_price, position.target_price)
            if fill is not None:
                pending_exits.pop(symbol, None)
                close_position(day, position, fill[0], fill[0] * (1 - costs.slippage_rate), fill[1], None)
        # 5. Mark to market at the completed close.
        stale = 0
        for symbol in symbols:
            candle = bar(symbol, day)
            if candle is not None:
                last_close[symbol] = float(candle["close"])
            elif symbol in positions:
                stale += 1
        value = sum(p.quantity * last_close[s] for s, p in positions.items())
        equity = cash + value
        if cash < -1e-6:
            raise AssertionError("cash went negative without margin support")
        curve.append({"date": day, "cash": cash, "positions_value": value, "equity": equity,
                      "gross_exposure": value / equity if equity > 0 else 0.0,
                      "open_positions": len(positions), "stale_marks": stale})
        previous_equity = equity
        # 6. Close decisions for the next session open.
        is_last = day_number == len(axis) - 1
        for symbol in symbols:
            candle = bar(symbol, day)
            if candle is None:
                continue
            position_index = index[symbol][day]
            item = by_symbol[symbol]
            if symbol in positions:
                if symbol in pending_exits:
                    continue
                if item.exit_signals[position_index]:
                    pending_exits[symbol] = ("EXIT_CONDITION", day)
                elif positions[symbol].held_sessions >= max_hold:
                    pending_exits[symbol] = ("MAX_HOLDING", day)
            elif item.entry_signals[position_index]:
                signals.append({"date": day, "symbol": symbol, "type": "ENTRY",
                                "actionable": not is_last})
                if not is_last:
                    pending_entries[symbol] = day

    for symbol in sorted(pending_exits):
        warnings.append(f"{symbol}: exit decided {pending_exits[symbol][1]} could not execute before the range end")
    open_positions = tuple(
        {"symbol": p.symbol, "signal_date": p.signal_date, "entry_date": p.entry_date, "quantity": p.quantity,
         "entry_fill_price": p.entry_fill, "mark_price": last_close[p.symbol],
         "unrealized_pnl": p.quantity * last_close[p.symbol] - p.entry_cash, "held_sessions": p.held_sessions,
         "status": "OPEN_AT_RANGE_END_NOT_A_TRADE"}
        for p in sorted(positions.values(), key=lambda x: x.symbol)
    )
    return SimulationResult(
        SIMULATOR_VERSION, EXECUTION_POLICY, INTRABAR_POLICY, tuple(signals), tuple(orders), tuple(trades),
        open_positions, tuple(curve), tuple(warnings),
    )
