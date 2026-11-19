"""M7.0 — the opportunity record and cross-stock ranking (PRD §7).

An :class:`OpportunityInput` gathers what is known at one completed decision bar for one symbol and
one source (an approved/candidate strategy or a model). :func:`build` turns it into the PRD record:

identity, timestamps and expiry, action (BUY / HOLD / EXIT / SELL_HOLDING / NO_TRADE), horizon,
strategy and model versions, trigger and entry zone, stop-loss and invalidation, targets, expected
gross and net return, probability and confidence, expected holding period, market regime, supporting
timeframe forecasts and indicator values (conflicts explained, never auto-suppressed), historical
evidence with sample sizes, limitations, data freshness, and the reason for no-trade.

Status comes only from the canonical :func:`contracts.live_decision` (plus risk and control blockers).
Nothing is fabricated: a missing input stays missing and the record says so.

Ranking (:func:`rank`): actionable BUY records are ordered by
``score = expected_net x p_lower / sqrt(holding_bars)`` where ``p_lower`` is the Wilson 90% lower bound
of the evidence probability, so thin evidence ranks below equally good but better-sampled evidence.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from . import contracts as C
from .db import AuditDB, jdump, sha, utc_now
from .targets import ns_label

OPPORTUNITY_CONTRACT = "stock-gplm.opportunity.m70.v1"
ACTIONS = ("BUY", "HOLD", "EXIT", "SELL_HOLDING", "NO_TRADE")
DEFAULTS = {"min_samples": 30, "min_expected_net": 0.0, "entry_zone_atr": 0.25, "entry_valid_bars": 1,
            "stop_atr": 2.0, "target_atr": 3.0, "second_target_mult": 1.5, "wilson_z": 1.645}


def wilson_lower(p: float, n: int, z: float = 1.645) -> float:
    if n <= 0 or not math.isfinite(p):
        return 0.0
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d)


@dataclass
class OpportunityInput:
    symbol: str
    timeframe: str
    horizon: str  # bucket name (e.g. d1_7)
    horizon_bars: int
    decision_ns: int
    close: float
    atr: float | None
    source_kind: str  # STRATEGY or MODEL
    source_id: str
    source_version: str | None
    signal: bool  # entry condition true at the decision bar (strategy) or model says UP
    evidence: Mapping[str, Any]  # n, mean_net, mean_gross, win_rate or p_up, mean_bars, scope, calibrated
    exit: Mapping[str, Any] = field(default_factory=dict)  # stop_atr, target_atr, max_hold_bars
    model_version: str | None = None
    regime: Mapping[str, Any] = field(default_factory=dict)
    timeframe_forecasts: Sequence[Mapping[str, Any]] = ()
    indicators: Sequence[Mapping[str, Any]] = ()
    freshness: C.Freshness | None = None
    holding: Mapping[str, Any] | None = None  # {"qty", "avg_price"} when the user holds the stock
    governance: Mapping[str, Any] = field(default_factory=dict)  # live_decision kwargs gathered by the caller
    extra_blockers: Sequence[str] = ()  # risk / control blockers
    limitations: Sequence[str] = ()


def build(inp: OpportunityInput, cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    c = {**DEFAULTS, **(cfg or {})}
    ev = dict(inp.evidence or {})
    n = int(ev.get("n") or 0)
    p = ev.get("p_up", ev.get("win_rate"))
    p = float(p) if p is not None and math.isfinite(float(p)) else None
    exp_net = ev.get("mean_net")
    exp_net = float(exp_net) if exp_net is not None and math.isfinite(float(exp_net)) else None
    atr = inp.atr if inp.atr and math.isfinite(inp.atr) and inp.atr > 0 else None
    stop_atr = float(inp.exit.get("stop_atr") or c["stop_atr"])
    tgt_atr = float(inp.exit.get("target_atr") or c["target_atr"])
    hold = int(inp.exit.get("max_hold_bars") or inp.horizon_bars)
    reasons: list[str] = []
    limitations = list(inp.limitations)
    # ---- action
    held = bool(inp.holding and float(inp.holding.get("qty") or 0) > 0)
    positive = inp.signal and exp_net is not None and exp_net > c["min_expected_net"]
    if inp.freshness is not None and not inp.freshness.fresh:
        reasons.append(f"DATA_NOT_FRESH:{inp.freshness.status}")
    if n < int(c["min_samples"]):
        reasons.append(f"WEAK_EVIDENCE:n={n}<{c['min_samples']}")
    if exp_net is None:
        reasons.append("NO_EXPECTED_RETURN")
    elif exp_net <= c["min_expected_net"]:
        reasons.append("NEGATIVE_EXPECTED_NET")
    if not inp.signal:
        reasons.append("NO_SIGNAL")
    if atr is None:
        reasons.append("ATR_UNAVAILABLE")
    if held:
        action = "HOLD" if positive and not reasons else ("SELL_HOLDING" if exp_net is not None and exp_net < 0
                                                         else "HOLD")
    else:
        action = "BUY" if not reasons else "NO_TRADE"
    # ---- levels (only when ATR is known; never invented)
    levels: dict[str, Any] = {"trigger": None, "entry_zone": None, "stop": None, "invalidation": None, "targets": []}
    if atr is not None:
        levels = {"trigger": round(inp.close, 4),
                  "entry_zone": [round(inp.close, 4), round(inp.close + c["entry_zone_atr"] * atr, 4)],
                  "stop": round(inp.close - stop_atr * atr, 4),
                  "invalidation": f"close below {round(inp.close - stop_atr * atr, 4)} or entry zone not reached "
                                  f"within {c['entry_valid_bars']} bar(s)",
                  "targets": [round(inp.close + tgt_atr * atr, 4),
                              round(inp.close + tgt_atr * c["second_target_mult"] * atr, 4)]}
    # ---- confidence and conflicts
    p_lo = wilson_lower(p, n, c["wilson_z"]) if p is not None else 0.0
    conf = "HIGH" if n >= 200 and p_lo > 0.55 else "MEDIUM" if n >= 60 and p_lo > 0.5 else "LOW"
    want = "UP" if action in ("BUY", "HOLD") else "DOWN"
    conflicts = [f"{f.get('timeframe')} forecast {f.get('direction')} (p_up={f.get('p_up')})"
                 for f in inp.timeframe_forecasts if f.get("direction") in ("UP", "DOWN") and f.get("direction") != want]
    if conflicts and conf != "LOW":
        conf = "MEDIUM" if conf == "HIGH" else "LOW"
    if not ev.get("calibrated"):
        limitations.append("probability is a historical win rate / model output, not a calibrated probability")
    if ev.get("scope") == "UNIVERSE":
        limitations.append("evidence learned across the wider universe, not this stock alone")
    if n and n < 100:
        limitations.append(f"small sample ({n} trades)")
    limitations.append("research decision support only; no order is placed")
    dec_label = ns_label(inp.decision_ns)
    fields = {"action": "BUY" if action == "BUY" else None, "entry": levels["entry_zone"],
              "invalidation": levels["invalidation"], "stop": levels["stop"],
              "target": levels["targets"][0] if levels["targets"] else None, "horizon": inp.horizon_bars,
              "probability": p, "expected_return": exp_net, "model_version": inp.model_version or inp.source_version,
              "timestamp": dec_label, "freshness_status": inp.freshness.status if inp.freshness else None,
              "strategy_version": inp.source_version}
    missing = C.opportunity_missing(fields, strategy_involved=inp.source_kind == "STRATEGY") if action == "BUY" else []
    g = dict(inp.governance or {})
    blockers = list(inp.extra_blockers) + ([] if action == "BUY" else [f"ACTION_{action}"])
    decision = C.live_decision(latest_eligible_bar=g.get("latest_eligible_bar", False), freshness=inp.freshness,
                               calendar_id=g.get("calendar_id"), calendar_covers=g.get("calendar_covers", False),
                               model=g.get("model"), adaptive_model=g.get("adaptive_model", False),
                               model_required=inp.source_kind == "MODEL",
                               strategy_driven=inp.source_kind == "STRATEGY", strategy=g.get("strategy"),
                               universe=g.get("universe"), opportunity_missing_fields=missing,
                               cost_policy_version=g.get("cost_policy_version"),
                               neutral_band_version=g.get("neutral_band_version", C.NEUTRAL_BAND_VERSION),
                               gate_blockers=blockers)
    score = (exp_net * p_lo / math.sqrt(max(1, hold))) if (action == "BUY" and exp_net is not None) else None
    nat = f"{inp.symbol}|{inp.timeframe}|{inp.horizon}|{inp.source_kind}:{inp.source_id}@{inp.source_version}|{inp.decision_ns}"
    rec = {"contract": OPPORTUNITY_CONTRACT, "opportunity_id": "op-" + sha(nat)[:20], "natural_key": nat,
           "symbol": inp.symbol, "timeframe": inp.timeframe, "horizon": inp.horizon, "horizon_bars": inp.horizon_bars,
           "decision_bar": dec_label, "decision_ns": inp.decision_ns, "created_utc": utc_now(),
           "entry_valid_bars": int(c["entry_valid_bars"]), "action": action, **levels,
           "expected_gross": ev.get("mean_gross"), "expected_net": exp_net, "probability": p,
           "probability_lower": round(p_lo, 4), "confidence": conf, "expected_holding_bars": ev.get("mean_bars") or hold,
           "source": {"kind": inp.source_kind, "id": inp.source_id, "version": inp.source_version},
           "model_version": inp.model_version, "regime": dict(inp.regime),
           "timeframe_forecasts": [dict(f) for f in inp.timeframe_forecasts], "conflicts": conflicts,
           "indicators": [dict(i) for i in inp.indicators],
           "evidence": {**ev, "samples": n}, "limitations": limitations,
           "freshness": inp.freshness.to_dict() if inp.freshness else None,
           "no_trade_reasons": reasons if action in ("NO_TRADE", "SELL_HOLDING") else [],
           "status": decision.status, "status_reasons": decision.reasons, "live_decision": decision.to_dict(),
           "score": None if score is None else round(score, 8), "rank": None}
    return rec


def rank(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign ``rank`` to BUY records with a score (best first); others keep ``rank=None``. Stable order."""
    buys = sorted([r for r in records if r["action"] == "BUY" and r.get("score") is not None],
                  key=lambda r: (-r["score"], r["symbol"], r["natural_key"]))
    for i, r in enumerate(buys, 1):
        r["rank"] = i
    return buys + [r for r in records if r not in buys]


def store(db: AuditDB, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Append records (natural key unique: re-running the same decision bar stores nothing new)."""
    new = 0
    with db.tx() as c:
        for r in records:
            new += c.execute("INSERT OR IGNORE INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (r["opportunity_id"], r["natural_key"], r["symbol"], r["timeframe"], r["horizon"],
                              r["action"], r["status"], r.get("rank"), r.get("score"), r["decision_bar"],
                              int(r["decision_ns"]), None, jdump(r), r["created_utc"], db.run_id)).rowcount
    if new:
        db.event("OPPORTUNITIES_STORED", {"new": new, "offered": len(records)}, subject="opportunities")
    return {"stored": new, "already_present": len(records) - new}


__all__ = ["ACTIONS", "DEFAULTS", "OPPORTUNITY_CONTRACT", "OpportunityInput", "build", "rank", "store", "wilson_lower"]
