"""Performance statistics for simulated portfolios, with explicit undefined metrics.

A metric that is not mathematically meaningful for the sample is reported as
``None`` with a reason in ``undefined``; it is never replaced by zero.
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


def _drawdown(equity: Sequence[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def performance_metrics(result: SimulationResult, initial_capital: float) -> dict[str, Any]:
    """Return headline statistics plus reasons for every undefined metric."""
    trades = result.trades
    curve = result.equity_curve
    equity = [float(point["equity"]) for point in curve]
    undefined: dict[str, str] = {}
    returns = [float(t["net_return"]) for t in trades]
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    ending = equity[-1]
    total_return = ending / initial_capital - 1.0
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
        "annualized_return": maybe(
            "annualized_return", sessions >= MIN_SESSIONS_FOR_ANNUALIZATION and ending > 0,
            f"requires at least {MIN_SESSIONS_FOR_ANNUALIZATION} sessions; observed {sessions}",
            lambda: (ending / initial_capital) ** (SESSIONS_PER_YEAR / sessions) - 1.0),
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
                            "total_return_includes_unrealized_marks": True}}


def equal_weight_buy_and_hold(
    instruments: Sequence[InstrumentSignals], sessions: Sequence[str], start: str, end: str,
    initial_capital: float, costs: CostModel,
) -> dict[str, Any]:
    """Equal-weight buy-and-hold of the same instruments over the same sessions.

    Each instrument with a bar on the first evaluated session is bought at that
    open (with slippage and buy fees) and marked at its last observed close in
    range (with sell slippage and fees). Units may be fractional. This is a
    same-universe reference, not a market index.
    """
    axis = [d for d in sessions if start <= d <= end]
    first = axis[0]
    members = [item for item in instruments if any(str(b["date"]) == first for b in item.bars)]
    if not members:
        return {"name": "EQUAL_WEIGHT_BUY_AND_HOLD_SAME_UNIVERSE", "total_return": None,
                "undefined_reason": "no instrument has a bar on the first evaluated session", "members": []}
    allocation = initial_capital / len(members)
    ending = 0.0
    for item in members:
        bars = [b for b in item.bars if first <= str(b["date"]) <= end]
        entry = float(bars[0]["open"]) * (1 + costs.slippage_rate) * (1 + costs.buy_fee_rate)
        exit_value = float(bars[-1]["close"]) * (1 - costs.slippage_rate) * (1 - costs.sell_fee_rate)
        units = allocation / entry
        for event in item.share_events:  # validated splits/bonuses change the held units, not value
            if first < event.ex_date <= str(bars[-1]["date"]):
                units /= event.price_factor
        ending += units * exit_value
    return {"name": "EQUAL_WEIGHT_BUY_AND_HOLD_SAME_UNIVERSE", "total_return": ending / initial_capital - 1.0,
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
