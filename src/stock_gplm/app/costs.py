"""M6.0 — itemised NSE cash-equity cost and fill model.

The legacy ``costs.<profile>`` section gives flat per-side basis points. When ``costs.schedule`` is
present, the per-side rates are instead *compiled* from an itemised Indian charge schedule
(brokerage with cap, STT, exchange transaction charge, SEBI fee, stamp duty, GST, DP charge, IPFT)
at an assumed order value, plus slippage chosen from a liquidity bucket.

Every number in the shipped schedule is an ``ASSUMPTION`` the owner must verify against a broker
contract note; :func:`contract_note` itemises one fill so it can be compared line by line.

Point-in-time rule: a lane's liquidity bucket is chosen from the **median traded value of its
discovery segment** (the first 60% of bars, the same split the strategy engine optimises on), so the
validation and test segments never influence their own slippage.

Fill conventions (unchanged, :mod:`stock_gplm.app.strategy_engine`): signal at bar close, entry at
the next open, stop checked before target on the same bar (conservative), gaps fill at the worse
open (``STOP_GAP`` / ``TARGET_GAP``). All fills produced by backtests and paper trading are
``SIMULATED``; :data:`FILL_BASIS` names the provenance values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.backtest import Costs

COST_CONTRACT = "stock-gplm.cost-schedule.m60.v1"
FILL_BASIS = ("SIMULATED", "PAPER", "ACTUAL")
PROFILES = ("delivery", "intraday")


class CostError(ValueError):
    pass


def _f(d: Mapping[str, Any], k: str, default: float = 0.0) -> float:
    v = d.get(k, default)
    if v is None:
        return default
    v = float(v)
    if not math.isfinite(v) or v < 0:
        raise CostError(f"cost item {k!r} must be a finite non-negative number, got {v!r}")
    return v


def validate_schedule(schedule: Mapping[str, Any]) -> list[str]:
    """Problems with a ``costs.schedule`` section (empty list = valid)."""
    errs: list[str] = []
    if not isinstance(schedule, Mapping):
        return ["schedule must be an object"]
    if float(schedule.get("assumed_order_value_inr", 0) or 0) <= 0:
        errs.append("assumed_order_value_inr must be > 0")
    for prof in PROFILES:
        p = schedule.get(prof)
        if not isinstance(p, Mapping):
            errs.append(f"missing profile {prof!r}")
            continue
        for k in ("stt_buy_pct", "stt_sell_pct", "exchange_txn_pct", "sebi_fee_pct", "stamp_buy_pct", "gst_pct",
                  "ipft_pct", "dp_charge_inr_per_sell"):
            try:
                _f(p, k)
            except CostError as exc:
                errs.append(f"{prof}.{exc}")
        b = p.get("brokerage", {})
        if not isinstance(b, Mapping):
            errs.append(f"{prof}.brokerage must be an object")
    buckets = schedule.get("slippage_buckets")
    if not isinstance(buckets, list) or not buckets:
        errs.append("slippage_buckets must be a non-empty list")
    else:
        mins = [float(b.get("min_traded_value_inr", -1)) for b in buckets]
        if mins != sorted(mins, reverse=True) or mins[-1] != 0:
            errs.append("slippage_buckets must be sorted by min_traded_value_inr descending and end with 0")
    return errs


@dataclass(frozen=True)
class Leg:
    """Itemised charges (INR) of one fill."""

    side: str
    notional: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    ipft: float
    gst: float
    dp_charge: float
    slippage: float

    @property
    def statutory(self) -> float:
        return self.stt + self.exchange_txn + self.sebi_fee + self.stamp_duty + self.ipft + self.gst + self.dp_charge

    @property
    def total(self) -> float:
        return self.brokerage + self.statutory + self.slippage

    def to_dict(self) -> dict[str, Any]:
        d = {k: round(getattr(self, k), 4) for k in ("notional", "brokerage", "stt", "exchange_txn", "sebi_fee",
                                                       "stamp_duty", "ipft", "gst", "dp_charge", "slippage")}
        return {"side": self.side, **d, "statutory": round(self.statutory, 4), "total": round(self.total, 4),
                "total_bps": round(self.total / self.notional * 1e4, 4) if self.notional else None}


def slippage_bps(schedule: Mapping[str, Any], median_traded_value_inr: float | None) -> tuple[float, str]:
    """(bps, bucket name) for a median per-session traded value; unknown liquidity takes the worst bucket."""
    buckets = schedule["slippage_buckets"]
    if median_traded_value_inr is None or not math.isfinite(median_traded_value_inr):
        b = buckets[-1]
        return float(b["bps"]), f"{b.get('name', 'lowest')}(LIQUIDITY_UNKNOWN)"
    for b in buckets:
        if median_traded_value_inr >= float(b["min_traded_value_inr"]):
            return float(b["bps"]), str(b.get("name", b["min_traded_value_inr"]))
    b = buckets[-1]
    return float(b["bps"]), str(b.get("name", "lowest"))


def leg(schedule: Mapping[str, Any], profile: str, side: str, notional: float, slip_bps: float = 0.0) -> Leg:
    """Itemise one fill of ``notional`` INR (``side`` BUY or SELL)."""
    if profile not in PROFILES:
        raise CostError(f"unknown cost profile {profile!r}")
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise CostError("side must be BUY or SELL")
    p = schedule[profile]
    b = p.get("brokerage", {}) or {}
    brk = notional * _f(b, "pct") / 100 + _f(b, "flat_inr")
    cap = b.get("cap_inr")
    if cap is not None:
        brk = min(brk, float(cap))
    pct = lambda k: notional * _f(p, k) / 100  # noqa: E731
    stt = pct("stt_buy_pct") if side == "BUY" else pct("stt_sell_pct")
    exch, sebi, ipft = pct("exchange_txn_pct"), pct("sebi_fee_pct"), pct("ipft_pct")
    stamp = pct("stamp_buy_pct") if side == "BUY" else 0.0
    gst = (brk + exch + sebi + ipft) * _f(p, "gst_pct") / 100
    dp = _f(p, "dp_charge_inr_per_sell") if side == "SELL" else 0.0
    return Leg(side, notional, brk, stt, exch, sebi, stamp, ipft, gst, dp, notional * slip_bps / 1e4)


def contract_note(schedule: Mapping[str, Any], profile: str, qty: float, buy_price: float, sell_price: float,
                  slip_bps: float = 0.0) -> dict[str, Any]:
    """Round trip of ``qty`` shares: itemised legs, total charges and net P&L (INR)."""
    b = leg(schedule, profile, "BUY", qty * buy_price, slip_bps)
    s = leg(schedule, profile, "SELL", qty * sell_price, slip_bps)
    gross = qty * (sell_price - buy_price)
    return {"contract": COST_CONTRACT, "profile": profile, "qty": qty, "buy": b.to_dict(), "sell": s.to_dict(),
            "gross_pnl": round(gross, 4), "charges": round(b.total + s.total, 4),
            "net_pnl": round(gross - b.total - s.total, 4), "fill_basis": "SIMULATED"}


def compile_profile(schedule: Mapping[str, Any], profile: str, slip_bps: float) -> dict[str, float]:
    """Per-side fractional costs at the schedule's assumed order value (legacy bps shape + detail)."""
    v = float(schedule["assumed_order_value_inr"])
    b, s = leg(schedule, profile, "BUY", v, slip_bps), leg(schedule, profile, "SELL", v, slip_bps)
    return {"commission_bps": 0.0, "slippage_bps": slip_bps,
            "buy_statutory_bps": (b.brokerage + b.statutory) / v * 1e4,
            "sell_statutory_bps": (s.brokerage + s.statutory) / v * 1e4}


def median_traded_value(sessions: Sequence[str], close: np.ndarray, volume: np.ndarray, upto: int) -> float | None:
    """Median per-session traded value (close x volume summed per session) over bars ``[0, upto)``."""
    if upto <= 0:
        return None
    tv = np.asarray(close[:upto], float) * np.asarray(volume[:upto], float)
    sess = np.asarray(sessions[:upto])
    ok = np.isfinite(tv)
    if not ok.any():
        return None
    _, inv = np.unique(sess[ok], return_inverse=True)
    per = np.bincount(inv, weights=tv[ok])
    return float(np.median(per)) if per.size else None


def lane_costs(costs_cfg: Mapping[str, Any], profile: str, sessions: Sequence[str] | None = None,
               close: np.ndarray | None = None, volume: np.ndarray | None = None,
               discovery_fraction: float = 0.60) -> tuple[Costs, dict[str, Any]]:
    """Per-side :class:`Costs` for one lane plus a detail record (stored with backtests).

    Without ``costs_cfg['schedule']`` this is exactly the legacy flat-bps model."""
    sched = costs_cfg.get("schedule") if isinstance(costs_cfg, Mapping) else None
    if not sched or not sched.get("enabled", True):
        c = Costs.from_bps(costs_cfg[profile])
        return c, {"model": "flat_bps", "profile": profile, "buy": c.buy, "sell": c.sell,
                   "basis": costs_cfg[profile].get("basis")}
    errs = validate_schedule(sched)
    if errs:
        raise CostError("; ".join(errs))
    mtv = None
    if sessions is not None and close is not None and volume is not None and len(close):
        mtv = median_traded_value(sessions, close, volume, int(len(close) * discovery_fraction))
    slip, bucket = slippage_bps(sched, mtv)
    bps = compile_profile(sched, profile, slip)
    c = Costs.from_bps(bps)
    return c, {"model": COST_CONTRACT, "profile": profile, "buy": c.buy, "sell": c.sell,
               "slippage_bps": slip, "liquidity_bucket": bucket,
               "median_traded_value_inr": None if mtv is None else round(mtv, 2),
               "liquidity_basis": f"discovery segment (first {discovery_fraction:.0%} of bars)",
               "assumed_order_value_inr": sched["assumed_order_value_inr"], "basis": sched.get("basis")}


__all__ = ["COST_CONTRACT", "CostError", "FILL_BASIS", "Leg", "compile_profile", "contract_note", "lane_costs", "leg",
           "median_traded_value", "slippage_bps", "validate_schedule"]
