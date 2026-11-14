"""Shared, conservative daily-bar execution rules for every V1 simulator.

Daily OHLC bars do not reveal intrabar ordering, so the rules below are fixed,
documented assumptions rather than tunable parameters:

* Decisions use a completed session close and execute at the next available
  session open (entries and signal/holding-period exits).
* Stops and targets are resting orders evaluated against the session bar.
* An open at or through the stop fills at the open (gap loss is not hidden).
* An open at or through the target fills at the target, never the better open.
* When one bar touches both levels, the stop is assumed to fill first.
"""
from __future__ import annotations

from typing import Any, Mapping

EXECUTION_POLICY = "completed_close_signal_next_session_open_v2"
INTRABAR_POLICY = "gap_through_stop_fills_at_open;target_capped_at_level;stop_first_when_both_touched"


def resting_exit(candle: Mapping[str, Any], stop_price: float, target_price: float) -> tuple[float, str] | None:
    """Return the conservative fill price and reason for resting stop/target orders."""
    opening = float(candle["open"])
    if opening <= stop_price:
        return opening, "STOP_LOSS_GAP"
    if opening >= target_price:
        return target_price, "TAKE_PROFIT_GAP"
    if float(candle["low"]) <= stop_price:
        return stop_price, "STOP_LOSS"
    if float(candle["high"]) >= target_price:
        return target_price, "TAKE_PROFIT"
    return None
