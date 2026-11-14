"""Performance statistics for simulated portfolios, with explicit undefined metrics.

A metric that is not mathematically meaningful for the sample is reported as
``None`` with a reason in ``undefined``; it is never replaced by zero.

Terminal valuation (M5.1, :data:`TERMINAL_VALUATION_POLICY`), applied identically
to strategy and benchmark:

* ``total_return`` is the estimated liquidation value at the final observed
  close. Each outstanding unit is valued at ``close * (1 - slippage)`` less sell
  commission and statutory charges on that notional. This is the same exit
  convention as a realized sell.
* ``total_return_marked_to_market`` values outstanding units at the close, with
  no exit costs. The equity curve, drawdown and risk ratios stay on this basis.
* Entry costs are paid once at entry. Closed trades already paid their exit
  costs. Only positions still open are charged the estimated exit, so no cost is
  counted twice.

Rankings use ``total_return``, so strategy and benchmark compare like for like.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Mapping, Sequence

from .portfolio import CostModel, InstrumentSignals, SimulationResult

SESSIONS_PER_YEAR = 252
MIN_SESSIONS_FOR_ANNUALIZATION = 252
MIN_SESSIONS_FOR_RISK_RATIOS = 252
MIN_TRADES_FOR_RISK_RATIOS = 30
TERMINAL_VALUATION_POLICY = "LIQUIDATION_ESTIMATE_AT_FINAL_OBSERVED_CLOSE_V1"


def liquidation_unit_value(close: float, costs: CostModel) -> float:
    """Net proceeds per unit of selling at ``close``: slippage, then sell fees on the fill notional."""
    return close * (1 - costs.slippage_rate) * (1 - costs.sell_fee_rate)


def _drawdown(equity: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def performance_metrics(result: SimulationResult, initial_capital: float, costs: CostModel) -> dict[str, Any]:
    """Return headline statistics plus reasons for every undefined metric."""
    trades = result.trades
    curve = result.equity_curve
    equity = [float(point["equity"]) for point in curve]
    undefined: dict[str, str] = {}
    returns = [float(t["net_return"]) for t in trades]
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    ending = equity[-1]
    exit_costs = sum((float(p["quantity"]) * float(p["mark_price"])
                      - float(p["quantity"]) * liquidation_unit_value(float(p["mark_price"]), costs)
                      for p in result.open_positions), 0.0)
    liquidation = ending - exit_costs
    total_return = liquidation / initial_capital - 1.0
    sessions = len(curve)

    def maybe(name: str, condition: bool, reason: str, value: Any) -> Any:
        if condition:
            return value() if callable(value) else value
        undefined[name] = reason
        return None

    daily = [b / a - 1.0 for a, b in zip([initial_capital, *equity[:-1]], equity)]
    ratio_ok = sessions >= MIN_SESSIONS_FOR_RISK_RATIOS and len(trades) >= MIN_TRADES_FOR_RISK_RATIOS
    ratio_reason = (f"requires at least {MIN_SESSIONS_FOR_RISK_RATIOS} sessions and "
                    f"{MIN_TRADES_FOR_RISK_RATIOS} closed trades; observed {sessions} and {len(trades)}")
    stdev = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily) / len(daily)) if daily else 0.0
    traded = sum(float(o["quantity"]) * float(o["fill_price"]) for o in result.orders if o["status"] == "FILLED")
    mean_equity = statistics.fmean(equity)
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    metrics = {
        "signals": len(result.signals),
        "trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": maybe("win_rate", bool(trades), "no closed trades", lambda: len(wins) / len(trades)),
        "gross_pnl": sum((float(t["gross_pnl"]) for t in trades), 0.0),
        "net_pnl_realized": sum((float(t["net_pnl"]) for t in trades), 0.0),
        "unrealized_pnl": sum((float(p["unrealized_pnl"]) for p in result.open_positions), 0.0),
        "total_return": total_return,
        "total_return_marked_to_market": ending / initial_capital - 1.0,
        "terminal_exit_cost_estimate": exit_costs,
        "liquidation_equity_estimate": liquidation,
        "annualized_return": maybe(
            "annualized_return", sessions >= MIN_SESSIONS_FOR_ANNUALIZATION and liquidation > 0,
            f"requires at least {MIN_SESSIONS_FOR_ANNUALIZATION} sessions; observed {sessions}",
            lambda: (liquidation / initial_capital) ** (SESSIONS_PER_YEAR / sessions) - 1.0),
        "max_drawdown": _drawdown([initial_capital, *equity]),
        "average_trade_return": maybe("average_trade_return", bool(returns), "no closed trades",
                                      lambda: statistics.fmean(returns)),
        "median_trade_return": maybe("median_trade_return", bool(returns), "no closed trades",
                                     lambda: statistics.median(returns)),
        "profit_factor": maybe("profit_factor", gross_loss > 0, "no losing trades, so the ratio is undefined",
                               lambda: gross_profit / gross_loss),
        "average_holding_sessions": maybe("average_holding_sessions", bool(trades), "no closed trades",
                                          lambda: statistics.fmean(t["holding_sessions"] for t in trades)),
        "average_gross_exposure": statistics.fmean(float(p["gross_exposure"]) for p in curve),
        "sessions_with_position_share": sum(1 for p in curve if p["open_positions"]) / sessions,
        "turnover": traded / mean_equity if mean_equity > 0 else None,
        "ending_equity": ending,
        "sessions": sessions,
        "sharpe_ratio_annualized": maybe(
            "sharpe_ratio_annualized", ratio_ok and stdev > 0, ratio_reason if not ratio_ok else "zero variance",
            lambda: statistics.fmean(daily) / stdev * math.sqrt(SESSIONS_PER_YEAR)),
        "sortino_ratio_annualized": maybe(
            "sortino_ratio_annualized", ratio_ok and downside > 0,
            ratio_reason if not ratio_ok else "no downside variance",
            lambda: statistics.fmean(daily) / downside * math.sqrt(SESSIONS_PER_YEAR)),
    }
    return {"metrics": metrics, "undefined": undefined,
            "conventions": {"risk_free_rate": 0.0, "annualization_sessions": SESSIONS_PER_YEAR,
                            "terminal_valuation": TERMINAL_VALUATION_POLICY,
                            "total_return_includes_estimated_exit_costs_of_open_positions": True,
                            "equity_curve_basis": "MARKED_TO_MARKET_AT_CLOSE"}}


def equal_weight_buy_and_hold(
    instruments: Sequence[InstrumentSignals], sessions: Sequence[str], start: str, end: str,
    initial_capital: float, costs: CostModel,
) -> dict[str, Any]:
    """Equal-weight buy-and-hold of the same instruments over the same sessions.

    Each instrument with a bar on the first evaluated session is bought at that
    open (with slippage and buy fees) and valued at its last observed close in
    range under :data:`TERMINAL_VALUATION_POLICY`, the same terminal convention as
    the strategy. Units may be fractional. This is a same-universe reference, not
    a market index.
    """
    axis = [d for d in sessions if start <= d <= end]
    first = axis[0]
    members = [item for item in instruments if any(str(b["date"]) == first for b in item.bars)]
    if not members:
        return {"name": "EQUAL_WEIGHT_BUY_AND_HOLD_SAME_UNIVERSE", "total_return": None,
                "undefined_reason": "no instrument has a bar on the first evaluated session", "members": []}
    allocation = initial_capital / len(members)
    ending = marked = 0.0
    for item in members:
        bars = [b for b in item.bars if first <= str(b["date"]) <= end]
        entry = float(bars[0]["open"]) * (1 + costs.slippage_rate) * (1 + costs.buy_fee_rate)
        units = allocation / entry
        for event in item.share_events:  # validated splits/bonuses change the held units, not value
            if first < event.ex_date <= str(bars[-1]["date"]):
                units /= event.price_factor
        ending += units * liquidation_unit_value(float(bars[-1]["close"]), costs)
        marked += units * float(bars[-1]["close"])
    return {"name": "EQUAL_WEIGHT_BUY_AND_HOLD_SAME_UNIVERSE", "total_return": ending / initial_capital - 1.0,
            "total_return_marked_to_market": marked / initial_capital - 1.0,
            "terminal_valuation": TERMINAL_VALUATION_POLICY,
            "members": [item.symbol for item in members], "start": first, "end": axis[-1],
            "corporate_action_basis": "DATASET_PRICE_BASIS_WITH_SUPPLIED_SHARE_EVENTS"}


def comparable(runs: Sequence[Mapping[str, Any]]) -> tuple[bool, list[str]]:
    """Return whether runs can be compared fairly, and the reasons when not."""
    reasons: list[str] = []
    keys = ("eligibility_mode", "dataset_fingerprint", "evaluation_range", "cost_model", "portfolio_settings")
    for key in keys:
        if len({repr(run.get(key)) for run in runs}) > 1:
            reasons.append(f"runs differ in {key}")
    if any(run.get("eligibility_mode") != "STRICT_RESEARCH" for run in runs):
        reasons.append("only STRICT_RESEARCH runs may be ranked")
    return not reasons, reasons
